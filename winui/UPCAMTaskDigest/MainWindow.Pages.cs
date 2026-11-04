using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using System.Text.Json.Nodes;
using Windows.Storage;
using Windows.Storage.Pickers;

namespace Upcam.Desktop;

public sealed partial class MainWindow
{
    private FrameworkElement BuildHome()
    {
        var hero = Column(
            Paragraph("让日报按时完成", size: 28),
            Paragraph("先在本机收集和校验 Teambition 数据，只将摘要所需内容交给 OpenAI，再发送到配置的钉钉群。", true),
            Paragraph(_savedAuto ? "● 自动运行已开启" : "○ 自动运行尚未开启", size: 18),
            Toolbar(NavigationButton("运行与诊断", "run"), NavigationButton("配置项目范围", "project")));
        var summary = Pair(
            Card(Column(Paragraph("项目与映射", size: 18),
                Paragraph(string.IsNullOrEmpty(_state.Text("project_id")) ? "尚未选择项目" : _state.Text("project_id"), true),
                Paragraph($"{_state.Map("owner_names").Count} 位成员 · {_state.Map("status_names").Count} 个状态"),
                Paragraph(_state.ScopeVerified ? "项目范围只读核验已通过" : "保存配置以自动核验当前项目范围", true))),
            Card(Column(Paragraph("下一步与运行时间", size: 18),
                Paragraph($"上午 {_state.Text("morning_run_at")} · 下午 {_state.Text("afternoon_run_at")}"),
                Paragraph(_state.LedgerMessage, true),
                NavigationButton("管理自动运行", "schedule"))));
        return LocalScroll(Column(Card(hero), summary,
            Card(Column(Paragraph("本机数据", size: 18), Paragraph(_state.DataDirectory, true),
                Paragraph("关闭窗口后留在系统托盘；从托盘菜单选择“退出应用”才会停止。电脑需保持开机、账户已登录、联网且未休眠。", true),
                Paragraph($"WinUI 3 · v{_state.ApplicationVersion} · 规则 {_state.RulesetRevision}", true)))));
    }

    private FrameworkElement BuildAccounts()
    {
        var tabs = Identify(new TabView
        {
            IsAddTabButtonVisible = false, CanDragTabs = false, CanReorderTabs = false,
            TabWidthMode = TabViewWidthMode.Equal
        }, "account-tabs");
        tabs.TabItems.Add(new TabViewItem
        {
            Header = "Teambition", IsClosable = false,
            Content = LocalScroll(Column(Card(Column(
                Pair(SettingText("Tenant / Organization ID", "tenant_id", "tenant-id"),
                     SettingText("Project ID", "project_id", "project-id")),
                SecretInput("Teambition App ID", "teambition_app_id"),
                SecretInput("Teambition App Secret", "teambition_app_secret"),
                Paragraph("输入框留空会保留已保存的密钥。新值只在保存后由当前 Windows 用户 DPAPI 加密；不显示已保存的原值。", true),
                ActionButton("只读测试连接", "test-teambition", async () =>
                {
                    await EnsureSavedAsync();
                    var result = await Worker.SendAsync("test_teambition");
                    SetStatus(Value(result, "status"));
                })))))
        });
        tabs.TabItems.Add(new TabViewItem
        {
            Header = "钉钉", IsClosable = false,
            Content = LocalScroll(Column(Card(Column(
                SecretInput("机器人 Webhook（含 access_token）", "dingtalk_webhook"),
                SecretInput("机器人签名密钥", "dingtalk_signing_secret"),
                Paragraph("这里的检查只验证本机 URL 与签名配置，不向群里发送测试消息。", true),
                ActionButton("检查本机机器人配置", "check-dingtalk", async () =>
                {
                    await EnsureSavedAsync();
                    var result = await Worker.SendAsync("validate_dingtalk_config");
                    SetStatus(Value(result, "status"));
                })))))
        });
        tabs.TabItems.Add(new TabViewItem { Header = "OpenAI", IsClosable = false, Content = BuildOpenAi() });
        return tabs;
    }

