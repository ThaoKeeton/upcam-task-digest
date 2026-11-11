using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;

namespace Upcam.Desktop;

public sealed partial class MainWindow
{
    // This conversation is deliberately process-local. It is not part of the
    // saved settings or the scheduled digest, and navigation does not reset it.
    private readonly List<ChatLine> _chatMessages = new();
    private string _chatInput = "";
    private string? _chatDraftId;
    private string _chatDraftAnswer = "";
    private bool _chatSendRequested;
    private TextBox? _chatInputControl;

    private sealed record ChatLine(string Speaker, string Text);

    private UIElement BuildChat()
    {
        var conversation = Identify(new StackPanel { Spacing = 10 }, "chat-conversation");
        conversation.Children.Add(Paragraph(UiStrings.T("使用已保存的项目配置查询 Teambition。聊天内容会交给 OpenAI 理解，必要时相关项目内容也会发送以规划或整理；本机对话仅在本次运行期间保存在内存中。"), true));
        if (_chatMessages.Count == 0)
            conversation.Children.Add(Card(Paragraph(UiStrings.T("输入问题后，助手会规划只读查询并展示结果。可以继续追问。"), true)));
        foreach (ChatLine line in _chatMessages)
        {
            var text = Paragraph(line.Text);
            text.IsTextSelectionEnabled = true;
            conversation.Children.Add(Card(Column(Paragraph(UiStrings.T(line.Speaker), true), text)));
        }

        var scroll = Identify(new ScrollViewer
        {
            Content = conversation,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            Padding = new Thickness(0, 0, 6, 0)
        }, "chat-scroll");
        scroll.Loaded += (_, _) => DispatcherQueue.TryEnqueue(() => scroll.ChangeView(null, scroll.ScrollableHeight, null));

        var input = Identify(new TextBox
        {
            Header = UiStrings.T("输入问题"),
            PlaceholderText = UiStrings.T("例如：项目中最近有哪些任务更新？"),
            // Enable multiline before assigning text, or WinUI can retain only its first line.
            AcceptsReturn = true,
            Text = _chatInput,
            TextWrapping = TextWrapping.Wrap,
            Height = 84
        }, "chat-message-input");
        ScrollViewer.SetVerticalScrollBarVisibility(input, ScrollBarVisibility.Auto);
        _chatInputControl = input;
        input.TextChanging += (_, _) =>
        {
            // WinUI may deliver events from a control that navigation replaced.
            if (_page == "chat" && ReferenceEquals(_chatInputControl, input)) _chatInput = input.Text;
        };

        var ask = Identify(new Button { Content = UiStrings.T("查询") }, "chat-ask");
        ask.Style = (Style)Application.Current.Resources["AccentButtonStyle"];
        ask.Click += async (_, _) => await SendChatMessageAsync();
        var reset = Identify(new Button { Content = UiStrings.T("清空对话") }, "chat-reset");
        reset.Click += async (_, _) => await ResetChatAsync();
        var send = Identify(new Button
        {
            Content = UiStrings.T("预览并发送到钉钉"),
            IsEnabled = !string.IsNullOrEmpty(_chatDraftId)
        }, "chat-send-preview");
        send.Click += async (_, _) => await SendChatDraftAsync();
        var resolve = Identify(new Button { Content = UiStrings.T("解除发送阻断") }, "chat-resolve-send");
        resolve.Click += async (_, _) => await ResolveChatSendAsync();

        string sendHint = _chatSendRequested && !string.IsNullOrEmpty(_chatDraftId)
            ? "已提出发送请求；请先预览并明确确认。"
            : "如需分享最新答复，请点击预览并确认；查询不会自动发送。";
        var composer = Column(input, Toolbar(ask, reset, send, resolve), Paragraph(UiStrings.T(sendHint), true));
        // Reclaim 24 DIP for the conversation at minimum height with a two-line status.
        composer.Spacing = 4;
        var layout = new Grid { RowSpacing = 4 };
        layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        layout.Children.Add(scroll);
        layout.Children.Add(composer); Grid.SetRow(composer, 1);
        return layout;
    }

    private void RefreshChatIfVisible()
    {
        if (_page == "chat") PageHost.Content = BuildChat();
    }

