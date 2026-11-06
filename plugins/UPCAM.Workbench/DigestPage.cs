using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using Upcam.Desktop;

namespace Upcam.Workbench;

/// <summary>A fresh native tree per factory; the subscription exists only while this tree is loaded.</summary>
internal sealed partial class DigestPage : Grid
{
    private readonly PluginState _state;
    private readonly PluginSession _session;
    private readonly bool _settings;
    private readonly TextBlock _status;
    private readonly TextBlock _operation;
    private readonly TextBlock _scheduleCheck;
    private readonly ContentControl _body;
    private readonly Button _save;
    private readonly Button _cancel;
    private bool _subscribed;
    private bool _rendering;
    private TextBox? _runOutput;
    private ListView? _historyList;
    private JsonArray? _historySource;

    internal DigestPage(PluginState state, PluginSession session, bool settings)
    {
        _state = state;
        _session = session;
        _settings = settings;
        Name = settings ? "UpcamSettingsPage" : "UpcamDigestPage";
        Identify(this, settings ? "settings-page" : "digest-page", settings ? "UPCAM native settings" : "UPCAM native digest");
        RowSpacing = 8;
        Padding = new Thickness(16);
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        _status = Identify(Paragraph("Open the local profile to begin."), "plugin-status", "UPCAM backend status");
        _operation = Identify(Paragraph("0 · No operation yet.", 12), "operation-result", "UPCAM operation result");
        _scheduleCheck = Identify(Paragraph("0 · 尚未检查已保存的计划。", 12), "last-schedule-check", "Last saved schedule check");
        _save = ActionButton("Save business settings", "save-settings", async () => { await _session.SaveAsync(); });
        _save.Visibility = settings ? Visibility.Visible : Visibility.Collapsed;
        _cancel = ActionButton("Cancel current operation", "cancel-operation", () => { _session.CancelCurrent(); return Task.CompletedTask; });
        var reconnect = ActionButton("Open / reconnect backend", "open-backend", async () => { await _session.ActivateAsync(); });
        var heading = Column(Paragraph(settings ? "UPCAM settings" : "UPCAM Task Digest", 24),
            Paragraph(DigestPlugin.Synthetic ? "CI synthetic data · all service operations are simulated." :
                "Local Teambition collection, OpenAI summary and configured DingTalk delivery.", 12));
        Children.Add(heading);
        var toolbar = Toolbar(_save, reconnect, _cancel);
        Children.Add(toolbar); SetRow(toolbar, 1);
        var feedback = Column(_status, _operation, _scheduleCheck); feedback.Spacing = 2;
        Children.Add(feedback); SetRow(feedback, 2);
        _body = new ContentControl { HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Stretch };
        Children.Add(_body); SetRow(_body, 3);
        Render();
        Loaded += OnLoaded;
        Unloaded += OnUnloaded;
    }

    private async void OnLoaded(object sender, RoutedEventArgs args)
    {
        if (_subscribed) return;
        _subscribed = true;
        _state.Changed += OnStateChanged;
        try { await _session.ActivateAsync(); }
        catch (OperationCanceledException) { }
        catch { ShowLocalFailure(); }
    }

    private void OnUnloaded(object sender, RoutedEventArgs args)
    {
        _state.Changed -= OnStateChanged;
        _subscribed = false;
    }

    private void OnStateChanged(object? sender, bool rebuild)
    {
        void Apply()
        {
            if (_subscribed) { if (rebuild) Render(); else RefreshStatus(); }
        }
        if (DispatcherQueue.HasThreadAccess) Apply();
        else DispatcherQueue.TryEnqueue(Apply);
    }

    private void RefreshStatus()
    {
        lock (_state.Sync)
        {
            _status.Text = _state.Status;
            _operation.Text = _state.OperationResult;
            _scheduleCheck.Text = _state.ScheduleCheckResult;
            _save.IsEnabled = _state.Ready && (!_state.Busy || _state.BackgroundActivity);
            _cancel.IsEnabled = _state.Busy;
            _body.IsEnabled = !_state.Busy || _state.BackgroundActivity;
            // A saved-schedule check updates only read-only results. Editors keep their tree, focus and cursor.
            if (_runOutput is not null && _runOutput.Text != _state.Current.Data.Output)
                _runOutput.Text = _state.Current.Data.Output;
            if (_historyList is not null && !ReferenceEquals(_historySource, _state.Current.Data.History))
            {
                _historySource = _state.Current.Data.History;
                FillHistoryList(_historyList, _historySource, _state.Current.SelectedHistoryId);
            }
        }
    }

