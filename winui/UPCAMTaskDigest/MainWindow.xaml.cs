using Microsoft.UI.Dispatching;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using System.Text.Json.Nodes;
using Windows.Graphics;

namespace Upcam.Desktop;

public sealed partial class MainWindow : WinUIEx.WindowEx
{
    private readonly DesktopState _state = new();
    private readonly bool _smoke;
    private readonly bool _background;
    private readonly bool _afterUpdate;
    private WorkerClient? _worker;
    private NativeDesktop? _native;
    private DispatcherQueueTimer? _scheduler;
    private bool _initialized, _busy, _exitRequested, _savedAuto, _navigating;
    private bool _exitInProgress, _ready;
    private readonly SemaphoreSlim _dialogGate = new(1, 1);
    private string _page = "home";
    private readonly Dictionary<string, (string Title, string Description)> _routes = new()
    {
        ["home"] = ("主页", "站会日报 · 本机自动化"),
        ["general"] = ("常规", "管理应用更新、语言、主题、启动和系统托盘。"),
        ["accounts"] = ("账户与模型", "连接服务、保管凭据，选择 API 模型与请求设置。"),
        ["project"] = ("项目范围", "按项目真实更新时间筛选任务，并核验只读接口。"),
        ["members"] = ("成员映射", "核对 API 成员 ID 与姓名，支持 JSON / 制表符批量导入。"),
        ["statuses"] = ("状态映射", "核对状态名称，并明确哪些状态代表已解决。"),
        ["schedule"] = ("自动运行", "设置日报运行时间和采集窗口；应用启动选项位于常规页。"),
        ["run"] = ("运行与诊断", "手动可再次发送同一时段；定时任务防重，结果不明时停止。"),
        ["history"] = ("运行记录", "查看本机运行状态、诊断说明和已生成日报。")
    };

    internal MainWindow(bool smoke, bool background, bool afterUpdate = false)
    {
        _smoke = smoke;
        _background = background;
        _afterUpdate = afterUpdate;
        InitializeComponent();
        Title = "UPCAM 站会日报 · v" + typeof(MainWindow).Assembly.GetName().Version?.ToString(3) +
            (smoke ? " · CI 虚构数据" : "");
        AppTitleBar.Title = Title;
        ExtendsContentIntoTitleBar = true;
        SetTitleBar(AppTitleBar);
        AppWindow.TitleBar.PreferredHeightOption = TitleBarHeightOption.Tall;
        AppWindow.TitleBar.ButtonBackgroundColor = Microsoft.UI.Colors.Transparent;
        AppWindow.TitleBar.ButtonInactiveBackgroundColor = Microsoft.UI.Colors.Transparent;
        AppWindow.TitleBar.ButtonForegroundColor = Microsoft.UI.Colors.White;
        AppWindow.TitleBar.ButtonInactiveForegroundColor = Windows.UI.Color.FromArgb(255, 153, 153, 153);
        AppWindow.TitleBar.ButtonHoverBackgroundColor = Windows.UI.Color.FromArgb(255, 58, 58, 58);
        AppWindow.TitleBar.ButtonHoverForegroundColor = Microsoft.UI.Colors.White;
        AppWindow.Resize(new SizeInt32(1180, 800));
        AppWindow.Closing += Window_Closing;
        RootGrid.ActualThemeChanged += (_, _) => UpdateCaptionTheme();
        Closed += (_, _) =>
        {
            _scheduler?.Stop();
            _updateTimer?.Stop();
            _native?.Dispose();
            _worker?.Dispose();
        };
        RootGrid.Loaded += Window_Loaded;
        SaveButton.IsEnabled = false;
    }