    private async Task SendChatMessageAsync()
    {
        string text = _chatInput.Trim();
        if (string.IsNullOrWhiteSpace(text))
        {
            SetStatus("请输入问题。");
            return;
        }
        bool started = false;
        bool completed = await RunOperationAsync("正在规划只读查询并整理答复…", async () =>
        {
            started = true;
            // A new turn invalidates the previous send candidate before any I/O.
            _chatDraftId = null;
            _chatDraftAnswer = "";
            _chatSendRequested = false;
            _chatInput = "";
            _chatMessages.Add(new ChatLine("你", text));
            RefreshChatIfVisible();

            JsonObject result = await Worker.SendAsync("chat_turn", new JsonObject { ["message"] = text });
            string answer = Value(result, "answer");
            if (string.IsNullOrWhiteSpace(answer))
                throw new InvalidOperationException("本次查询没有返回可显示的答复；未发送到钉钉。");
            _chatMessages.Add(new ChatLine("助手", answer));
            string draftId = Value(result, "draft_id");
            _chatDraftId = string.IsNullOrWhiteSpace(draftId) ? null : draftId;
            _chatDraftAnswer = _chatDraftId is null ? "" : answer;
            _chatSendRequested = result["send_requested"] is JsonValue flag &&
                flag.TryGetValue<bool>(out bool requested) && requested;
            SetStatus(Value(result, "status", "查询完成；没有自动发送到钉钉。"));
            RefreshChatIfVisible();
        });
        if (started && !completed)
        {
            _chatInput = text;
            _chatMessages.Add(new ChatLine("系统", UiStrings.T("本次查询未完成，问题已保留在输入框中；不会自动重试。")));
            RefreshChatIfVisible();
        }
    }

    private async Task ResetChatAsync()
    {
        await RunOperationAsync("正在清空本次对话…", async () =>
        {
            // Even if the reset response is lost, an old draft must not be sent.
            _chatDraftId = null;
            _chatDraftAnswer = "";
            _chatSendRequested = false;
            RefreshChatIfVisible();
            JsonObject result = await Worker.SendAsync("chat_reset");
            _chatMessages.Clear();
            _chatInput = "";
            SetStatus(Value(result, "status", "已清空本次对话。"));
            RefreshChatIfVisible();
        });
    }

    private async Task SendChatDraftAsync()
    {
        const string previewMismatch = "发送预览与答复不一致，已阻止发送。请重新打开预览后再确认。";
        string? draftId = _chatDraftId;
        string answer = _chatDraftAnswer;
        if (string.IsNullOrEmpty(draftId) || string.IsNullOrWhiteSpace(answer))
        {
            SetStatus("没有可发送的最新答复。");
            return;
        }

        var previewText = Identify(new TextBox
        {
            AcceptsReturn = true, Text = answer, IsReadOnly = true,
            TextWrapping = TextWrapping.Wrap, Height = 240
        }, "chat-send-preview-text");
        ScrollViewer.SetVerticalScrollBarVisibility(previewText, ScrollBarVisibility.Auto);
        if (!ChatPreviewMatches(answer, previewText.Text))
        {
            SetStatus(previewMismatch);
            return;
        }
        var preview = Identify(new ContentDialog
        {
            XamlRoot = RootGrid.XamlRoot,
            Title = UiStrings.T("预览并确认发送"),
            PrimaryButtonText = UiStrings.T("确认发送"),
            CloseButtonText = UiStrings.T("取消"),
            DefaultButton = ContentDialogButton.Close,
            Content = Column(
                Paragraph(UiStrings.T("将使用已保存的钉钉机器人发送以下完整内容，不会自动添加 @ 成员。确认前不会发送。"), true),
                previewText)
        }, "chat-send-confirm");
        if (await ShowDialogAsync(preview) != ContentDialogResult.Primary) return;
        if (!ChatPreviewMatches(answer, previewText.Text))
        {
            SetStatus(previewMismatch);
            return;
        }
        if (_chatDraftId != draftId || _chatDraftAnswer != answer)
        {
            SetStatus("答复已变化，请重新预览后再发送。");
            return;
        }

        await RunOperationAsync("正在发送已确认的答复…", async () =>
        {
            // Never offer a blind retry after a request whose result is unknown.
            _chatDraftId = null;
            _chatDraftAnswer = "";
            _chatSendRequested = false;
            RefreshChatIfVisible();
            try
            {
                JsonObject result = await Worker.SendAsync("chat_send", new JsonObject
                {
                    ["draft_id"] = draftId,
                    ["request_id"] = Guid.NewGuid().ToString("D")
                });
                string state = Value(result, "state");
                string status = Value(result, "status");
                if (state == "confirmed")
                    status = string.IsNullOrWhiteSpace(status) ? "已发送到钉钉。" : status;
                else if (state == "not_sent")
                    status = string.IsNullOrWhiteSpace(status) ? "本次未发送。" : status;
                else
                    status = "发送结果未能确认。请先检查钉钉群；不会自动重试本次发送。";
                _chatMessages.Add(new ChatLine("系统", UiStrings.T(status)));
                SetStatus(status);
            }
            catch (Exception)
            {
                // A disconnect, timeout, or malformed response is not proof of non-delivery.
                const string uncertain = "发送结果未能确认。请先检查钉钉群；不会自动重试本次发送。";
                _chatMessages.Add(new ChatLine("系统", UiStrings.T(uncertain)));
                SetStatus(uncertain);
            }
            RefreshChatIfVisible();
        });
    }