    private void Render()
    {
        if (_rendering) return;
        _rendering = true;
        try
        {
            lock (_state.Sync)
            {
                var profile = _state.Current;
                var tabs = Identify(new TabView
                {
                    IsAddTabButtonVisible = false, CanDragTabs = false, CanReorderTabs = false,
                    TabWidthMode = TabViewWidthMode.SizeToContent
                }, _settings ? "settings-tabs" : "digest-tabs", _settings ? "UPCAM settings tabs" : "UPCAM digest tabs");
                void Add(string title, UIElement content) => tabs.TabItems.Add(Identify(new TabViewItem
                { Header = Label(title), Tag = title, IsClosable = false, Content = content,
                    IsEnabled = _state.Ready || (_settings ? title == "Profile" : title == "Overview") },
                    "tab-" + title.ToLowerInvariant().Replace(" & ", "-").Replace(' ', '-'), title));
                if (_settings)
                {
                    Add("Profile", BuildProfile(profile));
                    Add("Accounts", BuildAccounts(profile));
                    Add("Project", BuildProject(profile));
                    Add("Members", BuildMembers(profile));
                    Add("Statuses", BuildStatuses(profile));
                    Add("Schedule", BuildSchedule(profile));
                }
                else
                {
                    Add("Overview", BuildOverview(profile));
                    Add("Run & diagnostics", BuildRun(profile));
                    Add("History", BuildHistory(profile));
                }
                tabs.SelectedIndex = Math.Clamp(_settings ? _state.SettingsTab : _state.DigestTab, 0, tabs.TabItems.Count - 1);
                tabs.SelectionChanged += (_, _) =>
                {
                    lock (_state.Sync)
                    {
                        if (_settings) _state.SettingsTab = tabs.SelectedIndex;
                        else _state.DigestTab = tabs.SelectedIndex;
                    }
                };
                _body.Content = tabs;
            }
            RefreshStatus();
        }
        finally { _rendering = false; }
    }

    private FrameworkElement BuildOverview(ProfileDraft profile)
    {
        var data = profile.Data;
        return LocalScroll(Column(
            Paragraph("Let the digest finish on schedule", 22),
            Paragraph(_state.SavedAutoEnabled ? "Saved automatic sending is enabled." : "Saved automatic sending is disabled."),
            Paragraph($"Project: {data.Text("project_id", "Not configured")}\n{data.Map("owner_names").Count} member mappings · {data.Map("status_names").Count} status mappings"),
            Paragraph(data.ScopeVerified ? "Project read-only verification passed." : "Save the project scope and verify a representative time range."),
            Paragraph($"Morning {data.Text("morning_run_at")} · Afternoon {data.Text("afternoon_run_at")}"),
            Paragraph(UiStrings.T(data.LedgerMessage)),
            Paragraph("Profile: " + profile.Directory),
            Paragraph($"Backend {data.ApplicationVersion} · Rules {data.RulesetRevision}"),
            Paragraph(PluginSession.ActivationNotice),
            Paragraph("Business settings require Save business settings. Drafts survive navigation and profile changes in this session; save before disabling or exiting. Missing mappings and incomplete collections block delivery.")));
    }

