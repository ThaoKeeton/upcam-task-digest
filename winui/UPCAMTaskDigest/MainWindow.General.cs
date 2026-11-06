using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;
using CommunityToolkit.WinUI.Controls;

namespace Upcam.Desktop;

public sealed partial class MainWindow
{
    private Microsoft.UI.Dispatching.DispatcherQueueTimer? _updateTimer;
    private DateTimeOffset _lastAutomaticUpdateCheck = DateTimeOffset.MinValue;
    private bool _syntheticUpdateLaunch;
    private bool _syntheticRestart;
    private int _generalTabIndex;
    private bool HasUnsavedGeneral => _state.GeneralDirty || _state.ClearUpdateToken || _state.UpdateTokenDraft.Length > 0;

    private void ApplyGeneralAppearance()
    {
        UiStrings.Configure(_state.GeneralText("ui_language"), CultureInfo.CurrentUICulture.Name);
        RootGrid.Language = UiStrings.ResolvedLanguage;
        RootGrid.RequestedTheme = _state.GeneralText("ui_theme") switch
        {
            "dark" => ElementTheme.Dark,
            "light" => ElementTheme.Light,
            _ => ElementTheme.Default
        };
        Title = UiStrings.T("UPCAM 站会日报") + " · v" + typeof(MainWindow).Assembly.GetName().Version?.ToString(3) +
            (_smoke ? " · " + UiStrings.T("CI 虚构数据") : "");
        AppTitleBar.Title = Title;
        foreach (NavigationViewItem item in Navigation.MenuItems.OfType<NavigationViewItem>())
            if (item.Tag is string route && _routes.TryGetValue(route, out var info)) item.Content = UiStrings.T(info.Title);
        foreach (NavigationViewItemHeader header in Navigation.MenuItems.OfType<NavigationViewItemHeader>())
            header.Content = UiStrings.T("配置");
        _native?.ConfigureTray(_state.Flag("tray_icon_enabled"));
        UpdateCaptionTheme();
    }

    private void UpdateCaptionTheme()
    {
        bool dark = RootGrid.ActualTheme == ElementTheme.Dark;
        var foreground = dark ? Microsoft.UI.Colors.White : Microsoft.UI.Colors.Black;
        if (new Windows.UI.ViewManagement.AccessibilitySettings().HighContrast)
            foreground = new Windows.UI.ViewManagement.UISettings().GetColorValue(Windows.UI.ViewManagement.UIColorType.Foreground);
        AppWindow.TitleBar.ButtonBackgroundColor = Microsoft.UI.Colors.Transparent;
        AppWindow.TitleBar.ButtonInactiveBackgroundColor = Microsoft.UI.Colors.Transparent;
        AppWindow.TitleBar.ButtonForegroundColor = foreground;
        AppWindow.TitleBar.ButtonInactiveForegroundColor = dark
            ? Windows.UI.Color.FromArgb(255, 153, 153, 153) : Windows.UI.Color.FromArgb(255, 96, 96, 96);
        AppWindow.TitleBar.ButtonHoverBackgroundColor = dark
            ? Windows.UI.Color.FromArgb(255, 58, 58, 58) : Windows.UI.Color.FromArgb(255, 229, 229, 229);
        AppWindow.TitleBar.ButtonHoverForegroundColor = foreground;
    }

    private FrameworkElement BuildGeneral()
    {
        var tabs = new TabView { IsAddTabButtonVisible = false, CanDragTabs = false, CanReorderTabs = false };
        tabs.TabItems.Add(new TabViewItem { Header = UiStrings.T("版本更新"), IsClosable = false, Content = BuildUpdates() });
        tabs.TabItems.Add(new TabViewItem { Header = UiStrings.T("外观"), IsClosable = false, Content = BuildAppearance() });
        tabs.TabItems.Add(new TabViewItem { Header = UiStrings.T("启动与托盘"), IsClosable = false, Content = BuildStartupPreferences() });
        tabs.SelectedIndex = _generalTabIndex;
        tabs.SelectionChanged += (_, _) => _generalTabIndex = Math.Max(0, tabs.SelectedIndex);
        return Identify(tabs, "general-tabs");
    }