    private async void Window_Loaded(object sender, RoutedEventArgs e)
    {
        if (_initialized) return;
        _initialized = true;
        try
        {
            _native = new NativeDesktop(this, ShowMainWindow, () => _ = RequestExitAsync(), HandleCloseRequest);
            _worker = new WorkerClient(_smoke);
            ApplyInitializationResult(await _worker.SendAsync("initialize"));
            _savedAuto = _state.Flag("auto_send_enabled");
            ApplyGeneralAppearance();
            SaveButton.IsEnabled = true;
            Navigate("home");
            SetStatus(_smoke ? "CI 虚构数据模式：所有服务均为模拟，不会访问生产接口。" : _state.LedgerMessage);
            if (_afterUpdate && !_smoke)
                SetStatus(Value(await Worker.SendAsync("finish_update"), "message"));
            if (_background || _state.GeneralFlag("start_in_tray"))
            {
                if (_native.TrayReady) AppWindow.Hide();
                else SetStatus("系统托盘暂不可用，已保留主窗口。");
            }
            if (_smoke)
            {
                await RunSmokeAsync();
                return;
            }
            _scheduler = DispatcherQueue.CreateTimer();
            _scheduler.Interval = TimeSpan.FromSeconds(15);
            _scheduler.Tick += async (_, _) =>
            {
                if (_busy || !_savedAuto) return;
                await RunOperationAsync("正在检查已保存的自动运行计划…", async () =>
                {
                    var result = await Worker.SendAsync("run_due");
                    if (result["events"] is JsonArray events && events.Count > 0)
                    {
                        _state.Output = string.Join("\n", events.Select(x => $"{x?["schedule_key"]}: {x?["message"]}"));
                        SetStatus(_state.Output);
                        await RefreshHistoryAsync();
                    }
                    else SetStatus("自动运行已启用；应用正在系统托盘或主窗口运行。");
                }, showErrorDialog: false);
            };
            _scheduler.Start();
            StartUpdateChecks();
        }
        catch (Exception ex)
        {
            DisableWorkerOperations();
            if (_smoke) { FinishSmoke(false, ex.ToString()); return; }
            SetStatus("应用初始化失败；本机原有配置没有删除。");
            await ShowMessageAsync("无法初始化", "请确认已完整解压应用、worker 文件夹和运行时文件；如果旧版仍在托盘中运行，请先退出旧版。");
        }
    }

    private void ApplyInitializationResult(JsonObject result)
    {
        if (result["application_version"]?.GetValue<string>() != typeof(MainWindow).Assembly.GetName().Version?.ToString(3))
        {
            DisableWorkerOperations();
            throw new InvalidOperationException("应用界面与工作进程版本不一致，请完整解压同一版本。");
        }
        _state.Apply(result);
        _ready = true;
        Navigation.IsEnabled = true;
        PageHost.IsEnabled = true;
        SaveButton.IsEnabled = true;
    }

    private void DisableWorkerOperations()
    {
        _ready = false;
        _savedAuto = false;
        _scheduler?.Stop();
        _worker?.Dispose();
        _worker = null;
        Navigation.IsEnabled = false;
        PageHost.IsEnabled = false;
        SaveButton.IsEnabled = false;
    }

    private WorkerClient Worker => _ready && _worker is not null ? _worker :
        throw new InvalidOperationException("本机工作进程尚未就绪或版本不兼容，请完整解压同一版本并重新启动。");

    private void TitleBar_PaneToggleRequested(TitleBar sender, object args) =>
        Navigation.IsPaneOpen = !Navigation.IsPaneOpen;