    private FrameworkElement BuildRun(ProfileDraft profile)
    {
        var output = Identify(new TextBox
        {
            Header = "日报 / 诊断输出", IsReadOnly = true, AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap, Text = profile.Data.Output, MinHeight = 180,
            HorizontalAlignment = HorizontalAlignment.Stretch, VerticalAlignment = VerticalAlignment.Stretch
        }, "diagnostic-output", "Digest and diagnostic output");
        _runOutput = output;
        ScrollViewer.SetVerticalScrollBarVisibility(output, ScrollBarVisibility.Auto);
        var operations = Toolbar(
            ActionButton("Read-only diagnosis", "diagnose", async () =>
            {
                await _session.CallAsync("diagnose", "diagnose", p => p.Data.WindowPayload(), ApplyDiagnostic, requireSaved: true);
            }),
            ActionButton("Preview fictional sample", "preview-sample", async () => { await _session.PreviewSampleAsync(); }),
            ActionButton("Generate and send digest", "run-manual", async () =>
            {
                await _session.PerformAsync("run-manual", async (worker, p, token) =>
                {
                    JsonObject payload;
                    lock (_state.Sync)
                    {
                        PluginSession.RequireSaved(p);
                        payload = p.Data.WindowPayload();
                        payload["manual_request_id"] = Guid.NewGuid().ToString("D");
                    }
                    JsonObject result = await worker.SendAsync("run_manual", payload, token);
                    JsonObject history = await worker.SendAsync("history", null, token);
                    lock (_state.Sync)
                    {
                        p.Data.Output = Value(result, "markdown");
                        p.Data.History = history["runs"]?.DeepClone().AsArray() ?? new JsonArray();
                    }
                    return "The worker returned the delivery result. Review History before sending again.";
                });
            }));
        var exports = Identify(new Expander
        {
            Header = "诊断导出", HorizontalAlignment = HorizontalAlignment.Stretch,
            Content = Column(
                DraftText(profile, "Absolute local output file (.json)", "diagnostic-export-path", p => p.ExportPath, (p, value) => p.ExportPath = value),
                Paragraph("Workbench API 1 provides no picker window handle. Enter an absolute local file path. A structure export contains version, IDs and field shapes. Detailed export also contains project titles and activity text; choose a private local file."),
                Check("I consent to export project content to this local file.", "confirm-detailed-export", profile.DetailedExportConsent,
                    value => _state.EditDraft(profile, p => p.DetailedExportConsent = value)),
                Toolbar(
                    ActionButton("Export structure", "export-diagnostic", async () =>
                    {
                        await _session.CallAsync("export-diagnostic", "export_diagnostic_to_file", p =>
                        {
                            if (!p.Data.ExportAvailable) throw new InvalidOperationException("Run read-only diagnosis for the current settings and time range first.");
                            return p.Data.DiagnosticExportPayload(ExportTarget(p));
                        }, requireSaved: true);
                    }),
                    ActionButton("Copy diagnosis text", "copy-diagnostic", async () =>
                    {
                        await _session.PerformAsync("copy-diagnostic", async (worker, p, token) =>
                        {
                            JsonObject payload; string text;
                            lock (_state.Sync)
                            {
                                PluginSession.RequireSaved(p);
                                if (!p.Data.ExportAvailable || p.Data.DiagnosticText.Length == 0)
                                    throw new InvalidOperationException("Run read-only diagnosis for the current settings and time range first.");
                                payload = p.Data.WindowPayload(); text = p.Data.DiagnosticText;
                            }
                            await worker.SendAsync("diagnostic_status", payload, token);
                            var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
                            package.SetText(text);
                            var options = new Windows.ApplicationModel.DataTransfer.ClipboardContentOptions { IsAllowedInHistory = false, IsRoamable = false };
                            if (!Windows.ApplicationModel.DataTransfer.Clipboard.SetContentWithOptions(package, options))
                                throw new InvalidOperationException("The clipboard is unavailable. Try the local export instead.");
                            return "Diagnostic text copied without clipboard history or roaming. Check IDs before sharing.";
                        });
                    }),
                    ActionButton("Export detailed project content", "export-detailed-diagnostic", async () =>
                    {
                        await _session.PerformAsync("export-detailed-diagnostic", async (worker, p, token) =>
                        {
                            JsonObject payload;
                            lock (_state.Sync)
                            {
                                PluginSession.RequireSaved(p);
                                if (!p.DetailedExportConsent) throw new InvalidOperationException("Give explicit local project-content export consent first.");
                                payload = p.Data.WindowPayload(); payload["path"] = ExportTarget(p); payload["include_project_content"] = true;
                            }
                            JsonObject result = await worker.SendAsync("export_detailed_diagnostic_to_file", payload, token);
                            lock (_state.Sync) return ApplyDetailedExportResult(p, result);
                        });
                    })))
        }, "diagnostic-export", "Diagnostic export options");
        var top = Column(Paragraph("Range uses this computer's local time; format YYYY-MM-DD HH:MM."),
            WindowInputs(profile), operations, exports);
        return Fill(LocalScroll(top, 310), output);
    }