    private FrameworkElement BuildUpdates()
    {
        var snapshot = _state.GeneralPreferences;
        var token = Identify(new PasswordBox
        {
            Header = UiStrings.T("GitHub 私有仓库只读令牌（可选）"),
            Password = _state.UpdateTokenDraft,
            PlaceholderText = UiStrings.T(_state.ClearUpdateToken ? "保存后清除" :
                _state.SecretPresence["github_update_token"]?.GetValue<bool>() == true ? "本机已加密保存；留空保留" : "仅需本仓库 Contents: Read 权限")
        }, "update-token");
        token.PasswordChanged += (_, _) =>
        {
            if (ReferenceEquals(snapshot, _state.GeneralPreferences)) _state.UpdateTokenDraft = token.Password;
        };
        string state = Value(_state.UpdateState, "status", "not_checked");
        string message = Value(_state.UpdateState, "message", "尚未检查更新。当前仅使用本仓库正式稳定发布。");
        var download = ActionButton("下载并校验更新", "update-download", async () =>
        {
            await EnsureGeneralSavedAsync();
            _state.UpdateState = await Worker.SendAsync("download_update");
            Navigate("general");
            SetStatus(Value(_state.UpdateState, "message"));
        });
        download.IsEnabled = state is "available" or "downloaded";
        var install = ActionButton("安装更新并重新启动", "update-install", InstallUpdateAsync);
        install.IsEnabled = state == "downloaded";
        return LocalScroll(Column(
            Card(Column(Paragraph(UiStrings.F("当前版本 {0}", _state.ApplicationVersion), size: 20),
                Paragraph(UiStrings.T("更新通道：稳定版 · ThaoKeeton/upcam-task-digest"), true),
                Paragraph(UiStrings.T(message)),
                Toolbar(ActionButton("检查更新", "update-check", () => CheckForUpdatesAsync()), download, install),
                GeneralToggle("启动后自动检查更新（不自动安装）", "auto_check_updates", "auto-update-check"))),
            Card(Column(token,
                Paragraph(UiStrings.T("此仓库为私有仓库；令牌仅用于 GitHub 读取，在本机用 Windows DPAPI 加密，不发给 Teambition、OpenAI 或钉钉。"), true),
                ActionButton("清除更新令牌", "clear-update-token", () =>
                {
                    _state.UpdateTokenDraft = "";
                    _state.ClearUpdateToken = true;
                    Navigate("general");
                    SetStatus(UiStrings.T("保存常规设置后清除更新令牌。"));
                    return Task.CompletedTask;
                }))),
            Card(Paragraph(UiStrings.T("下载需通过发布来源、SHA-256 和整包校验；安装到当前用户目录，保留上一版本，无需管理员权限。尚无正式发布时会明确提示，不会把未验证下载当成可安装更新。"), true))));
    }

    private FrameworkElement BuildAppearance()
    {
        var language = GeneralChoice("界面语言", "ui_language", "ui-language",
            new[] { ("system", "Windows 默认"), ("zh-CN", "简体中文"), ("en-US", "English") });
        var theme = GeneralChoice("应用主题", "ui_theme", "ui-theme",
            new[] { ("system", "Windows 默认"), ("light", "浅色"), ("dark", "深色") });
        return LocalScroll(Column(
            PreferenceCard("界面语言", "支持简体中文和 English；其他 Windows 语言使用 English。项目名称、评论、诊断原文和仓库日报不会被自动翻译。", language),
            PreferenceCard("应用主题", "主题立即预览，保存后在下次启动时保留；标题栏、对话框和页面随主题变化。", theme),
            Card(Column(Paragraph(UiStrings.T("界面会立即切换；部分原生模板在保存并重新启动后应用所选语言。"), true),
                ActionButton("重新启动应用", "restart-app", RestartApplicationAsync)))));
    }

