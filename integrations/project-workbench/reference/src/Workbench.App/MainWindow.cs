using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Workbench.Core;
using Workbench.WinUI.Sdk;

namespace Workbench.App;

internal sealed partial class MainWindow : Window
{
    private readonly RunOptions _options;
    private readonly Grid _root;
    private readonly NavigationView _navigation;
    private readonly ContentControl _content;
    private readonly ContentControl _body;
    private readonly InfoBar _notice;
    private HostRuntime? _runtime;
    private PluginManager Manager => _runtime?.Manager ?? throw new InvalidOperationException("工作台尚未就绪。");
    private bool _refreshing;
    private bool _initialized;
    private Exception? _startupFailure;
    private bool _busy;
    private bool _closing;
    private bool _allowClose;
    private bool _stopped;
    private readonly TaskCompletionSource _closeCompleted = new();
    private readonly int _uiThread = Environment.CurrentManagedThreadId;

    public MainWindow(RunOptions options)
    {
        _options = options;
        options.TraceStartup("window-constructor-entered");
        _root = new Grid();
        options.TraceStartup("creating-navigation-view");
        _navigation = Ui.Named(new NavigationView
        {
            PaneDisplayMode = NavigationViewPaneDisplayMode.Auto,
            IsSettingsVisible = false,
            IsEnabled = false,
            IsBackButtonVisible = NavigationViewBackButtonVisible.Collapsed,
            PaneTitle = "Project Workbench"
        }, "工作台导航");
        options.TraceStartup("creating-content-controls");
        _content = new ContentControl { HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Stretch };
        _body = new ContentControl { HorizontalContentAlignment = HorizontalAlignment.Stretch, VerticalContentAlignment = VerticalAlignment.Stretch };
        options.TraceStartup("creating-info-bar");
        _notice = Ui.Named(new InfoBar { IsOpen = false }, "操作结果与错误");
        Title = options.SmokeTest ? "Project Workbench · isolated native smoke" : "Project Workbench · 原生插件工作台";
        _root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _root.RowDefinitions.Add(new RowDefinition());
        _root.Children.Add(_notice);
        Grid.SetRow(_body, 1);
        _root.Children.Add(_body);
        _body.Content = _navigation;
        _navigation.IsEnabled = false;
        _navigation.IsSettingsVisible = false;
        _navigation.Content = _content;
        Content = _root;
        AppWindow.Resize(new Windows.Graphics.SizeInt32(1180, 800));
        _navigation.SelectionChanged += (_, args) =>
        {
            if (!_initialized || _refreshing || _busy || _closing) return;
            if (args.IsSettingsSelected) Navigate(new("settings"));
            else if (args.SelectedItemContainer?.Tag is NavigationTarget target) Navigate(target);
        };
        AppWindow.Closing += OnClosing;
        ShowPage(Ui.Page("正在启动", "读取插件清单与本地设置…"));
        options.TraceStartup("window-content-ready");
    }

    public async Task InitializeAsync()
    {
        try
        {
            _options.TraceStartup("opening-host-runtime");
            _runtime = new HostRuntime(_options);
            _options.TraceStartup("loading-host-theme");
            LoadTheme();
            _options.TraceStartup("discovering-packages");
            await Manager.DiscoverAsync();
            _options.TraceStartup("restoring-enabled-packages");
            await Manager.RestoreEnabledAsync();
            RefreshNavigation();
            _initialized = true;
            _navigation.IsSettingsVisible = true;
            _navigation.IsEnabled = true;
            _options.TraceStartup("opening-home-page");
            Navigate(new("home"));
            _options.TraceStartup("host-initialization-completed");
            if (_options.SmokeTest) await NativeSmoke.RunAsync(this, _options);
        }
        catch (Exception error)
        {
            _startupFailure = error;
            _initialized = false;
            _navigation.IsEnabled = false;
            _navigation.IsSettingsVisible = false;
            DetachPluginViews();
            var failure = Ui.Page("工作台未能启动", error.Message);
            failure.Children.Add(Ui.Text("请关闭此窗口后重试。若用户目录已被占用，请先关闭另一个工作台实例。"));
            var close = Ui.Named(new Button { Content = "关闭窗口" }, "关闭未能启动的工作台窗口");
            close.Click += async (_, _) => await CloseAsync();
            failure.Children.Add(close);
            _body.Content = ScrollPage(failure); // Independent of Manager, even after partial initialization.
            Notice("启动或原生验证失败", error.Message, InfoBarSeverity.Error);
            if (_options.SmokeTest)
            {
                Environment.ExitCode = 1;
                _options.WriteFailure("initialize-or-native-smoke", error);
                await FinishSmokeAsync();
            }
        }
    }