    private void Navigation_SelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (!_initialized || !_ready || _worker is null || _navigating) return;
        if (args.SelectedItem is NavigationViewItem item && item.Tag is string route) Navigate(route);
    }

    private void Navigate(string route)
    {
        if (!_routes.TryGetValue(route, out var info)) return;
        _page = route;
        PageTitle.Text = UiStrings.T(info.Title);
        PageDescription.Text = UiStrings.T(info.Description);
        SaveButton.Content = UiStrings.T(route == "general" ? "保存常规设置" : "保存本机配置");
        PageHost.Content = route switch
        {
            "accounts" => BuildAccounts(),
            "general" => BuildGeneral(),
            "project" => BuildProject(),
            "members" => BuildMembers(),
            "statuses" => BuildStatuses(),
            "schedule" => BuildSchedule(),
            "run" => BuildRun(),
            "history" => BuildHistory(),
            _ => BuildHome()
        };
        _navigating = true;
        Navigation.SelectedItem = Navigation.MenuItems.OfType<NavigationViewItem>().FirstOrDefault(x => (string?)x.Tag == route);
        _navigating = false;
    }

    private async void SaveButton_Click(object sender, RoutedEventArgs e) =>
        await RunOperationAsync(UiStrings.T("正在保存本机配置…"), _page == "general" ? SaveGeneralCoreAsync : SaveCoreAsync);

    private async Task SaveCoreAsync()
    {
        JsonObject result = await Worker.SendAsync("save", _state.SavePayload());
        _state.Apply(result);
        _state.Apply(await Worker.SendAsync("initialize"));
        _savedAuto = _state.Flag("auto_send_enabled");
        Navigate(_page);
        SetStatus(result["status"]?.GetValue<string>() ?? "已保存本机配置。");
    }

    private async Task EnsureSavedAsync()
    {
        if (_state.Dirty || _state.SecretDrafts.Any(x => !string.IsNullOrEmpty(x.Value))) await SaveCoreAsync();
    }

    private async Task<bool> RunOperationAsync(string status, Func<Task> action, bool showErrorDialog = true)
    {
        if (!_ready) { SetStatus("工作进程未就绪或版本不兼容，请完整解压同一版本并重新启动。"); return false; }
        if (_busy) { SetStatus("已有操作正在运行，请等待完成。"); return false; }
        _busy = true;
        SaveButton.IsEnabled = false;
        PageHost.IsEnabled = false;
        BusyRing.Visibility = Visibility.Visible;
        BusyRing.IsActive = true;
        SetStatus(status);
        try { await action(); return true; }
        catch (Exception ex)
        {
            string message = ex is InvalidOperationException ? ex.Message : "操作未完成；详细 API 响应和凭据没有写入日志。";
            _state.Output = message;
            SetStatus(message);
            if (_smoke) throw;
            if (showErrorDialog) await ShowMessageAsync("操作未完成", message);
            return false;
        }
        finally
        {
            _busy = false;
            SaveButton.IsEnabled = _ready;
            PageHost.IsEnabled = _ready;
            BusyRing.IsActive = false;
            BusyRing.Visibility = Visibility.Collapsed;
        }
    }

    private void SetStatus(string message) => StatusText.Text = UiStrings.T(message);

    private async Task RefreshHistoryAsync()
    {
        var result = await Worker.SendAsync("history");
        _state.History = result["runs"]?.DeepClone().AsArray() ?? new JsonArray();
        if (_page == "history") Navigate("history");
    }

    private async Task<ContentDialogResult> ShowMessageAsync(string title, string text, string? primary = null)
    {
        var dialog = new ContentDialog
        {
            XamlRoot = RootGrid.XamlRoot, RequestedTheme = RootGrid.RequestedTheme,
            Title = UiStrings.T(title), CloseButtonText = UiStrings.T(primary is null ? "知道了" : "取消"),
            PrimaryButtonText = UiStrings.T(primary ?? ""),
            DefaultButton = primary is null ? ContentDialogButton.Close : ContentDialogButton.Primary,
            Content = new ScrollViewer { MaxHeight = 360, Content = Paragraph(UiStrings.T(text)) }
        };
        return await ShowDialogAsync(dialog);
    }

    private async Task<ContentDialogResult> ShowDialogAsync(ContentDialog dialog)
    {
        await _dialogGate.WaitAsync();
        try
        {
            if (_exitRequested) return ContentDialogResult.None;
            dialog.RequestedTheme = RootGrid.RequestedTheme;
            return await dialog.ShowAsync();
        }
        finally { _dialogGate.Release(); }
    }

    private void ShowMainWindow()
    {
        if (AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized } presenter)
            presenter.Restore();
        AppWindow.Show();
        Activate();
    }

    private void Window_Closing(AppWindow sender, AppWindowClosingEventArgs args)
    {
        if (_exitRequested) return;
        args.Cancel = HandleCloseRequest();
    }

    private bool HandleCloseRequest()
    {
        if (_exitRequested) return false;
        if (_native?.TrayReady == true && _state.Flag("close_to_tray"))
        {
            AppWindow.Hide();
        }
        else
        {
            _ = RequestExitAsync();
        }
        return true;
    }

    private async Task RequestExitAsync()
    {
        if (_exitRequested || _exitInProgress) return;
        _exitInProgress = true;
        try
        {
            ShowMainWindow();
            if (_busy || _state.Dirty || HasUnsavedGeneral || _state.SecretDrafts.Any(x => !string.IsNullOrEmpty(x.Value)))
            {
                string note = _busy
                    ? "当前操作仍在运行。退出会停止工作进程；若正在发送，结果将保留为不明并阻止自动重发。"
                    : "有尚未保存的设置或凭据。退出后这些编辑不会保留。";
                if (await ShowMessageAsync("退出应用", note, "退出") != ContentDialogResult.Primary) return;
            }
            _exitRequested = true;
            Close();
        }
        finally { _exitInProgress = false; }
    }
}