    private FrameworkElement SecretInput(string label, string key)
    {
        bool saved = _state.SecretPresence[key]?.GetValue<bool>() == true;
        var input = Identify(new PasswordBox
        {
            Header = label, Password = _state.SecretDrafts.GetValueOrDefault(key, ""),
            PlaceholderText = saved ? "本机已加密保存 · 留空保留" : "本机未保存",
            PasswordRevealMode = PasswordRevealMode.Peek
        }, "secret-" + key);
        input.PasswordChanged += (_, _) => _state.SetSecretDraft(key, input.Password);
        return input;
    }

    private FrameworkElement BuildOpenAi()
    {
        var model = Identify(new ComboBox { Header = "API 模型", HorizontalAlignment = HorizontalAlignment.Stretch }, "openai-model");
        string current = _state.Text("openai_model", "gpt-6-luna");
        var ids = _state.Models.Select(x => Value(x, "id")).Append(current).Distinct().OrderBy(x => x).ToArray();
        foreach (string id in ids) model.Items.Add(id);
        model.SelectedItem = current;
        var effort = Identify(new ComboBox { Header = "推理程度", PlaceholderText = "请选择已核验的选项", HorizontalAlignment = HorizontalAlignment.Stretch }, "reasoning-effort");
        var tier = Identify(new ComboBox { Header = "服务等级", PlaceholderText = "请选择已核验的选项", HorizontalAlignment = HorizontalAlignment.Stretch }, "service-tier");
        void SetOptions()
        {
            string selected = model.SelectedItem as string ?? current;
            effort.Items.Clear();
            effort.Items.Add("default");
            if (_state.Efforts[selected] is JsonArray values)
                foreach (JsonNode? value in values) effort.Items.Add(value!.GetValue<string>());
            string chosen = _state.Text("openai_reasoning_effort", "default");
            effort.SelectedItem = effort.Items.Contains(chosen) ? chosen : null;
            tier.Items.Clear();
            tier.Items.Add("default");
            if (_state.FastModels.Any(x => x?.GetValue<string>() == selected)) tier.Items.Add("fast");
            string chosenTier = _state.Text("openai_service_tier", "default");
            tier.SelectedItem = tier.Items.Contains(chosenTier) ? chosenTier : null;
        }
        SetOptions();
        model.SelectionChanged += (_, _) =>
        {
            if (model.SelectedItem is string selected)
            {
                _state.Set("openai_model", selected);
                SetOptions();
                _state.Set("openai_reasoning_effort", effort.SelectedItem as string ?? "default");
                _state.Set("openai_service_tier", tier.SelectedItem as string ?? "default");
            }
        };
        effort.SelectionChanged += (_, _) =>
        {
            if (effort.SelectedItem is string selected) _state.Set("openai_reasoning_effort", selected);
        };
        tier.SelectionChanged += (_, _) =>
        {
            if (tier.SelectedItem is string selected) _state.Set("openai_service_tier", selected);
        };
        return LocalScroll(Column(Card(Column(
            SecretInput("OpenAI API Key", "openai_api_key"),
            Paragraph(_state.ModelCacheStatus, true),
            ActionButton("刷新当前 Key 可用模型", "refresh-models", async () =>
            {
                await EnsureSavedAsync();
                _state.ApplyModels(await Worker.SendAsync("refresh_models"));
                Navigate("accounts");
                SetStatus("模型缓存已刷新；启动时只读取此缓存，直到你下次点击刷新。");
            }),
            model, Pair(effort, tier),
            Paragraph("default 为模型默认推理程度 / Standard 标准服务。fast 可能增加费用，且不保证更快；账户、项目或区域未必可用。只有此版本已核验的模型会提供 Fast。", true),
            Paragraph("模型列表来自当前 API Key 的 OpenAI API，可调用权限与 ChatGPT 产品中的模型选择不同。模型列表本身不提供推理程度能力；未知模型只提供 default。", true))),
            ActionButton("清除已保存的服务密钥", "clear-credentials", async () =>
            {
                if (await ShowMessageAsync("清除服务密钥", "清除 Teambition、钉钉与 OpenAI 凭据并关闭自动运行；防重复账本身份会保留。", "清除")
                    != ContentDialogResult.Primary) return;
                _state.Apply(await Worker.SendAsync("clear_credentials"));
                _savedAuto = false;
                Navigate("accounts");
                SetStatus("服务密钥已清除；自动运行已关闭。");
            })));
    }