    private FrameworkElement BuildStartupPreferences()
    {
        var behavior = new SettingsExpander
        {
            Header = UiStrings.T("启动与托盘"), IsExpanded = true,
            Description = UiStrings.T("托盘菜单提供显示主窗口和退出应用。关闭托盘时会关闭隐藏选项；托盘不可用时保留可见窗口。")
        };
        behavior.Items.Add(PreferenceCard("显示系统托盘图标", "", GeneralToggle("显示系统托盘图标", "tray_icon_enabled", "tray-enabled")));
        behavior.Items.Add(PreferenceCard("关闭主窗口时隐藏到托盘", "", GeneralToggle("关闭主窗口时隐藏到托盘", "close_to_tray", "close-to-tray")));
        behavior.Items.Add(PreferenceCard("手动启动时隐藏到托盘", "", GeneralToggle("手动启动时隐藏到托盘", "start_in_tray", "start-in-tray")));
        return LocalScroll(Column(
            PreferenceCard("当前 Windows 用户登录后启动，并以系统托盘运行",
                "使用已有的当前用户启动项，不需要管理员权限；关机、休眠、未登录或离线时不会执行日报。",
                GeneralToggle("当前 Windows 用户登录后启动，并以系统托盘运行", "start_with_windows", "startup")), behavior));
    }

    // PowerToys GeneralPage uses these MIT-licensed CommunityToolkit controls.
    // Source/versions and adaptation attribution: THIRD_PARTY_NOTICES.md.
    private static SettingsCard PreferenceCard(string label, string description, FrameworkElement input)
    {
        if (input is ComboBox combo) combo.Header = null;
        if (input is ToggleSwitch toggle) toggle.Header = null;
        Microsoft.UI.Xaml.Automation.AutomationProperties.SetName(input, UiStrings.T(label));
        return new SettingsCard { Header = UiStrings.T(label), Description = UiStrings.T(description), Content = input };
    }

    private ComboBox GeneralChoice(string label, string key, string id, (string Value, string Label)[] choices)
    {
        var box = Identify(new ComboBox { Header = UiStrings.T(label), HorizontalAlignment = HorizontalAlignment.Stretch }, id);
        foreach (var choice in choices) box.Items.Add(new ComboBoxItem { Tag = choice.Value, Content = UiStrings.T(choice.Label) });
        box.SelectedIndex = Math.Max(0, Array.FindIndex(choices, choice => choice.Value == _state.GeneralText(key)));
        box.SelectionChanged += (_, _) =>
        {
            if (box.SelectedItem is not ComboBoxItem { Tag: string value }) return;
            _state.SetGeneral(key, JsonValue.Create(value)!);
            ApplyGeneralAppearance();
            // Rebuild only the current page; text input commits synchronously to DesktopState.
            _generalTabIndex = 1;
            Navigate("general");
        };
        return box;
    }

    private ToggleSwitch GeneralToggle(string label, string key, string id)
    {
        var toggle = Identify(new ToggleSwitch { Header = UiStrings.T(label), IsOn = _state.GeneralFlag(key),
            OnContent = UiStrings.T("已开启"), OffContent = UiStrings.T("已关闭") }, id);
        if (key is "close_to_tray" or "start_in_tray") toggle.IsEnabled = _state.GeneralFlag("tray_icon_enabled");
        toggle.Toggled += (_, _) =>
        {
            _state.SetGeneral(key, JsonValue.Create(toggle.IsOn)!);
            if (key == "tray_icon_enabled" && !toggle.IsOn)
            {
                _state.SetGeneral("close_to_tray", JsonValue.Create(false)!);
                _state.SetGeneral("start_in_tray", JsonValue.Create(false)!);
            }
            if (key == "tray_icon_enabled")
            {
                _generalTabIndex = 2;
                Navigate("general");
            }
        };
        return toggle;
    }

    private async Task SaveGeneralCoreAsync()
    {
        var result = await Worker.SendAsync("save_preferences", _state.GeneralSavePayload());
        _state.ApplyGeneral(result);
        ApplyGeneralAppearance();
        Navigate(_page);
        SetStatus(Value(result, "status", "常规设置已保存。"));
    }