    private async Task ResolveChatSendAsync()
    {
        const string originalMismatch = "原文预览与匹配答复不一致，已阻止解除发送阻断。请重新打开核对后再确认。";
        void Report(string message)
        {
            _chatMessages.Add(new ChatLine("系统", UiStrings.T(message)));
            SetStatus(message);
            RefreshChatIfVisible();
        }

        // Preview is read-only. Confirmation below applies only to its specific
        // record and destination, which the worker checks again atomically.
        JsonObject? previewResult = null;
        bool inspected = await RunOperationAsync(UiStrings.T("正在读取待核对的发送记录…"), async () =>
        {
            try { previewResult = await Worker.SendAsync("chat_resolve_preview"); }
            catch (Exception) { /* A read failure never starts resolution. */ }
        });
        if (!inspected) return;
        if (previewResult is null)
        {
            Report(UiStrings.T("未能确认待核对记录；没有解除发送阻断，不会自动重试。"));
            return;
        }

        string state, status, previewId, requestId, destinationLabel, contentLabel, contentHash, attemptedAtUtc;
        try
        {
            state = Value(previewResult, "state");
            status = Value(previewResult, "status");
            if (state != "unknown")
            {
                Report(string.IsNullOrWhiteSpace(status) ?
                    UiStrings.T("未能确认待核对记录；没有解除发送阻断，不会自动重试。") : status);
                return;
            }
            previewId = Value(previewResult, "preview_id");
            requestId = Value(previewResult, "request_id");
            destinationLabel = Value(previewResult, "destination_label");
            contentLabel = Value(previewResult, "content_label");
            contentHash = Value(previewResult, "content_hash");
            attemptedAtUtc = Value(previewResult, "attempted_at_utc");
        }
        catch (Exception)
        {
            Report(UiStrings.T("未能确认待核对记录；没有解除发送阻断，不会自动重试。"));
            return;
        }

        static bool ValidId(string value) => Guid.TryParseExact(value, "D", out Guid id) &&
            value == id.ToString("D") && value[14] == '4' && "89ab".Contains(value[19]);
        static bool ValidHash(string value, int length) => value.Length == length && value.All(c =>
            (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'));
        bool validTime = attemptedAtUtc.Contains('T') &&
            (attemptedAtUtc.EndsWith("Z", StringComparison.Ordinal) || attemptedAtUtc.EndsWith("+00:00", StringComparison.Ordinal)) &&
            attemptedAtUtc.All(c => char.IsAsciiDigit(c) || c is '-' or 'T' or ':' or '.' or '+' or 'Z') &&
            DateTimeOffset.TryParse(attemptedAtUtc, System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.None, out DateTimeOffset attemptedAt) && attemptedAt.Offset == TimeSpan.Zero;
        if (!ValidId(previewId) || !ValidId(requestId) || !ValidHash(destinationLabel, 12) ||
            !ValidHash(contentLabel, 12) || !ValidHash(contentHash, 64) ||
            !contentHash.StartsWith(contentLabel, StringComparison.Ordinal) || !validTime)
        {
            Report(UiStrings.T("待核对记录信息不完整或无效；没有解除发送阻断。"));
            return;
        }

        static string? AnswerHash(string text)
        {
            try
            {
                byte[] bytes = new System.Text.UTF8Encoding(false, true).GetBytes(text);
                return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(bytes)).ToLowerInvariant();
            }
            catch (System.Text.EncoderFallbackException) { return null; }
        }
        // The ledger stores only a hash. Reuse in-memory answer text only when
        // exactly one assistant reply matches that entire hash byte-for-byte.
        var matchingAnswers = _chatMessages.Where(line =>
            line.Speaker == "助手" && AnswerHash(line.Text) == contentHash).Take(2).ToList();
        var details = Column(
            Paragraph(UiStrings.T("请先在钉钉核对之前的消息。确认后只解除这条记录的发送阻断，仅允许之后的新消息；原消息不会自动重发，仍在发送中的记录不能解除。")),
            Paragraph(UiStrings.F("机器人标识：{0}", destinationLabel)),
            Paragraph(UiStrings.F("记录编号：{0}", requestId)),
            Paragraph(UiStrings.F("发送尝试时间（UTC）：{0}", attemptedAtUtc)),
            Paragraph(UiStrings.F("内容校验短哈希：{0}", contentLabel)));
        TextBox? original = null;
        string? originalAnswer = null;
        if (matchingAnswers.Count == 1)
        {
            originalAnswer = matchingAnswers[0].Text;
            details.Children.Add(Paragraph(UiStrings.T("本次运行中匹配的原文"), true));
            original = Identify(new TextBox
            {
                AcceptsReturn = true, Text = originalAnswer, IsReadOnly = true,
                TextWrapping = TextWrapping.Wrap, Height = 140
            }, "chat-resolve-original-text");
            ScrollViewer.SetVerticalScrollBarVisibility(original, ScrollBarVisibility.Auto);
            details.Children.Add(original);
        }
        else
        {
            details.Children.Add(Paragraph(UiStrings.T("本次运行没有可匹配的原文；仅在你能从钉钉记录确认具体消息时继续，否则取消。")));
        }
        if (original is not null && !ChatPreviewMatches(originalAnswer!, original.Text))
        {
            Report(originalMismatch);
            return;
        }

        var dialog = Identify(new ContentDialog
        {
            XamlRoot = RootGrid.XamlRoot,
            Title = UiStrings.T("解除发送阻断"),
            PrimaryButtonText = UiStrings.T("确认解除"),
            CloseButtonText = UiStrings.T("取消"),
            DefaultButton = ContentDialogButton.Close,
            Content = new ScrollViewer
            {
                MaxHeight = 260,
                Content = details
            }
        }, "chat-resolve-confirm");
        if (await ShowDialogAsync(dialog) != ContentDialogResult.Primary) return;
        if (original is not null && !ChatPreviewMatches(originalAnswer!, original.Text))
        {
            Report(originalMismatch);
            return;
        }

        await RunOperationAsync(UiStrings.T("正在处理发送阻断…"), async () =>
        {
            string resultStatus;
            try
            {
                JsonObject result = await Worker.SendAsync("chat_resolve_send", new JsonObject
                {
                    ["confirmed"] = true,
                    ["preview_id"] = previewId
                });
                string resultState = Value(result, "state");
                resultStatus = Value(result, "status");
                if (resultState is not ("resolved" or "not_found" or "in_flight" or "not_resolved") ||
                    string.IsNullOrWhiteSpace(resultStatus))
                    resultStatus = UiStrings.T("未能确认解除结果；不会自动重试。");
            }
            catch (Exception)
            {
                // A lost response does not establish whether the guard changed.
                resultStatus = UiStrings.T("未能确认解除结果；不会自动重试。");
            }
            Report(resultStatus);
        });
    }

    private static string NormalizeChatNewlines(string text) => text.Replace("\r\n", "\n").Replace("\r", "\n");

    private static bool ChatPreviewMatches(string expected, string actual)
    {
        // Only newline encoding may differ. Preserve every character, empty line
        // and trailing line; do not trim, fold whitespace or normalize Unicode.
        string[] expectedLines = NormalizeChatNewlines(expected).Split('\n');
        string[] actualLines = NormalizeChatNewlines(actual).Split('\n');
        return expectedLines.Length == actualLines.Length && expectedLines.SequenceEqual(actualLines, StringComparer.Ordinal);
    }
}