    private FrameworkElement BuildProject()
    {
        var archived = Identify(new CheckBox { Content = "纳入已归档任务", IsChecked = _state.Flag("include_archived") }, "include-archived");
        archived.Click += (_, _) => _state.Set("include_archived", archived.IsChecked == true);
        var scope = Card(Column(
            Paragraph($"项目：{_state.Text("project_id", "尚未配置")}", size: 18),
            Paragraph("推荐范围：指定 Project ID 下，当前真实 updated 时间落在日报半开窗口内的任务；默认不含归档，不添加其他条件。它不等同于网页“昨天/今天动态”视图。", true),
            Paragraph("保存配置即采用当前项目、归档选项和筛选条件。范围或 Teambition 凭据变化时，会使用下面的时间区间自动只读核验；核验未通过时仍保存编辑，自动运行会关闭。", true),
            archived, BuildFilterEditor()));
        var probe = Card(Column(Paragraph("只读核验与映射发现", size: 18), TimeInputs(),
            Paragraph("选择确有任务更新和动态的区间。保存时会校验完整翻页、来源与字段样本，并发现映射 ID。成员姓名和状态名称仍需核对。", true),
            ActionButton("只读验证项目并发现映射 ID", "probe-project", async () =>
            {
                if (_state.SecretDrafts.Any(x => !string.IsNullOrEmpty(x.Value))) await SaveCoreAsync();
                var result = await Worker.SendAsync("probe_project", _state.WindowPayload(scope: true));
                _state.ApplyProbe(result);
                _state.Output = $"完整读取 {result["task_count"]} 个任务、{result["activity_count"]} 条动态。核对成员/状态映射后保存配置，保存时会采用当前项目范围。";
                Navigate("project");
                SetStatus(_state.Output);
            }, accent: true)));
        return LocalScroll(Column(scope, probe));
    }