    private void RefreshNavigation()
    {
        _refreshing = true;
        try
        {
            _navigation.MenuItems.Clear();
            AddNavigation("首页", Symbol.Home, new("home"));
            AddNavigation("插件", Symbol.AllApps, new("plugins"));
            AddNavigation("命令", Symbol.Play, new("commands"));
            foreach (var item in Manager.Contributions.Views)
            {
                if (item.View is not WinUiView view) continue;
                var label = view.Kind == WinUiViewKind.Settings ? $"{view.Title} · 设置" : view.Title;
                AddNavigation(label, view.Kind == WinUiViewKind.Settings ? Symbol.Setting : Symbol.Document,
                    new("view", item.PluginId, view.Id));
            }
            if (_navigation.SettingsItem is NavigationViewItem settings) Ui.Named(settings, "工作台设置");
        }
        finally { _refreshing = false; }
    }

    private void AddNavigation(string title, Symbol icon, NavigationTarget target)
    {
        var item = Ui.Named(new NavigationViewItem
        {
            Content = title,
            Icon = new SymbolIcon(icon),
            Tag = target
        }, title);
        _navigation.MenuItems.Add(item);
    }

    private void Navigate(NavigationTarget target)
    {
        if (!_initialized) return;
        try
        {
            switch (target.Page)
            {
                case "plugins": ShowPage(PluginsPage()); break;
                case "commands": ShowPage(CommandsPage()); break;
                case "settings": ShowPage(SettingsPage()); break;
                case "view":
                    var registration = Manager.Contributions.Views.SingleOrDefault(v => v.PluginId == target.PluginId && v.View.Id == target.ViewId);
                    if (registration?.View is not WinUiView view) throw new InvalidOperationException("插件页面已停用或未提供原生视图。");
                    AssertUiThread();
                    _content.Content = view.CreateView() ?? throw new InvalidOperationException("插件返回了空视图。");
                    break;
                default: ShowPage(HomePage()); break;
            }
        }
        catch (Exception error)
        {
            ShowPage(Ui.Page("无法打开页面", error.Message));
            Notice("无法打开页面", error.Message, InfoBarSeverity.Error);
        }
    }

    private void ShowPage(FrameworkElement page) => _content.Content = ScrollPage(page);

    private static ScrollViewer ScrollPage(FrameworkElement page) => new()
    {
        Content = page,
        HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        HorizontalContentAlignment = HorizontalAlignment.Stretch
    };

    private void Notice(string title, string message, InfoBarSeverity severity = InfoBarSeverity.Informational)
    {
        _notice.Title = title;
        _notice.Message = message;
        _notice.Severity = severity;
        _notice.IsOpen = true;
    }

    private async Task RunAsync(Func<Task> action)
    {
        if (!_initialized || _busy || _closing) return;
        _busy = true;
        _navigation.IsEnabled = false;
        _content.IsEnabled = false;
        try { await action(); }
        catch (Exception error) { Notice("操作失败", error.Message, InfoBarSeverity.Error); }
        finally
        {
            _busy = false;
            if (_initialized && !_closing) { _navigation.IsEnabled = true; _content.IsEnabled = true; }
        }
    }

    private async Task EnableAsync(PluginSnapshot package)
    {
        var manifest = package.Manifest!;
        var approve = false;
        if (!package.IsTrusted)
        {
            var dialog = new ContentDialog
            {
                XamlRoot = _root.XamlRoot,
                Title = $"信任并启用 {manifest.Name}？",
                Content = $"{manifest.Id}\n{package.PackagePath}\n\n插件是与工作台同进程运行的可信代码，可访问当前用户的文件和系统权限。能力说明并非权限限制；程序集加载上下文不是安全沙箱。仅信任您审查过的本地代码。",
                PrimaryButtonText = "信任并启用",
                CloseButtonText = "取消",
                DefaultButton = ContentDialogButton.Close
            };
            approve = await dialog.ShowAsync() == ContentDialogResult.Primary;
            if (!approve) return;
        }
        var result = await Manager.EnableAsync(manifest.Id, confirmTrust: approve);
        RefreshNavigation();
        Navigate(new("plugins"));
        Notice(result.Succeeded ? "插件已启用" : "启用失败", result.Message,
            result.Succeeded ? InfoBarSeverity.Success : InfoBarSeverity.Error);
    }

    private async Task DisableAsync(string pluginId)
    {
        // No page cache: release content and navigation entries before plugin StopAsync runs.
        DetachPluginViews();
        var result = await Manager.DisableAsync(pluginId);
        RefreshNavigation();
        Navigate(new("plugins"));
        Notice(result.Succeeded ? "插件已停用" : "插件停止发生错误", result.Message,
            result.Succeeded ? InfoBarSeverity.Success : InfoBarSeverity.Error);
    }

    private void DetachPluginViews()
    {
        _content.Content = null;
        _refreshing = true;
        _navigation.MenuItems.Clear();
        _navigation.SelectedItem = null;
        _refreshing = false;
    }