    private async Task EnsureGeneralSavedAsync()
    {
        if (HasUnsavedGeneral) await SaveGeneralCoreAsync();
    }

    private async Task CheckForUpdatesAsync()
    {
        await EnsureGeneralSavedAsync();
        _state.UpdateState = await Worker.SendAsync("check_updates");
        if (_page == "general") Navigate("general");
        SetStatus(Value(_state.UpdateState, "message"));
    }

    private void StartUpdateChecks()
    {
        if (_smoke) return;
        _updateTimer = DispatcherQueue.CreateTimer();
        _updateTimer.Interval = TimeSpan.FromMinutes(15);
        _updateTimer.Tick += async (_, _) => await CheckAutomaticUpdatesAsync();
        _updateTimer.Start();
        _ = CheckAutomaticUpdatesAsync();
    }

    private async Task CheckAutomaticUpdatesAsync()
    {
        if (_busy || !_ready || HasUnsavedGeneral || !_state.Flag("auto_check_updates") ||
            DateTimeOffset.UtcNow - _lastAutomaticUpdateCheck < TimeSpan.FromHours(6)) return;
        _lastAutomaticUpdateCheck = DateTimeOffset.UtcNow;
        await RunOperationAsync(UiStrings.T("正在检查稳定版更新…"), async () =>
        {
            _state.UpdateState = await Worker.SendAsync("check_updates");
            if (_page == "general") Navigate("general");
            SetStatus(Value(_state.UpdateState, "message"));
        }, showErrorDialog: false);
    }

    private async Task InstallUpdateAsync()
    {
        if (_state.Dirty || _state.SecretDrafts.Any(x => x.Value.Length > 0) || HasUnsavedGeneral)
            throw new InvalidOperationException(UiStrings.T("请先保存尚未保存的设置和凭据，再安装更新。"));
        var result = await Worker.SendAsync("prepare_update");
        if (Value(result, "status") != "ready") throw new InvalidOperationException(Value(result, "message"));
        string path = Path.GetFullPath(Value(result, "executable"));
        string root = Path.GetFullPath(_state.DataDirectory) + Path.DirectorySeparatorChar;
        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase) ||
            !string.Equals(Path.GetFileName(path), "UPCAMTaskDigest.exe", StringComparison.OrdinalIgnoreCase) || !File.Exists(path))
            throw new InvalidOperationException(UiStrings.T("更新启动路径未通过本机校验。"));
        // Adapt PowerToys' shared-read execution lock, without claiming code signing.
        using var executableLock = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        var lockedProof = await Worker.SendAsync("prepare_update");
        if (Value(lockedProof, "status") != "ready" || Path.GetFullPath(Value(lockedProof, "executable")) != path)
            throw new InvalidOperationException(UiStrings.T("锁定后的更新校验未通过；当前版本已保留。"));
        if (_smoke)
        {
            _syntheticUpdateLaunch = true;
            SetStatus(UiStrings.T("合成更新校验完成；未启动其他应用或修改启动项。"));
            return;
        }
        using var process = await ProcessHandoff.StartAsync(path, afterUpdate: true);
        _scheduler?.Stop();
        _updateTimer?.Stop();
        _exitRequested = true;
        Close();
    }

    private async Task RestartApplicationAsync()
    {
        if (_state.Dirty || HasUnsavedGeneral || _state.SecretDrafts.Any(x => x.Value.Length > 0))
            throw new InvalidOperationException(UiStrings.T("请先保存尚未保存的设置和凭据，再重新启动应用。"));
        if (_smoke) { _syntheticRestart = true; return; }
        using var process = await ProcessHandoff.StartAsync(Path.Combine(AppContext.BaseDirectory, "UPCAMTaskDigest.exe"), afterUpdate: false);
        _scheduler?.Stop();
        _updateTimer?.Stop();
        _exitRequested = true;
        Close();
    }
}