    private FrameworkElement BuildFilterEditor()
    {
        var filters = new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 140 };
        foreach (var item in _state.Filters)
            filters.Items.Add(new ListViewItem { Tag = item.Key, Content = Paragraph($"{item.Key} = {item.Value?.GetValue<string>()}") });
        var field = new ComboBox { Header = "稳定字段", HorizontalAlignment = HorizontalAlignment.Stretch };
        foreach (var item in new Dictionary<string, string>
        {
            ["executorId"] = "执行人 ID", ["tasklistId"] = "任务列表 ID", ["stageId"] = "阶段 ID",
            ["tfsId"] = "状态 ID", ["scenariofieldconfigId"] = "自定义字段配置 ID",
            ["visible"] = "可见性", ["parentTaskId"] = "父任务 ID"
        }) field.Items.Add(new ComboBoxItem { Tag = item.Key, Content = item.Value });
        field.SelectedIndex = 0;
        var value = new TextBox { Header = "字段值 / API ID" };
        return new Expander
        {
            Header = $"额外项目筛选（可选，当前 {_state.Filters.Count} 项）",
            HorizontalAlignment = HorizontalAlignment.Stretch,
            Content = Column(Pair(field, value), filters, Toolbar(
                ActionButton("添加 / 更新", "filter-put", async () =>
                {
                    var result = await Worker.SendAsync("update_view_filter", new JsonObject
                    {
                        ["action"] = "put", ["field"] = (field.SelectedItem as ComboBoxItem)?.Tag as string,
                        ["value"] = value.Text, ["filters"] = _state.Filters.DeepClone()
                    });
                    _state.SetFilters(result["filters"]!.AsObject()); Navigate("project");
                    SetStatus("筛选条件已编辑；保存配置时会自动只读核验当前范围。");
                }),
                ActionButton("删除选中条件", "filter-remove", async () =>
                {
                    if (filters.SelectedItem is not ListViewItem selected) return;
                    var result = await Worker.SendAsync("update_view_filter", new JsonObject
                    {
                        ["action"] = "remove", ["field"] = selected.Tag as string,
                        ["filters"] = _state.Filters.DeepClone()
                    });
                    _state.SetFilters(result["filters"]!.AsObject()); Navigate("project");
                })))
        };
    }

    private FrameworkElement BuildMembers()
    {
        var list = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single }, "member-list");
        foreach (string id in _state.Map("owner_names").Select(p => p.Key).Concat(_state.CandidateOwners).Distinct().Order())
        {
            string name = _state.Map("owner_names")[id]?.GetValue<string>() ?? "待核对姓名";
            list.Items.Add(new ListViewItem { Tag = id, HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Content = Pair(Paragraph(id, true), Paragraph(name)) });
        }
        return FillLayout(Column(
            Paragraph($"已保存或待保存 {_state.Map("owner_names").Count} 条映射。当前批准的接口只能发现样本 ID，不是完整成员名册；员工编号不能代替 API ID。", true),
            Toolbar(
                ActionButton("添加成员", "member-add", () => EditMemberAsync(null)),
                ActionButton("编辑选中成员", "member-edit", () => EditMemberAsync((list.SelectedItem as ListViewItem)?.Tag as string)),
                ActionButton("批量导入", "member-import", ImportMembersAsync, accent: true),
                ActionButton("删除选中映射", "member-delete", () =>
                {
                    if (list.SelectedItem is ListViewItem selected && selected.Tag is string id)
                    { var map = (JsonObject)_state.Map("owner_names").DeepClone(); map.Remove(id); _state.Set("owner_names", map); Navigate("members"); }
                    return Task.CompletedTask;
                }))), Card(list));
    }

    private async Task EditMemberAsync(string? id)
    {
        var inputId = new TextBox { Header = "API 成员 ID", Text = id ?? "", IsReadOnly = id is not null };
        var name = new TextBox { Header = "姓名", Text = id is null ? "" : _state.Map("owner_names")[id]?.GetValue<string>() ?? "" };
        var dialog = EditorDialog("成员映射", Column(inputId, name));
        if (await ShowDialogAsync(dialog) != ContentDialogResult.Primary) return;
        var mapping = (JsonObject)_state.Map("owner_names").DeepClone();
        mapping[inputId.Text.Trim()] = name.Text;
        var result = await Worker.SendAsync("update_owner_mapping", new JsonObject { ["mappings"] = mapping });
        _state.Set("owner_names", result["mappings"]!.DeepClone());
        Navigate("members"); SetStatus("成员映射已编辑；保存本机配置后生效。");
    }

    private async Task ImportMembersAsync()
    {
        var input = Identify(new TextBox
        {
            AcceptsReturn = true, TextWrapping = TextWrapping.NoWrap, Height = 210,
            PlaceholderText = "{\"demo-member-01\":\"虚构成员甲\"}\n也可每行：成员ID<实际制表符>姓名"
        }, "member-import-text");
        ScrollViewer.SetVerticalScrollBarVisibility(input, ScrollBarVisibility.Auto);
        var dialog = EditorDialog("批量导入成员映射", Column(
            Paragraph("粘贴 JSON 对象或 ID 与姓名之间有一个制表符的多行文本。冲突和格式错误会整批拒绝。", true), input), "导入");
        if (await ShowDialogAsync(dialog) != ContentDialogResult.Primary) return;
        var result = await Worker.SendAsync("import_owner_mappings", new JsonObject
        { ["source"] = input.Text, ["existing"] = _state.Map("owner_names").DeepClone() });
        _state.Set("owner_names", result["mappings"]!.DeepClone());
        Navigate("members"); SetStatus($"已导入 {result["imported_count"]} 条映射；保存本机配置后生效。");
    }

    private FrameworkElement BuildStatuses()
    {
        var list = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single }, "status-list");
        var resolved = (_state.Settings["resolved_status_ids"] as JsonArray)?.Select(x => x!.GetValue<string>()).ToHashSet() ?? new();
        foreach (string id in _state.Map("status_names").Select(p => p.Key).Concat(_state.CandidateStatuses).Distinct().Order())
        {
            string name = _state.Map("status_names")[id]?.GetValue<string>() ?? "待核对名称";
            string suggestion = _state.StatusSuggestions.GetValueOrDefault(id, "");
            var row = new Grid { ColumnSpacing = 16, Padding = new Thickness(0, 10, 0, 10) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(2, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.Children.Add(Column(Paragraph(id, true), Paragraph(name, size: 16)));
            var hint = Paragraph(string.IsNullOrEmpty(suggestion) ? "" : "样本建议：" + suggestion, true);
            row.Children.Add(hint); Grid.SetColumn(hint, 1);
            var done = Paragraph(resolved.Contains(id) ? "✓ 已解决" : "未解决", true);
            row.Children.Add(done); Grid.SetColumn(done, 2);
            list.Items.Add(new ListViewItem { Tag = id, HorizontalContentAlignment = HorizontalAlignment.Stretch, Content = row });
        }
        return FillLayout(Column(
            Paragraph("状态名称必须与真实项目一致。样本动态返回的名称只作建议，不能凭 ID 猜名称；请逐项确认已解决状态。", true),
            Toolbar(
                ActionButton("添加状态", "status-add", () => EditStatusAsync(null)),
                ActionButton("编辑选中状态", "status-edit", () => EditStatusAsync((list.SelectedItem as ListViewItem)?.Tag as string), accent: true),
                ActionButton("删除选中映射", "status-delete", () =>
                {
                    if (list.SelectedItem is ListViewItem selected && selected.Tag is string id)
                    {
                        var map = (JsonObject)_state.Map("status_names").DeepClone(); map.Remove(id);
                        _state.Set("status_names", map);
                        _state.Set("resolved_status_ids", new JsonArray(resolved.Where(x => x != id).Select(x => (JsonNode?)JsonValue.Create(x)).ToArray()));
                        Navigate("statuses");
                    }
                    return Task.CompletedTask;
                }))), Card(list));
    }

    private async Task EditStatusAsync(string? id)
    {
        var inputId = Identify(new TextBox { Header = "API 状态 ID", Text = id ?? "", IsReadOnly = id is not null }, "status-id-input");
        var name = Identify(new TextBox { Header = "已核对名称", Text = id is null ? "" : _state.Map("status_names")[id]?.GetValue<string>() ?? "" }, "status-name-input");
        var resolved = (_state.Settings["resolved_status_ids"] as JsonArray)?.Select(x => x!.GetValue<string>()).ToHashSet() ?? new();
        var done = new CheckBox { Content = "此状态代表已解决", IsChecked = id is not null && resolved.Contains(id) };
        string suggestion = id is null ? "" : _state.StatusSuggestions.GetValueOrDefault(id, "");
        var dialog = EditorDialog("状态映射", Column(inputId, name, Paragraph("样本名称建议：" + (suggestion.Length == 0 ? "无" : suggestion), true), done));
        if (await ShowDialogAsync(dialog) != ContentDialogResult.Primary) return;
        string key = inputId.Text.Trim();
        var mapping = (JsonObject)_state.Map("status_names").DeepClone(); mapping[key] = name.Text;
        if (done.IsChecked == true) resolved.Add(key); else resolved.Remove(key);
        var result = await Worker.SendAsync("update_status_mapping", new JsonObject
        { ["mappings"] = mapping, ["resolved_status_ids"] = new JsonArray(resolved.Select(x => (JsonNode?)JsonValue.Create(x)).ToArray()) });
        _state.Set("status_names", result["mappings"]!.DeepClone());
        _state.Set("resolved_status_ids", result["resolved_status_ids"]!.DeepClone());
        Navigate("statuses"); SetStatus("状态映射已编辑；保存本机配置后生效。");
    }

    private ContentDialog EditorDialog(string title, UIElement content, string primary = "加入配置") => new()
    {
        XamlRoot = RootGrid.XamlRoot, RequestedTheme = ElementTheme.Dark, Title = title,
        PrimaryButtonText = primary, CloseButtonText = "取消", DefaultButton = ContentDialogButton.Primary,
        Content = new ScrollViewer { Content = content, MaxHeight = 420 }
    };

    private FrameworkElement BuildSchedule()
    {
        var limit = Identify(new NumberBox
        {
            Header = "单次采集任务上限（1–700）", Minimum = 1, Maximum = 700,
            Value = _state.Number("max_tasks_per_run", 300), SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact
        }, "task-limit");
        limit.ValueChanged += (_, _) =>
        {
            if (!double.IsNaN(limit.Value)) _state.Set("max_tasks_per_run", (int)limit.Value);
        };
        FrameworkElement Slot(string label, string prefix) => Card(Column(Paragraph(label, size: 18),
            SettingText("运行时间", prefix + "_run_at", prefix + "-run-at", "HH:MM"),
            Pair(SettingText(prefix == "morning" ? "采集开始（前一日）" : "采集开始", prefix + "_start_at", prefix + "-start-at", "HH:MM"),
                 SettingText("采集结束（当日，不含）", prefix + "_end_at", prefix + "-end-at", "HH:MM"))));
        return LocalScroll(Column(
            Card(Column(Toggle("应用运行期间自动采集、校验、摘要并发送", "auto_send_enabled", "auto-run"),
                Toggle("当前 Windows 用户登录后启动，并以系统托盘运行", "start_with_windows", "startup"),
                Paragraph("保存后生效。电脑关机、休眠、未登录或断网时无法执行；重开后只补跑最近 24 小时内、自动执行启用期间且没有本机记录的时段。", true))),
            Pair(Slot("上午日报", "morning"), Slot("下午日报", "afternoon")),
            Card(Column(limit, Paragraph("超出任务、分页、请求、响应体或时间预算时会阻断后续摘要与发送；不会把不完整采集当成没有动态。", true)))));
    }

    private FrameworkElement BuildRun()
    {
        var output = Identify(new TextBox
        {
            IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            HorizontalAlignment = HorizontalAlignment.Stretch, VerticalAlignment = VerticalAlignment.Stretch,
            Text = _state.Output
        }, "diagnostic-output");
        ScrollViewer.SetVerticalScrollBarVisibility(output, ScrollBarVisibility.Auto);
        var export = ActionButton("导出脱敏结构", "export-diagnostic", ExportDiagnosticAsync);
        export.IsEnabled = _state.ExportAvailable;
        var copy = ActionButton("复制诊断文字", "copy-diagnostic", CopyDiagnosticAsync);
        copy.IsEnabled = _state.ExportAvailable && !string.IsNullOrEmpty(_state.DiagnosticText);
        var toolbar = Toolbar(
            ActionButton("只读诊断", "diagnose", async () =>
            {
                await EnsureSavedAsync();
                _state.ExportAvailable = false;
                _state.DiagnosticFileStem = "";
                _state.DiagnosticText = "";
                Navigate("run");
                var result = await Worker.SendAsync("diagnose", _state.WindowPayload());
                _state.Output = Value(result, "message");
                _state.ExportAvailable = result["export_available"]?.GetValue<bool>() == true;
                _state.DiagnosticFileStem = Value(result, "diagnostic_file_stem");
                if (result["diagnostic"] is JsonObject metadata)
                    _state.Output += $"\n\n诊断 v{Value(metadata, "application_version")} · UTC {Value(metadata, "created_at_utc")}";
                _state.DiagnosticText = _state.Output;
                Navigate("run"); SetStatus(Value(result, "safety", "未调用 OpenAI；未发送钉钉。"));
            }, accent: true),
            ActionButton("生成并自动发送", "run-manual", async () =>
            {
                await EnsureSavedAsync();
                var result = await Worker.SendAsync("run_manual", _state.WindowPayload());
                _state.Output = Value(result, "markdown");
                await RefreshHistoryAsync(); Navigate("run");
                SetStatus("日报已发送并收到钉钉确认。运行记录：" + Value(result, "run_id"));
            }),
            ActionButton("预览虚构样例", "preview-sample", async () =>
            {
                var result = await Worker.SendAsync("preview_sample", _state.WindowPayload());
                _state.Output = Value(result, "markdown"); Navigate("run"); SetStatus(Value(result, "status"));
            }), export, copy);
        var range = new Grid { ColumnSpacing = 12 };
        range.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        range.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var details = Column(Paragraph("本机采集范围 · 结束不含", true, 12),
            Paragraph($"开始  {_state.ManualStart}\n结束  {_state.ManualEnd}", true));
        details.Spacing = 4;
        range.Children.Add(details);
        var editWindow = ActionButton("修改时间范围", "edit-window", EditWindowAsync);
        editWindow.VerticalAlignment = VerticalAlignment.Center;
        range.Children.Add(editWindow); Grid.SetColumn(editWindow, 1);
        var rangeCard = (Border)Card(range);
        rangeCard.Padding = new Thickness(12);
        var header = Column(rangeCard, toolbar);
        header.Spacing = 8;
        return FillLayout(header, output);
    }

    private async Task EditWindowAsync()
    {
        var start = Identify(new TextBox { Header = "开始（本机时间）", Text = _state.ManualStart }, "edit-window-start");
        var end = Identify(new TextBox { Header = "结束（不含，本机时间）", Text = _state.ManualEnd }, "edit-window-end");
        var dialog = EditorDialog("修改采集时间", Column(start, end,
            Paragraph("格式为 YYYY-MM-DD HH:MM。只读诊断不会调用模型或发送；正式运行会按该区间自动发送。", true)), "应用");
        if (await ShowDialogAsync(dialog) != ContentDialogResult.Primary) return;
        if (!DateTime.TryParseExact(start.Text.Trim(), "yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.None, out var first) ||
            !DateTime.TryParseExact(end.Text.Trim(), "yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.None, out var last) || last <= first)
            throw new InvalidOperationException("时间范围无效，请使用 YYYY-MM-DD HH:MM，且结束晚于开始。");
        _state.ManualStart = start.Text.Trim();
        _state.ManualEnd = end.Text.Trim();
        Navigate("run");
    }

    private async Task ExportDiagnosticAsync()
    {
        if (!_state.ExportAvailable || string.IsNullOrEmpty(_state.DiagnosticFileStem))
            throw new InvalidOperationException("请先针对当前设置和时间范围重新运行只读诊断。");
        var picker = new FileSavePicker { SuggestedFileName = _state.DiagnosticFileStem };
        picker.FileTypeChoices.Add("JSON", new List<string> { ".json" });
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        StorageFile? file = await picker.PickSaveFileAsync();
        if (file is null) return;
        await Worker.SendAsync("export_diagnostic_to_file", _state.DiagnosticExportPayload(file.Path));
        SetStatus("已导出版本、时间窗、字段结构及任务/动态/缺失成员 ID；不含标题、姓名、评论正文或凭据。分享前请检查 ID。");
    }

    private async Task CopyDiagnosticAsync()
    {
        if (!_state.ExportAvailable || string.IsNullOrEmpty(_state.DiagnosticText))
            throw new InvalidOperationException("请针对当前设置和时间范围重新运行只读诊断。");
        await Worker.SendAsync("diagnostic_status", _state.WindowPayload());
        var package = new Windows.ApplicationModel.DataTransfer.DataPackage();
        package.SetText(_state.DiagnosticText);
        if (!Windows.ApplicationModel.DataTransfer.Clipboard.SetContentWithOptions(package, DiagnosticClipboardOptions()))
            throw new InvalidOperationException("剪贴板暂时不可用，请重试复制或导出脱敏结构。");
        SetStatus("已复制本机诊断文字，含动作类型与任务/成员 ID，不含评论正文或凭据。分享前请检查内容。");
    }

    private static Windows.ApplicationModel.DataTransfer.ClipboardContentOptions DiagnosticClipboardOptions() =>
        new() { IsAllowedInHistory = false, IsRoamable = false };

    private FrameworkElement BuildHistory()
    {
        var list = Identify(new ListView { IsItemClickEnabled = true, SelectionMode = ListViewSelectionMode.Single }, "history-list");
        foreach (JsonNode? row in _state.History)
        {
            string state = Value(row, "state");
            string label = state switch
            {
                "sent" => "已确认发送", "delivery_unknown" => "结果不明 · 阻止重试",
                "blocked" => "校验阻断", "sending" => "发送中", "preview" => "虚构样例", _ => state
            };
            list.Items.Add(new ListViewItem
            {
                Tag = Value(row, "run_id"), HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Content = Column(Pair(Paragraph(Value(row, "created_at_utc"), true), Paragraph(label)),
                    Paragraph($"{Value(row, "window_start_local")} → {Value(row, "window_end_local")}"),
                    new TextBlock { Text = Value(row, "delivery_note", "点击查看日报或诊断说明"), TextTrimming = TextTrimming.CharacterEllipsis, MaxLines = 1 })
            });
        }
        list.ItemClick += async (_, args) =>
        {
            if (args.ClickedItem is not ListViewItem item || item.Tag is not string id || _busy) return;
            await RunOperationAsync("正在读取本机记录…", async () =>
            {
                var result = await Worker.SendAsync("history_detail", new JsonObject { ["run_id"] = id });
                var row = result["run"];
                await ShowMessageAsync("运行记录", Value(row, "delivery_note") + "\n\n" + Value(row, "digest_markdown"));
            });
        };
        return FillLayout(Toolbar(ActionButton("刷新运行记录", "refresh-history", async () =>
        { await RefreshHistoryAsync(); SetStatus("运行记录已刷新。"); }),
            Paragraph("发送结果不明或已确认的记录不会自动重发。", true)), Card(list));
    }
}