    private static string ExportTarget(ProfileDraft profile)
    {
        string path = profile.ExportPath.Trim();
        if (!Path.IsPathFullyQualified(path) || path.StartsWith(@"\\", StringComparison.Ordinal) || !path.EndsWith(".json", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("Enter an absolute local .json file path.");
        return Path.GetFullPath(path);
    }

    private static void ApplyDiagnostic(ProfileDraft profile, JsonObject result)
    {
        var data = profile.Data;
        data.Output = Value(result, "message");
        data.ExportAvailable = result["export_available"]?.GetValue<bool>() == true;
        data.DiagnosticFileStem = Value(result, "diagnostic_file_stem");
        if (result["diagnostic_summary"] is JsonObject summary)
            data.Output += $"\n\nUnsupported actions in range: {summary["unknown_events_in_report_window"]}; unknown time: {summary["unknown_events_with_unknown_time"]}. Catalog complete: {summary["catalog_complete"]}.";
        data.DiagnosticText = data.Output;
    }

    private static string ApplyDetailedExportResult(ProfileDraft profile, JsonObject result)
    {
        // Diagnosis can succeed while writing the detailed snapshot is refused. Keep the diagnosis in both cases.
        ApplyDiagnostic(profile, result);
        JsonObject? export = result["detailed_export"] as JsonObject;
        if (export?["saved"] is JsonValue saved && saved.TryGetValue<bool>(out bool confirmed) && confirmed)
            return "详细诊断已保存，包含项目内容并已剔除配置凭据；请仅私下分享用于排查。";

        string? reason = export?["message"] is JsonValue message && message.TryGetValue<string>(out string? text) ? text : null;
        // Only fixed, reviewed worker messages cross into operation status; never copy arbitrary protocol/error text.
        string safeReason = reason switch
        {
            "设置或凭据在采集期间已改变，请重新执行详细诊断；未写入项目内容。" => "设置或凭据在采集期间已改变，请重新执行详细诊断；未写入项目内容。",
            "设置或凭据在准备文件期间已改变，请重新执行详细诊断；未写入项目内容。" => "设置或凭据在准备文件期间已改变，请重新执行详细诊断；未写入项目内容。",
            "保存目录在诊断期间已改变；没有写入详细文件，请重新选择位置。" => "保存目录在诊断期间已改变；没有写入详细文件，请重新选择位置。",
            "详细诊断未能确认保存成功，文件可能已生成；请检查本机保存位置、权限和可用空间。脱敏结构仍可导出。" => "详细诊断未能确认保存成功，文件可能已生成；请检查本机保存位置、权限和可用空间。脱敏结构仍可导出。",
            "无法安全准备详细诊断：数据超过预算或认证信息无法可靠清理；没有保存项目正文。脱敏结构仍可导出。" => "无法安全准备详细诊断：数据超过预算或认证信息无法可靠清理；没有保存项目正文。脱敏结构仍可导出。",
            "采集或诊断未完成，没有写入详细项目数据。" => "采集或诊断未完成，没有写入详细项目数据。",
            _ => "未能确认详细诊断保存成功；请检查本机目标位置、文件名、权限和可用空间后重新导出。诊断结果已保留，不会自动重试。"
        };
        throw new InvalidOperationException(safeReason);
    }

    private FrameworkElement BuildHistory(ProfileDraft profile)
    {
        var list = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single, MinHeight = 100, MaxHeight = 260 }, "history-list", "Local digest run history");
        _historyList = list;
        _historySource = profile.Data.History;
        FillHistoryList(list, _historySource, profile.SelectedHistoryId);
        list.SelectionChanged += (_, _) =>
        {
            if (list.SelectedItem is ListViewItem item && item.Tag is string id)
                _state.EditDraft(profile, p => p.SelectedHistoryId = id);
        };
        var detail = Identify(new TextBox { Header = "选中记录", IsReadOnly = true, AcceptsReturn = true,
            TextWrapping = TextWrapping.Wrap, Text = profile.HistoryDetail, MinHeight = 160 }, "history-detail", "Selected digest run details");
        return LocalScroll(Column(
            Paragraph("Delivery states 'sending' and 'delivery_unknown' require checking the group and local record. Confirmed and uncertain scheduled deliveries are never replayed automatically."),
            Toolbar(
                ActionButton("Refresh history", "refresh-history", async () =>
                {
                    await _session.CallAsync("refresh-history", "history", _ => null,
                        (p, result) => p.Data.History = result["runs"]?.DeepClone().AsArray() ?? new JsonArray());
                }),
                ActionButton("Open selected run", "history-detail-open", async () =>
                {
                    await _session.CallAsync("history-detail", "history_detail", p => new JsonObject { ["run_id"] = p.SelectedHistoryId },
                        (p, result) => p.HistoryDetail = Value(result["run"], "delivery_note") + "\n\n" + Value(result["run"], "digest_markdown"));
                })), list, detail));
    }

    private static void FillHistoryList(ListView list, JsonArray history, string selectedId)
    {
        list.Items.Clear();
        foreach (JsonNode? run in history)
        {
            string id = Value(run, "run_id");
            var item = new ListViewItem
            {
                Tag = id, HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Content = Paragraph($"{Value(run, "created_at_utc")} · {Value(run, "state")}\n{Value(run, "window_start_local")} → {Value(run, "window_end_local")}\n{Value(run, "delivery_note")}")
            };
            list.Items.Add(item);
            if (id == selectedId) list.SelectedItem = item;
        }
    }

