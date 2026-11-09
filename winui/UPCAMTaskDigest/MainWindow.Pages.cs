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
            Paragraph(UiStrings.T("让日报按时完成"), size: 28),
            Paragraph(UiStrings.T("先在本机收集和校验 Teambition 数据，只将摘要所需内容交给 OpenAI，再发送到配置的钉钉群。"), true),
            Paragraph(UiStrings.T(_savedAuto ? "● 自动运行已开启" : "○ 自动运行尚未开启"), size: 18),
            Toolbar(NavigationButton("运行与诊断", "run"), NavigationButton("配置项目范围", "project")));
        var summary = Pair(
            Card(Column(Paragraph(UiStrings.T("项目与映射"), size: 18),
                Paragraph(string.IsNullOrEmpty(_state.Text("project_id")) ? UiStrings.T("尚未选择项目") : _state.Text("project_id"), true),
                Paragraph(UiStrings.F("{0} 位成员 · {1} 个状态", _state.Map("owner_names").Count, _state.Map("status_names").Count)),
                Paragraph(UiStrings.T(_state.ScopeVerified ? "项目范围只读核验已通过" : "保存配置以自动核验当前项目范围"), true))),
            Card(Column(Paragraph(UiStrings.T("下一步与运行时间"), size: 18),
                Paragraph(UiStrings.F("上午 {0} · 下午 {1}", _state.Text("morning_run_at"), _state.Text("afternoon_run_at"))),
                Paragraph(UiStrings.T(_state.LedgerMessage), true),
                NavigationButton("管理自动运行", "schedule"))));
        return LocalScroll(Column(Card(hero), summary,
            Card(Column(Paragraph(UiStrings.T("本机数据"), size: 18), Paragraph(_state.DataDirectory, true),
                Paragraph(UiStrings.T("窗口关闭行为和托盘选项在常规页设置。电脑需保持开机、账户已登录、联网且未休眠。"), true),
                Paragraph(UiStrings.F("WinUI 3 · v{0} · 规则 {1}", _state.ApplicationVersion, _state.RulesetRevision), true)))));
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
            Header = UiStrings.T("Teambition"), IsClosable = false,
            Content = LocalScroll(Column(Card(Column(
                Pair(SettingText("Tenant / Organization ID", "tenant_id", "tenant-id"),
                     SettingText("Project ID", "project_id", "project-id")),
                SecretInput("Teambition App ID", "teambition_app_id"),
                SecretInput("Teambition App Secret", "teambition_app_secret"),
                Paragraph(UiStrings.T("输入框留空会保留已保存的密钥。新值只在保存后由当前 Windows 用户 DPAPI 加密；不显示已保存的原值。"), true),
                ActionButton("只读测试连接", "test-teambition", async () =>
                {
                    await EnsureSavedAsync();
                    var result = await Worker.SendAsync("test_teambition");
                    SetStatus(UiStrings.F("Teambition 只读查询成功；读取 {0} 个任务。", result["records"]));
                })))))
        });
        tabs.TabItems.Add(new TabViewItem
        {
            Header = UiStrings.T("钉钉"), IsClosable = false,
            Content = BuildDingTalk()
        });
        tabs.TabItems.Add(new TabViewItem { Header = UiStrings.T("OpenAI"), IsClosable = false, Content = BuildOpenAi() });
        return tabs;
    }

    private FrameworkElement BuildDingTalk()
    {
        var mode = Identify(new ComboBox { Header = UiStrings.T("艾特标识类型"), HorizontalAlignment = HorizontalAlignment.Stretch },
            "dingtalk-mention-mode");
        mode.Items.Add(UiStrings.T("手机号"));
        mode.Items.Add(UiStrings.T("钉钉 userId"));
        mode.SelectedIndex = _state.Text("dingtalk_mention_mode", "mobile") == "user_id" ? 1 : 0;
        mode.SelectionChanged += (_, _) => _state.Set("dingtalk_mention_mode", mode.SelectedIndex == 1 ? "user_id" : "mobile");
        var targets = SettingText("艾特指定成员（可选，最多 50 人）", "dingtalk_mentions", "dingtalk-mentions", "留空不艾特；每行一个标识，不加 @", multiline: true);
        targets.Height = 112;
        targets.MaxLength = 8000;
        return LocalScroll(Column(
            Card(Column(SecretInput("机器人 Webhook（含 access_token）", "dingtalk_webhook"),
                SecretInput("机器人签名密钥", "dingtalk_signing_secret"))),
            Card(Column(Paragraph(UiStrings.T("艾特指定成员"), size: 18), mode, targets,
                Paragraph(UiStrings.T("可用换行、逗号、分号或空格分隔。填写群内成员的手机号或钉钉 userId；不要填写姓名、员工编号或 Teambition 成员 ID。"), true),
                Paragraph(UiStrings.T("填写后会在群消息末尾添加 @ 标识；实际通知与姓名显示由钉钉处理。留空沿用原消息，不艾特所有人。"), true))),
            ActionButton("检查本机机器人配置", "check-dingtalk", async () =>
            {
                await EnsureSavedAsync();
                var result = await Worker.SendAsync("validate_dingtalk_config");
                SetStatus(UiStrings.F("Webhook 与签名格式通过本机检查；提醒对象 {0} 个，仅校验本机格式，未核验群成员资格；没有发送测试消息。", result["mention_count"]));
            }), Paragraph(UiStrings.T("这里只检查本机格式，不验证群成员，也不发送测试消息。"), true)));
    }

    private FrameworkElement SecretInput(string label, string key)
    {
        bool saved = _state.SecretPresence[key]?.GetValue<bool>() == true;
        var savedSettings = _state.Settings;
        var input = Identify(new PasswordBox
        {
            Header = UiStrings.T(label), Password = _state.SecretDrafts.GetValueOrDefault(key, ""),
            PlaceholderText = UiStrings.T(saved ? "本机已加密保存 · 留空保留" : "本机未保存"),
            PasswordRevealMode = PasswordRevealMode.Peek
        }, "secret-" + key);
        input.PasswordChanged += (_, _) =>
        {
            if (ReferenceEquals(savedSettings, _state.Settings)) _state.SetSecretDraft(key, input.Password);
        };
        return input;
    }

    private FrameworkElement BuildOpenAi()
    {
        var model = Identify(new ComboBox { Header = UiStrings.T("API 模型"), HorizontalAlignment = HorizontalAlignment.Stretch }, "openai-model");
        string current = _state.Text("openai_model", "gpt-6-luna");
        var ids = _state.Models.Select(x => Value(x, "id")).Append(current).Distinct().OrderBy(x => x).ToArray();
        foreach (string id in ids) model.Items.Add(id);
        model.SelectedItem = current;
        var effort = Identify(new ComboBox { Header = UiStrings.T("推理程度"), PlaceholderText = UiStrings.T("请选择已核验的选项"), HorizontalAlignment = HorizontalAlignment.Stretch }, "reasoning-effort");
        var tier = Identify(new ComboBox { Header = UiStrings.T("服务等级"), PlaceholderText = UiStrings.T("请选择已核验的选项"), HorizontalAlignment = HorizontalAlignment.Stretch }, "service-tier");
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
            Paragraph(UiStrings.T("default 为模型默认推理程度 / Standard 标准服务。fast 可能增加费用，且不保证更快；账户、项目或区域未必可用。只有此版本已核验的模型会提供 Fast。"), true),
            Paragraph(UiStrings.T("模型列表来自当前 API Key 的 OpenAI API，可调用权限与 ChatGPT 产品中的模型选择不同。模型列表本身不提供推理程度能力；未知模型只提供 default。"), true))),
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
        var archived = Identify(new CheckBox { Content = UiStrings.T("纳入已归档任务"), IsChecked = _state.Flag("include_archived") }, "include-archived");
        archived.Click += (_, _) => _state.Set("include_archived", archived.IsChecked == true);
        var scope = Card(Column(
            Paragraph(UiStrings.F("项目：{0}", _state.Text("project_id", UiStrings.T("尚未配置"))), size: 18),
            Paragraph(UiStrings.T("推荐范围：扫描指定 Project ID 下当前可读任务的动态，按动态发生时间筛选日报窗口；默认不含归档，不添加其他条件。任务在截止后再更新也会检查；已删除或不可见任务无法保证追溯。"), true),
            Paragraph(UiStrings.T("保存配置即采用当前项目、归档选项和筛选条件。范围或 Teambition 凭据变化时，会使用下面的时间区间自动只读核验；核验未通过时仍保存编辑，自动运行会关闭。"), true),
            archived, BuildFilterEditor()));
        var probe = Card(Column(Paragraph(UiStrings.T("只读核验与映射发现"), size: 18), TimeInputs(),
            Paragraph(UiStrings.T("选择含任务动态的区间。保存时会校验完整翻页、来源与字段样本，并发现映射 ID。任务当前更新时间不必落在所选区间；成员姓名和状态名称仍需核对。"), true),
            ActionButton("只读验证项目并发现映射 ID", "probe-project", async () =>
            {
                if (_state.SecretDrafts.Any(x => !string.IsNullOrEmpty(x.Value))) await SaveCoreAsync();
                var result = await Worker.SendAsync("probe_project", _state.WindowPayload(scope: true));
                _state.ApplyProbe(result);
                _state.Output = UiStrings.F("完整读取 {0} 个任务、{1} 条动态。核对成员/状态映射后保存配置，保存时会采用当前项目范围。", result["task_count"], result["activity_count"]);
                Navigate("project");
                SetStatus(_state.Output);
            }, accent: true)));
        var references = LocalScroll(Column(
            Card(Column(Paragraph(UiStrings.T("任务编号"), size: 18),
                Paragraph(UiStrings.T("标题保留原文中的冒号等标点，前面显示项目任务编号。编号数字从 Teambition 的真实 uniqueId 读取；API 已提供项目前缀时可留空，否则填写已核实的项目编号前缀。"), true),
                SettingText("已确认的任务编号前缀（可选）", "task_number_prefix", "task-number-prefix", "例如 UPCAM"))),
            Card(Column(Paragraph(UiStrings.T("任务打开位置"), size: 18),
                Paragraph(UiStrings.T("可粘贴当前项目“全部”视图的完整链接，或填写视图 ID。保存后只保留视图 ID；这不会改变日报采集范围。留空使用官方的任务直达链接。"), true),
                SettingText("“全部”视图链接或 ID（可选）", "task_link_view_id", "task-link-view", "https://www.teambition.com/project/项目ID/tasks/view/视图ID")))));
        var tabs = Identify(new TabView { IsAddTabButtonVisible = false, CanDragTabs = false,
            CanReorderTabs = false }, "project-tabs");
        tabs.TabItems.Add(new TabViewItem { Header = UiStrings.T("采集范围"), IsClosable = false,
            Content = LocalScroll(Column(scope, probe)) });
        tabs.TabItems.Add(new TabViewItem { Header = UiStrings.T("任务编号与链接"), IsClosable = false, Content = references });
        return tabs;
    }

    private FrameworkElement BuildFilterEditor()
    {
        var filters = new ListView { SelectionMode = ListViewSelectionMode.Single, MaxHeight = 140 };
        foreach (var item in _state.Filters)
            filters.Items.Add(new ListViewItem { Tag = item.Key, Content = Paragraph($"{item.Key} = {item.Value?.GetValue<string>()}") });
        var field = new ComboBox { Header = UiStrings.T("稳定字段"), HorizontalAlignment = HorizontalAlignment.Stretch };
        foreach (var item in new Dictionary<string, string>
        {
            ["executorId"] = "执行人 ID", ["tasklistId"] = "任务列表 ID", ["stageId"] = "阶段 ID",
            ["tfsId"] = "状态 ID", ["scenariofieldconfigId"] = "自定义字段配置 ID",
            ["visible"] = "可见性", ["parentTaskId"] = "父任务 ID"
        }) field.Items.Add(new ComboBoxItem { Tag = item.Key, Content = UiStrings.T(item.Value) });
        field.SelectedIndex = 0;
        var value = new TextBox { Header = UiStrings.T("字段值 / API ID") };
        return new Expander
        {
            Header = UiStrings.F("额外项目筛选（可选，当前 {0} 项）", _state.Filters.Count),
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
            string name = _state.Map("owner_names")[id]?.GetValue<string>() ?? UiStrings.T("待核对姓名");
            list.Items.Add(new ListViewItem { Tag = id, HorizontalContentAlignment = HorizontalAlignment.Stretch,
                Content = Pair(Paragraph(id, true), Paragraph(name)) });
        }
        return FillLayout(Column(
            Paragraph(UiStrings.F("已保存或待保存 {0} 条映射。当前批准的接口只能发现样本 ID，不是完整成员名册；员工编号不能代替 API ID。", _state.Map("owner_names").Count), true),
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
        var inputId = new TextBox { Header = UiStrings.T("API 成员 ID"), Text = id ?? "", IsReadOnly = id is not null };
        var name = new TextBox { Header = UiStrings.T("姓名"), Text = id is null ? "" : _state.Map("owner_names")[id]?.GetValue<string>() ?? "" };
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
            PlaceholderText = UiStrings.T("{\"demo-member-01\":\"虚构成员甲\"}\n也可每行：成员ID<实际制表符>姓名")
        }, "member-import-text");
        ScrollViewer.SetVerticalScrollBarVisibility(input, ScrollBarVisibility.Auto);
        var dialog = EditorDialog("批量导入成员映射", Column(
            Paragraph(UiStrings.T("粘贴 JSON 对象或 ID 与姓名之间有一个制表符的多行文本。冲突和格式错误会整批拒绝。"), true), input), "导入");
        if (await ShowDialogAsync(dialog) != ContentDialogResult.Primary) return;
        var result = await Worker.SendAsync("import_owner_mappings", new JsonObject
        { ["source"] = input.Text, ["existing"] = _state.Map("owner_names").DeepClone() });
        _state.Set("owner_names", result["mappings"]!.DeepClone());
        Navigate("members"); SetStatus(UiStrings.F("已导入 {0} 条映射；保存本机配置后生效。", result["imported_count"]));
    }

    private FrameworkElement BuildStatuses()
    {
        var list = Identify(new ListView { SelectionMode = ListViewSelectionMode.Single }, "status-list");
        var resolved = (_state.Settings["resolved_status_ids"] as JsonArray)?.Select(x => x!.GetValue<string>()).ToHashSet() ?? new();
        foreach (string id in _state.Map("status_names").Select(p => p.Key).Concat(_state.CandidateStatuses).Distinct().Order())
        {
            string name = _state.Map("status_names")[id]?.GetValue<string>() ?? UiStrings.T("待核对名称");
            string suggestion = _state.StatusSuggestions.GetValueOrDefault(id, "");
            var row = new Grid { ColumnSpacing = 16, Padding = new Thickness(0, 10, 0, 10) };
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(2, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            row.Children.Add(Column(Paragraph(id, true), Paragraph(name, size: 16)));
            var hint = Paragraph(string.IsNullOrEmpty(suggestion) ? "" : UiStrings.F("样本建议：{0}", suggestion), true);
            row.Children.Add(hint); Grid.SetColumn(hint, 1);
            var done = Paragraph(UiStrings.T(resolved.Contains(id) ? "✓ 已解决" : "未解决"), true);
            row.Children.Add(done); Grid.SetColumn(done, 2);
            list.Items.Add(new ListViewItem { Tag = id, HorizontalContentAlignment = HorizontalAlignment.Stretch, Content = row });
        }
        return FillLayout(Column(
            Paragraph(UiStrings.T("状态名称必须与真实项目一致。样本动态返回的名称只作建议，不能凭 ID 猜名称；请逐项确认已解决状态。"), true),
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
        var inputId = Identify(new TextBox { Header = UiStrings.T("API 状态 ID"), Text = id ?? "", IsReadOnly = id is not null }, "status-id-input");
        var name = Identify(new TextBox { Header = UiStrings.T("已核对名称"), Text = id is null ? "" : _state.Map("status_names")[id]?.GetValue<string>() ?? "" }, "status-name-input");
        var resolved = (_state.Settings["resolved_status_ids"] as JsonArray)?.Select(x => x!.GetValue<string>()).ToHashSet() ?? new();
        var done = new CheckBox { Content = UiStrings.T("此状态代表已解决"), IsChecked = id is not null && resolved.Contains(id) };
        string suggestion = id is null ? "" : _state.StatusSuggestions.GetValueOrDefault(id, "");
        var dialog = EditorDialog("状态映射", Column(inputId, name, Paragraph(UiStrings.F("样本名称建议：{0}", suggestion.Length == 0 ? UiStrings.T("无") : suggestion), true), done));
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
        XamlRoot = RootGrid.XamlRoot, RequestedTheme = RootGrid.RequestedTheme, Title = UiStrings.T(title),
        PrimaryButtonText = UiStrings.T(primary), CloseButtonText = UiStrings.T("取消"), DefaultButton = ContentDialogButton.Primary,
        Content = new ScrollViewer { Content = content, MaxHeight = 420 }
    };

    private FrameworkElement BuildSchedule()
    {
        var limit = Identify(new NumberBox
        {
            Header = UiStrings.T("每批采集任务数（1–700）"), Minimum = 1, Maximum = 700,
            Value = _state.Number("max_tasks_per_run", 300), SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact
        }, "task-limit");
        limit.ValueChanged += (_, _) =>
        {
            if (!double.IsNaN(limit.Value)) _state.Set("max_tasks_per_run", (int)limit.Value);
        };
        var concurrent = Identify(new NumberBox
        {
            Header = UiStrings.T("并行读取任务数（1–4）"), Minimum = 1, Maximum = 4,
            Value = _state.Number("max_concurrent_tasks", 4), SpinButtonPlacementMode = NumberBoxSpinButtonPlacementMode.Compact
        }, "task-concurrency");
        concurrent.ValueChanged += (_, _) =>
        {
            if (double.IsNaN(concurrent.Value)) return;
            int count = (int)Math.Clamp(concurrent.Value, 1, 4);
            _state.Set("max_concurrent_tasks", count);
            if (concurrent.Value != count) concurrent.Value = count;
        };
        FrameworkElement Slot(string label, string prefix) => Card(Column(Paragraph(UiStrings.T(label), size: 18),
            SettingText("运行时间", prefix + "_run_at", prefix + "-run-at", "HH:MM"),
            Pair(SettingText(prefix == "morning" ? "采集开始（前一日）" : "采集开始", prefix + "_start_at", prefix + "-start-at", "HH:MM"),
                 SettingText(prefix == "morning" ? "采集结束（当日）" : "采集结束", prefix + "_end_at", prefix + "-end-at", "HH:MM"))));
        return LocalScroll(Column(
            Card(Column(Toggle("应用运行期间自动采集、校验、摘要并发送", "auto_send_enabled", "auto-run"),
                Paragraph(UiStrings.T("保存后生效。电脑关机、休眠、未登录或断网时无法执行；重开后只补跑最近 24 小时内、自动执行启用期间且没有本机记录的时段。"), true))),
            Pair(Slot("上午日报", "morning"), Slot("下午日报", "afternoon")),
            Card(Column(limit, concurrent,
                Paragraph(UiStrings.T("默认并行读取 4 个任务；所有线程共用至少 0.25 秒的请求间隔，遇到 429 会共同退避。可降到 1；不提高程序原有请求速率上限，实际速度取决于候选数量与接口延迟。"), true),
                Paragraph(UiStrings.T("默认每批 300 个任务；这不是项目总数限制，全项目最多 10,000 个当前可读任务。缩短时间窗不会减少基础扫描。"), true),
                Paragraph(UiStrings.T("整次采集最多 50,000 次请求、1,000,000 条返回记录、512 MiB 响应、128 MiB 保留数据及两小时（含进度缓存年龄），最多进行 3 轮来源重核；各批仍受分页、请求、记录、响应体和时间预算约束。超过任一限制会阻断摘要与发送，不会截断采集。"), true),
                Paragraph(UiStrings.T("完整读取任务及全部动态分页后才保存短期进度。取消或断电保留未完成进度，自本次采集开始最多两小时；下次会重列任务并刷新变更。这不是历史快照。"), true),
                Toolbar(ActionButton("清除未完成的采集进度", "clear-collection-progress", async () =>
                {
                    var result = await Worker.SendAsync("clear_collection_progress");
                    SetStatus(Value(result, "status"));
                })),
                Paragraph(UiStrings.T("仅清除临时采集进度，保留本机设置、凭据、运行历史和发送防重账本；不改变自动发送授权。"), true)))));
    }

    private FrameworkElement BuildRun()
    {
        var output = Identify(new TextBox
        {
            IsReadOnly = true, AcceptsReturn = true, TextWrapping = TextWrapping.Wrap,
            HorizontalAlignment = HorizontalAlignment.Stretch, VerticalAlignment = VerticalAlignment.Stretch,
            Text = _state.Output == "选择时间范围后，先运行只读诊断。" ? UiStrings.T(_state.Output) : _state.Output
        }, "diagnostic-output");
        ScrollViewer.SetVerticalScrollBarVisibility(output, ScrollBarVisibility.Auto);
        var export = ActionButton("导出脱敏结构", "export-diagnostic", ExportDiagnosticAsync);
        export.IsEnabled = _state.ExportAvailable;
        var copy = ActionButton("复制诊断文字", "copy-diagnostic", CopyDiagnosticAsync);
        copy.IsEnabled = _state.ExportAvailable && !string.IsNullOrEmpty(_state.DiagnosticText);
        var toolbar = Toolbar(
            ActionButton("只读诊断", "diagnose", async () =>
            {
                RequireDiagnosticSaved();
                _state.ExportAvailable = false;
                _state.DiagnosticFileStem = "";
                _state.DiagnosticText = "";
                Navigate("run");
                await DiagnoseWithProgressAsync();
            }, accent: true),
            ActionButton("生成并自动发送", "run-manual", async () =>
            {
                await EnsureSavedAsync();
                var payload = _state.WindowPayload();
                payload["manual_request_id"] = Guid.NewGuid().ToString("D");
                var result = await Worker.SendAsync("run_manual", payload);
                _state.Output = Value(result, "markdown");
                await RefreshHistoryAsync(); Navigate("run");
                SetStatus(UiStrings.F("日报已发送并收到钉钉确认。运行记录：{0}", Value(result, "run_id")));
            }),
            ActionButton("预览虚构样例", "preview-sample", async () =>
            {
                var result = await Worker.SendAsync("preview_sample", _state.WindowPayload());
                _state.Output = Value(result, "markdown"); Navigate("run"); SetStatus(Value(result, "status"));
            }), export, copy,
            ActionButton("详细诊断导出…", "export-detailed-diagnostic", ExportDetailedDiagnosticAsync));
        var range = new Grid { ColumnSpacing = 12 };
        range.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
        range.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
        var details = Column(Paragraph(UiStrings.T("本机采集范围"), true, 12),
            Paragraph(UiStrings.F("开始  {0}\n结束  {1}", _state.ManualStart, _state.ManualEnd), true));
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
        var start = Identify(new TextBox { Header = UiStrings.T("开始（本机时间）"), Text = _state.ManualStart }, "edit-window-start");
        var end = Identify(new TextBox { Header = UiStrings.T("结束（本机时间）"), Text = _state.ManualEnd }, "edit-window-end");
        var dialog = EditorDialog("修改采集时间", Column(start, end,
            Paragraph(UiStrings.T("格式为 YYYY-MM-DD HH:MM。只读诊断不会调用模型或发送；正式运行会按该区间自动发送。"), true)), "应用");
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

    private void ApplyDiagnosticResult(JsonObject result)
    {
        _state.Output = Value(result, "message");
        _state.ExportAvailable = result["export_available"]?.GetValue<bool>() == true;
        _state.DiagnosticFileStem = Value(result, "diagnostic_file_stem");
        if (result["diagnostic_summary"] is JsonObject counts)
        {
            int inWindow = counts["unknown_events_in_report_window"]?.GetValue<int>() ?? 0;
            int unknownTime = counts["unknown_events_with_unknown_time"]?.GetValue<int>() ?? 0;
            _state.Output += UiStrings.F("\n\n窗口内未支持动作：{0} 条。时间归属无法判断的未支持动作：{1} 条。", inWindow, unknownTime);
            if (counts["catalog_complete"]?.GetValue<bool>() != true)
                _state.Output += UiStrings.T("\n诊断目录未完整，以上仅为已扫描部分，不能据此认定没有其他动态。");
        }
        if (result["diagnostic"] is JsonObject metadata)
            _state.Output += UiStrings.F("\n\n诊断 v{0} · UTC {1}", Value(metadata, "application_version"), Value(metadata, "created_at_utc"));
        _state.DiagnosticText = _state.Output;
    }

    private async Task ExportDetailedDiagnosticAsync()
    {
        var consent = Identify(new ContentDialog
        {
            XamlRoot = RootGrid.XamlRoot, RequestedTheme = RootGrid.RequestedTheme,
            Title = UiStrings.T("详细诊断包含项目内容"), PrimaryButtonText = UiStrings.T("选择本机文件夹"), CloseButtonText = UiStrings.T("取消"),
            DefaultButton = ContentDialogButton.Close,
            Content = new ScrollViewer { MaxHeight = 360, Content = Paragraph(UiStrings.T("文件会保留所选时间段的任务标题、动态正文、引用、实际动作及必要映射，供私下分析。" +
                "应用将剔除配置凭据和识别到的认证字段；项目正文仍可能含手工写入的私人信息。" +
                "请选择未同步的本机文件夹，勿放入代码仓库或公开分享。此操作只读采集，不调用模型、不发送群消息。")) }
        }, "detailed-export-consent");
        if (await ShowDialogAsync(consent) != ContentDialogResult.Primary)
        {
            SetStatus("已取消详细诊断，没有采集或保存项目内容。");
            return;
        }
        var picker = new FolderPicker { SuggestedStartLocation = PickerLocationId.Downloads };
        picker.FileTypeFilter.Add("*");
        WinRT.Interop.InitializeWithWindow.Initialize(picker, WinRT.Interop.WindowNative.GetWindowHandle(this));
        StorageFolder? folder = await picker.PickSingleFolderAsync();
        if (folder is null) { SetStatus("已取消保存，没有采集详细项目数据。"); return; }
        await EnsureSavedAsync();
        string fileName = $"teambition-detailed-diagnostic-v{_state.ApplicationVersion}-{DateTime.UtcNow:yyyyMMddTHHmmssZ}-{Guid.NewGuid():N}.json";
        var payload = _state.WindowPayload();
        payload["path"] = Path.Combine(folder.Path, fileName);
        payload["include_project_content"] = true;
        _state.ExportAvailable = false;
        _state.DiagnosticText = "";
        var result = await Worker.SendAsync("export_detailed_diagnostic_to_file", payload);
        ApplyDiagnosticResult(result);
        Navigate("run");
        string message = UiStrings.T(Value(result["detailed_export"], "message", "详细诊断没有保存。"));
        if (result["detailed_export"]?["saved"]?.GetValue<bool>() == true)
            message += UiStrings.F(" 文件名：{0}", fileName);
        SetStatus(message);
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
                Content = Column(Pair(Paragraph(Value(row, "created_at_utc"), true), Paragraph(UiStrings.T(label))),
                    Paragraph($"{Value(row, "window_start_local")} → {Value(row, "window_end_local")}"),
                    new TextBlock { Text = UiStrings.T(Value(row, "delivery_note", "点击查看日报或诊断说明")), TextTrimming = TextTrimming.CharacterEllipsis, MaxLines = 1 })
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
            Paragraph(UiStrings.T("发送结果不明或已确认的记录不会自动重发。"), true)), Card(list));
    }
}