    private void OnClosing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (_allowClose) return;
        args.Cancel = true;
        if (_closing) return;
        _ = CloseAsync();
    }

    private async Task StopAsync()
    {
        if (_stopped) return;
        _initialized = false;
        DetachPluginViews();
        _body.Content = ScrollPage(Ui.Page("正在关闭", "正在等待插件停止。协作式取消无法强制中断不响应的插件。"));
        _navigation.IsEnabled = false;
        if (_runtime is not null)
        {
            await Manager.ShutdownAsync(); // Preserve desired-enabled preferences on application exit.
            await _runtime.DisposeAsync();
        }
        _stopped = true;
    }

    private async Task CloseAsync()
    {
        if (_closing) return;
        _closing = true;
        try
        {
            await StopAsync();
            _allowClose = true;
            Close();
            _closeCompleted.TrySetResult();
        }
        catch (Exception error)
        {
            Environment.ExitCode = 1;
            _options.WriteFailure("shutdown", error);
            Notice("关闭失败", error.Message, InfoBarSeverity.Error);
            _closing = false;
            _closeCompleted.TrySetException(error);
        }
    }

    internal void AssertUiThread()
    {
        if (!DispatcherQueue.HasThreadAccess || Environment.CurrentManagedThreadId != _uiThread)
            throw new InvalidOperationException("Native factory did not run on the WinUI UI thread.");
    }

    internal PluginManager SmokeManager => Manager;
    internal Exception? SmokeStartupFailure => _startupFailure;
    internal bool SmokeNavigationAvailable => _navigation.IsEnabled || _navigation.IsSettingsVisible;
    internal void SmokeTryStartupNavigation()
    {
        _navigation.SelectedItem = _navigation.SettingsItem;
        Navigate(new("settings"));
        Navigate(new("home"));
    }
    internal void SmokeDetach() => DetachPluginViews();
    internal void SmokeSetTheme(ElementTheme theme) { AssertUiThread(); _root.RequestedTheme = theme; }
    internal async Task SmokeAttachAsync(WinUiView view)
    {
        AssertUiThread();
        var element = view.CreateView() ?? throw new InvalidOperationException("Native factory returned null.");
        await AttachSmokeElementAsync(element);
    }

    private async Task AttachSmokeElementAsync(FrameworkElement element)
    {
        var loaded = new TaskCompletionSource();
        void OnLoaded(object sender, RoutedEventArgs args) => loaded.TrySetResult();
        element.Loaded += OnLoaded;
        try
        {
            _content.Content = element;
            await loaded.Task.WaitAsync(TimeSpan.FromSeconds(15));
            if (element.XamlRoot is null) throw new InvalidOperationException("Native view was not attached to the host XamlRoot.");
        }
        finally { element.Loaded -= OnLoaded; }
    }

    internal async Task SmokeVerifyLiveCardThemesAsync()
    {
        AssertUiThread();
        var card = Ui.Card(Ui.Text("Live host card theme verification"));
        await AttachSmokeElementAsync(card);
        async Task<(Windows.UI.Color Fill, Windows.UI.Color Stroke)> ReadThemeAsync(ElementTheme theme)
        {
            _root.RequestedTheme = theme;
            var settled = new TaskCompletionSource();
            if (!DispatcherQueue.TryEnqueue(Microsoft.UI.Dispatching.DispatcherQueuePriority.Low, () => settled.TrySetResult()))
                throw new InvalidOperationException("Could not queue live theme verification.");
            await settled.Task.WaitAsync(TimeSpan.FromSeconds(15));
            card.UpdateLayout();
            if (card.ActualTheme != theme)
                throw new InvalidOperationException($"Card actual theme did not become {theme}.");
            if (card.Background is not Microsoft.UI.Xaml.Media.SolidColorBrush fill
                || card.BorderBrush is not Microsoft.UI.Xaml.Media.SolidColorBrush stroke)
                throw new InvalidOperationException("Card theme brushes were not resolved.");
            return (fill.Color, stroke.Color);
        }
        var light = await ReadThemeAsync(ElementTheme.Light);
        var dark = await ReadThemeAsync(ElementTheme.Dark);
        var lightAgain = await ReadThemeAsync(ElementTheme.Light);
        if (light.Fill.Equals(dark.Fill) || light.Stroke.Equals(dark.Stroke) || !light.Equals(lightAgain))
            throw new InvalidOperationException("The same host card did not re-resolve fill/stroke across Light/Dark/Light.");
        _root.RequestedTheme = ElementTheme.Default;
    }

    internal Task SmokeStopAsync() => StopAsync();
    internal async Task FinishSmokeAsync()
    {
        if (!_closing) await CloseAsync();
        else await _closeCompleted.Task;
    }

    private sealed record NavigationTarget(string Page, string? PluginId = null, string? ViewId = null);
}