    private static T Identify<T>(T control, string id, string name) where T : DependencyObject
    {
        AutomationProperties.SetAutomationId(control, id);
        AutomationProperties.SetName(control, name);
        return control;
    }

    private static TextBlock Paragraph(string text, double size = 14) => new()
    {
        Text = Label(text), FontSize = size, TextWrapping = TextWrapping.Wrap, IsTextSelectionEnabled = true
    };

    private static StackPanel Column(params UIElement[] children)
    {
        var panel = new StackPanel { Spacing = 10, Padding = new Thickness(0, 8, 6, 8) };
        foreach (UIElement child in children) panel.Children.Add(child);
        return panel;
    }

    private static NativeWrapPanel Toolbar(params UIElement[] controls)
    {
        var panel = new NativeWrapPanel();
        foreach (UIElement control in controls) panel.Children.Add(control);
        return panel;
    }

    private static ScrollViewer LocalScroll(UIElement content, double? maxHeight = null)
    {
        var scroll = new ScrollViewer { Content = content, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto, HorizontalContentAlignment = HorizontalAlignment.Stretch };
        if (maxHeight is double height) scroll.MaxHeight = height;
        return scroll;
    }

    private static Grid Fill(FrameworkElement first, FrameworkElement second)
    {
        var layout = new Grid { RowSpacing = 8 };
        layout.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        layout.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        layout.Children.Add(first); layout.Children.Add(second); SetRow(second, 1);
        return layout;
    }

    private Button ActionButton(string title, string id, Func<Task> action)
    {
        var button = Identify(new Button { Content = Label(title), HorizontalAlignment = HorizontalAlignment.Left }, id, title);
        button.Click += async (_, _) =>
        {
            try { await action(); }
            catch (OperationCanceledException) { }
            catch { ShowLocalFailure(); }
        };
        return button;
    }

    private void ShowLocalFailure()
    {
        lock (_state.Sync)
            _state.OperationResult = $"{++_state.OperationRevision} · native-interface · failed · The local interface operation did not complete. No request was replayed.";
        _state.Notify(false);
    }

    private TextBox SettingText(ProfileDraft profile, string label, string key, string id, bool multiline = false)
    {
        var settingsAtCreation = profile.Data.Settings;
        var input = Identify(new TextBox { Header = Label(label), Text = profile.Data.Text(key), AcceptsReturn = multiline,
            TextWrapping = multiline ? TextWrapping.Wrap : TextWrapping.NoWrap }, id, label);
        if (multiline) { input.MinHeight = 96; input.MaxHeight = 180; }
        input.TextChanging += (_, _) => _state.Edit(profile, data =>
        {
            if (ReferenceEquals(settingsAtCreation, data.Settings)) data.Set(key, input.Text);
        });
        return input;
    }

    private TextBox DraftText(ProfileDraft profile, string label, string id, Func<ProfileDraft, string> read,
        Action<ProfileDraft, string> write, bool multiline = false)
    {
        var input = Identify(new TextBox { Header = Label(label), Text = read(profile), AcceptsReturn = multiline,
            TextWrapping = multiline ? TextWrapping.Wrap : TextWrapping.NoWrap }, id, label);
        if (multiline) { input.MinHeight = 100; input.MaxHeight = 200; }
        input.TextChanging += (_, _) => _state.EditDraft(profile, p => write(p, input.Text));
        return input;
    }

    private static CheckBox Check(string label, string id, bool value, Action<bool> changed)
    {
        var box = Identify(new CheckBox { Content = Label(label), IsChecked = value }, id, label);
        box.Checked += (_, _) => changed(true);
        box.Unchecked += (_, _) => changed(false);
        return box;
    }

    private FrameworkElement WindowInputs(ProfileDraft profile)
    {
        var first = Identify(new TextBox { Header = "采集开始", Text = profile.Data.ManualStart, PlaceholderText = "YYYY-MM-DD HH:MM" }, "window-start", "Collection range start local time");
        var last = Identify(new TextBox { Header = "采集结束", Text = profile.Data.ManualEnd, PlaceholderText = "YYYY-MM-DD HH:MM" }, "window-end", "Collection range end local time");
        first.TextChanging += (_, _) => _state.Edit(profile, data => data.ManualStart = first.Text);
        last.TextChanging += (_, _) => _state.Edit(profile, data => data.ManualEnd = last.Text);
        return Column(first, last);
    }

    private static string Value(JsonNode? node, string key, string fallback = "") => PluginSession.JsonText(node, key, fallback);
}
