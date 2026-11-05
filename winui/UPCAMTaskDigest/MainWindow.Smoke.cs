using Microsoft.UI.Xaml;
using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Automation.Peers;
using Microsoft.UI.Xaml.Automation.Provider;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using System.Text.Json;
using System.Text.Json.Nodes;
using Windows.Foundation;
using Windows.Graphics;
using Windows.Graphics.Imaging;
using Windows.Storage;
using Windows.Storage.Streams;

namespace Upcam.Desktop;

public sealed partial class MainWindow
{
    private readonly List<string> _smokeSteps = new();
    private readonly List<object> _smokeLayouts = new();
    private readonly List<object> _smokeIcons = new();

    private async Task RunSmokeAsync()
    {
        try
        {
            ShowMainWindow();
            string output = Path.Combine(AppContext.BaseDirectory, "smoke-output");
            Directory.CreateDirectory(output);
            try
            {
                ApplyInitializationResult(new JsonObject { ["application_version"] = "0.0.0" });
                throw new InvalidOperationException("Incompatible initialization unexpectedly succeeded");
            }
            catch (InvalidOperationException ex) when (ex.Message.Contains("版本不一致")) { }
            Assert(!_ready && _worker is null && !Navigation.IsEnabled && !SaveButton.IsEnabled && !PageHost.IsEnabled,
                "Incompatible worker is disposed and all business controls are disabled");
            bool incompatibleActionInvoked = false;
            bool accepted = await RunOperationAsync("Synthetic incompatible operation", () =>
            {
                incompatibleActionInvoked = true;
                return Task.CompletedTask;
            });
            Assert(!accepted && !incompatibleActionInvoked,
                "Manual action cannot run after incompatible initialization");
            _worker = new WorkerClient(_smoke);
            ApplyInitializationResult(await _worker.SendAsync("initialize"));
            Assert(_ready && Navigation.IsEnabled && PageHost.IsEnabled && SaveButton.IsEnabled,
                "Compatible synthetic worker restores native operation state");
            var titleBar = Find<TitleBar>("app-titlebar");
            Assert(titleBar is not null && ExtendsContentIntoTitleBar, "Native custom TitleBar replaces colored system title bar");
            Assert(titleBar!.IconSource is BitmapIconSource { ShowAsMonochrome: false }, "TitleBar keeps original icon colors");
            Assert(AppWindow.TitleBar.ButtonBackgroundColor == Microsoft.UI.Colors.Transparent,
                "Native caption button background blends with dark title bar");
            foreach (uint dpi in new uint[] { 96, 144, 192 })
            {
                var dimensions = _native!.IconSizeForDpi(dpi);
                Assert(dimensions.Width == 16 * dpi / 96 && dimensions.Height == dimensions.Width,
                    $"Native tray icon selects exact {dpi} DPI dimensions");
                _smokeIcons.Add(new { dpi, width = dimensions.Width, height = dimensions.Height });
            }
            Assert(_native!.WindowIconHandle != IntPtr.Zero && _native.TrayIconHandle != IntPtr.Zero,
                "Window and tray both contain actual native icon handles");
            await NativeUiEvidence.CaptureIconAsync(_native.WindowIconHandle, 32, Path.Combine(output, "window-icon.png"));
            await NativeUiEvidence.CaptureIconAsync(_native.TrayIconHandle, 32, Path.Combine(output, "tray-icon.png"));
            Assert(NativeUiEvidence.IconPixels(_native.WindowIconHandle, 16).SequenceEqual(
                NativeUiEvidence.IconPixels(_native.TrayIconHandle, 16)), "Actual window and tray icon pixels match");
            _native.RequestSystemCommand(0xF030);
            await WaitForAsync(() => AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Maximized });
            Assert(true, "Native maximize system command remains functional");
            _native.RequestSystemCommand(0xF120);
            await WaitForAsync(() => AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Restored });
            Assert(true, "Native restore system command remains functional");
            _native.RequestSystemCommand(0xF020);
            await WaitForAsync(() => AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Minimized });
            ShowMainWindow();
            await WaitForAsync(() => AppWindow.Presenter is OverlappedPresenter { State: OverlappedPresenterState.Restored });
            Assert(true, "Tray show restores a minimized native window");
            await VerifyUtf8PipeBoundariesAsync();
            _state.ManualStart = DateTime.Now.AddHours(-2).ToString("yyyy-MM-dd HH:mm");
            _state.ManualEnd = DateTime.Now.ToString("yyyy-MM-dd HH:mm");
            var models = await Worker.SendAsync("refresh_models");
            _state.ApplyModels(models);
            Assert(_state.Models.Count == 2, "Synthetic model refresh and IPC");
            Navigate("members");
            Invoke(Find<Button>("member-import")!);
            await WaitForAsync(() => Find<TextBox>("member-import-text") is not null);
            Find<TextBox>("member-import-text")!.Text = "{\"ci-member\":\"CI 虚构成员\"}";
            Invoke(await WaitForPrimaryAsync());
            await WaitForAsync(() => !_busy);
            Assert(_state.Map("owner_names")["ci-member"]?.GetValue<string>() == "CI 虚构成员", "Native JSON import dialog");

            Navigate("statuses");
            Invoke(Find<Button>("status-add")!);
            await WaitForAsync(() => Find<TextBox>("status-id-input") is not null);
            Find<TextBox>("status-id-input")!.Text = "ci-status";
            Find<TextBox>("status-name-input")!.Text = "CI 虚构状态";
            Invoke(await WaitForPrimaryAsync());
            await WaitForAsync(() => !_busy);
            Assert(_state.Map("status_names")["ci-status"]?.GetValue<string>() == "CI 虚构状态", "Native status editor dialog");

            Navigate("project");
            _state.Set("task_number_prefix", "STALE");
            _state.Set("task_link_view_id", new string('e', 24));
            _state.Set("project_id", "ci-another-project");
            Assert(_state.Text("task_number_prefix") == "" && _state.Text("task_link_view_id") == "" &&
                _state.Settings["task_display_scope"] is null,
                "Changing source project clears prior project display preferences");
            _state.Set("project_id", "demo-project");
            var projectTabs = Find<TabView>("project-tabs")!;
            projectTabs.SelectedIndex = 1;
            await WaitForAsync(() => Find<TextBox>("task-number-prefix") is not null);
            var prefixInput = Find<TextBox>("task-number-prefix")!;
            var viewInput = Find<TextBox>("task-link-view")!;
            prefixInput.Text = "DEMO";
            viewInput.Text = "https://www.teambition.com/project/demo-project/tasks/view/" + new string('f', 24);
            Assert(_state.Text("task_number_prefix") == "DEMO" &&
                _state.Text("task_link_view_id") == viewInput.Text,
                "Display text edits reach the saved state synchronously before switching tabs");
            projectTabs.SelectedIndex = 0;
            Assert(Find<CheckBox>("confirm-scope") is null, "Project page has no scope confirmation checkbox");
            JsonObject savePayload = _state.SavePayload();
            Assert(!savePayload.ContainsKey("scope_confirmed") &&
                savePayload["verification_window"]?["start"]?.GetValue<string>() == _state.ManualStart &&
                savePayload["verification_window"]?["end"]?.GetValue<string>() == _state.ManualEnd,
                "Save supplies the current verification window without a manual confirmation field");
            Invoke(SaveButton);
            await WaitForAsync(() => !_busy);
            Assert(_state.ScopeVerified && _state.Flag("api_contract_verified"),
                "Save automatically verifies the scope with a real read-only synthetic query");
            Assert(_state.Text("task_number_prefix") == "DEMO" && _state.Text("task_link_view_id") == new string('f', 24),
                "Native task display settings save confirmed prefix and normalize the all-view URL");
            prefixInput.Text = "STALE";
            viewInput.Text = new string('e', 24);
            Assert(_state.Text("task_number_prefix") == "DEMO" && _state.Text("task_link_view_id") == new string('f', 24),
                "Detached settings controls cannot overwrite reloaded saved preferences");
            Assert(_state.CandidateOwners.Contains("demo-person") && _state.CandidateStatuses.Contains("demo-open"),
                "Automatic save also discovers mapping IDs");
            Assert(_state.Number("schema_version", 0) == 1 && !_state.Flag("auto_send_enabled") && _state.History.Count == 0,
                "Automatic scope validation preserves legacy settings and does not send");
            Invoke(Find<Button>("probe-project")!);
            await WaitForAsync(() => !_busy);
            Assert(_state.CandidateOwners.Contains("demo-person"), "Read-only synthetic project probe");

            Task<ContentDialogResult> firstDialog = ShowMessageAsync("CI 对话框一", "虚构并发提示", "继续");
            Button firstPrimary = await WaitForPrimaryAsync();
            Task<ContentDialogResult> queuedDialog = ShowMessageAsync("CI 对话框二", "虚构排队提示", "继续");
            Invoke(firstPrimary);
            await firstDialog;
            Invoke(await WaitForPrimaryAsync());
            await queuedDialog;
            Assert(true, "Native dialog serialization prevents reentrant failures");

            Navigate("run");
            Invoke(Find<Button>("preview-sample")!);
            await WaitForAsync(() => !_busy);
            Assert(_state.Output.Contains("虚构示例"), "Repository template preview button");
            Invoke(Find<Button>("diagnose")!);
            await WaitForAsync(() => !_busy);
            Assert(_state.ExportAvailable && _state.Output.Contains("通过"), "Read-only diagnostic button and safe export availability");
            Assert(_state.Output.Contains("窗口内未支持动作：0 条") && _state.Output.Contains("时间归属无法判断的未支持动作：0 条"),
                "Native diagnostics distinguish unsupported actions from unknown time membership");
            string previousDiagnostic = _state.DiagnosticText;
            Invoke(Find<Button>("export-detailed-diagnostic")!);
            await WaitForAsync(() => Find<ContentDialog>("detailed-export-consent") is not null);
            var detailedDialog = Find<ContentDialog>("detailed-export-consent")!;
            Assert(detailedDialog.DefaultButton == ContentDialogButton.Close &&
                detailedDialog.Title.ToString()!.Contains("项目内容"), "Native detailed export discloses project content and defaults to cancel");
            detailedDialog.Hide();
            await WaitForAsync(() => !_busy);
            Assert(_state.DiagnosticText == previousDiagnostic, "Canceling detailed export preserves the safe diagnostic");
            string privateTemporary = Path.Combine(Path.GetTempPath(), "upcam-detailed-ci-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(privateTemporary);
            try
            {
                string detailPath = Path.Combine(privateTemporary, "synthetic-detailed.json");
                var detailPayload = _state.WindowPayload();
                detailPayload["path"] = detailPath;
                detailPayload["include_project_content"] = true;
                var detailResult = await Worker.SendAsync("export_detailed_diagnostic_to_file", detailPayload);
                string detailText = File.ReadAllText(detailPath);
                Assert(detailResult["detailed_export"]?["saved"]?.GetValue<bool>() == true &&
                    detailText.Contains("CI 虚构") && !detailText.Contains("synthetic-app-secret") &&
                    !detailText.Contains("synthetic-key-cannot-call-openai") && !detailText.Contains("access_token=synthetic-only"),
                    "Packaged worker exports synthetic project evidence without configured credentials");
                Assert(detailResult["diagnostic_summary"]?["unknown_events_in_report_window"]?.GetValue<int>() == 0 &&
                    JsonNode.Parse(detailText)?["sendable"]?.GetValue<bool>() == false,
                    "Detailed snapshot remains diagnostic-only with explicit counters");
            }
            finally { Directory.Delete(privateTemporary, true); }
            var clipboardOptions = DiagnosticClipboardOptions();
            Assert(!clipboardOptions.IsAllowedInHistory && !clipboardOptions.IsRoamable,
                "Diagnostic clipboard options disable history and device roaming");
            Invoke(Find<Button>("copy-diagnostic")!);
            await WaitForAsync(() => !_busy);
            string copiedDiagnostic = await Windows.ApplicationModel.DataTransfer.Clipboard.GetContent().GetTextAsync();
            Assert(copiedDiagnostic == _state.DiagnosticText && copiedDiagnostic.Contains("只读校验通过"),
                "Native copy button copies only the current local diagnostic");
            Assert(Title.Contains("v" + _state.ApplicationVersion) &&
                _state.ApplicationVersion == typeof(MainWindow).Assembly.GetName().Version?.ToString(3),
                "Visible native application and worker versions match");
            Assert(_state.DiagnosticFileStem.StartsWith("teambition-diagnostic-v" + _state.ApplicationVersion),
                "Diagnostic export suggests versioned unique filename");
            string diagnosticPath = Path.Combine(output, _state.DiagnosticFileStem + ".json");
            await Worker.SendAsync("export_diagnostic_to_file", _state.DiagnosticExportPayload(diagnosticPath));
            JsonNode exported = JsonNode.Parse(File.ReadAllText(diagnosticPath))!;
            Assert(exported["diagnostic"]?["application_version"]?.GetValue<string>() == _state.ApplicationVersion &&
                exported["diagnostic"]?["report_window"]?["end_exclusive"] is not null,
                "Native export writes actual version and explicit report window");
            Assert(exported["format"]?.GetValue<string>() == "upcam-teambition-safe-shape-v5" &&
                exported["tasks"]?[0]?["activities"]?[0]?["action_fingerprint"]?["sha256"] is not null,
                "Native exported diagnostic contains protocol fingerprints");
            string previousStart = _state.ManualStart;
            _state.ManualStart = DateTime.ParseExact(previousStart, "yyyy-MM-dd HH:mm", null)
                .AddMinutes(-1).ToString("yyyy-MM-dd HH:mm");
            Assert(!_state.ExportAvailable, "Changing report window invalidates cached export in native state");
            Navigate("run");
            Assert(!Find<Button>("export-diagnostic")!.IsEnabled, "Stale export button is disabled");
            Assert(!Find<Button>("copy-diagnostic")!.IsEnabled && _state.DiagnosticText == "",
                "Stale diagnostic copy is disabled after window changes");
            _state.ManualStart = previousStart;
            Invoke(Find<Button>("run-manual")!);
            await WaitForAsync(() => !_busy);
            Assert(_state.History.Count == 1, "Synthetic workflow and local history");
            Assert(_state.Output.Contains("DEMO\\-101") && _state.Output.Contains("🕒 时间范围") &&
                _state.Output.Contains("📊 更新 1 项：工作进展 1 项") &&
                _state.Output.Contains("/tasks/view/" + new string('f', 24) + "/task/ci-synthetic-task"),
                "Packaged formal report uses business number, actual time range, progress category and all-view route");
            Assert(!_state.Output.Contains("评论 1 条") && !_state.Output.Contains("其他变更 3 条") &&
                !_state.Output.Contains("任务位置变更") && !_state.Output.Contains("未核实") &&
                !_state.Output.Contains("CI 虚构通知内容") && !_state.Output.Contains("ci-unverified-account"),
                "Native formal report excludes diagnostic prose while retaining validated source evidence locally");

            foreach (var size in new[] { new SizeInt32(1180, 800), new SizeInt32(900, 700), new SizeInt32(760, 620), new SizeInt32(640, 540) })
            {
                AppWindow.Resize(size);
                await Task.Delay(150);
                RootGrid.UpdateLayout();
                Navigation.IsPaneOpen = true;
                RootGrid.UpdateLayout();
                Assert(Navigation.PaneDisplayMode == NavigationViewPaneDisplayMode.Left &&
                    Navigation.DisplayMode == NavigationViewDisplayMode.Expanded, "Navigation uses inline pane at " + size.Width);
                Rect pageBounds = PageHost.TransformToVisual(RootGrid).TransformBounds(new Rect(0, 0, PageHost.ActualWidth, PageHost.ActualHeight));
                Assert(pageBounds.Left >= Navigation.OpenPaneLength, "Expanded navigation reserves content space at " + size.Width);
                Button? toggle = FindIn<Button>(AppTitleBar, x => x is FrameworkElement fe && fe.Name == "PART_PaneToggleButton");
                Assert(toggle is not null, "Native title bar pane button exists");
                Invoke(toggle!);
                await Task.Delay(100);
                Assert(!Navigation.IsPaneOpen, "Native title bar pane button collapses navigation");
                Invoke(toggle!);
                await Task.Delay(100);
                Assert(Navigation.IsPaneOpen, "Native title bar pane button expands inline navigation");
                _smokeLayouts.Add(new
                {
                    requestedWidthPixels = size.Width, requestedHeightPixels = size.Height,
                    actualWidthPixels = AppWindow.Size.Width, actualHeightPixels = AppWindow.Size.Height,
                    widthDip = RootGrid.ActualWidth, heightDip = RootGrid.ActualHeight,
                    rasterizationScale = RootGrid.XamlRoot.RasterizationScale
                });
                foreach (string route in _routes.Keys)
                {
                    Navigate(route);
                    await Task.Delay(60);
                    RootGrid.UpdateLayout();
                    Assert(PageHost.ActualWidth > 300 && PageHost.ActualHeight > 180, $"Layout {route} {size.Width}x{size.Height}");
                    if (route == "statuses")
                    {
                        var list = Find<ListView>("status-list")!;
                        Assert(list.ActualHeight > PageHost.ActualHeight * 0.40, $"Status list fills content {size.Width}x{size.Height}");
                        foreach (string id in new[] { "status-add", "status-edit", "status-delete" })
                            AssertInside(Find<Button>(id)!, "Status toolbar " + id);
                    }
                    if (route == "run")
                    {
                        var textbox = Find<TextBox>("diagnostic-output")!;
                        string NormalizeLines(string value) => value.Replace("\r\n", "\n").Replace("\r", "\n");
                        Assert(NormalizeLines(textbox.Text) == NormalizeLines(_state.Output),
                            "Diagnostic output preserves all source lines");
                        Assert(textbox.ActualHeight >= 60, $"Diagnostic result has usable space {size.Width}x{size.Height}");
                        AssertInside(textbox, "Diagnostic output");
                        foreach (string id in new[] { "diagnose", "run-manual", "preview-sample", "export-diagnostic", "copy-diagnostic", "export-detailed-diagnostic" })
                            AssertInside(Find<Button>(id)!, "Run toolbar " + id);
                    }
                }
            }
            AppWindow.Resize(new SizeInt32(1180, 800));
            foreach (string route in new[] { "home", "statuses", "run" })
            {
                Navigate(route); await Task.Delay(200);
                await CaptureAsync(Path.Combine(output, route + ".png"));
            }
            await NativeUiEvidence.CaptureWindowAsync(WinRT.Interop.WindowNative.GetWindowHandle(this), Path.Combine(output, "native-window.png"));
            await NativeUiEvidence.CaptureIconAsync(_native!.WindowIconHandle, 32, Path.Combine(output, "window-icon.png"));
            await NativeUiEvidence.CaptureIconAsync(_native.TrayIconHandle, 32, Path.Combine(output, "tray-icon.png"));
            if (_native?.TrayReady == true)
            {
                _native.RequestCaptionClose();
                await Task.Delay(100);
                Assert(!AppWindow.IsVisible, "Close hides to native tray");
                ShowMainWindow();
                Assert(AppWindow.IsVisible, "Tray show restores main window");
            }
            File.WriteAllText(Path.Combine(output, "report.json"), JsonSerializer.Serialize(new
            {
                success = true, framework = "WinUI 3 / Windows App SDK",
                dpiAwareness = "PerMonitorV2", actualRasterizationScale = RootGrid.XamlRoot.RasterizationScale,
                highDpiMonitorAvailable = RootGrid.XamlRoot.RasterizationScale > 1.0,
                trayAvailable = _native?.TrayReady == true,
                syntheticOnly = true, viewportChecks = _smokeLayouts, iconDpiChecks = _smokeIcons,
                nativeWindowCapture = "PrintWindow", matchingNativeIconPixels = true, steps = _smokeSteps
            }, new JsonSerializerOptions { WriteIndented = true }));
            FinishSmoke(true, "passed");
        }
        catch (Exception ex)
        {
            // This mode uses isolated synthetic fixtures only; no production values exist.
            try
            {
                string directory = Path.Combine(AppContext.BaseDirectory, "smoke-output");
                Directory.CreateDirectory(directory);
                await CaptureAsync(Path.Combine(directory, "failure.png"));
            }
            catch { }
            FinishSmoke(false, ex.ToString());
        }
    }

    private void Assert(bool condition, string description)
    {
        if (!condition) throw new InvalidOperationException(description);
        _smokeSteps.Add(description);
    }

    private async Task VerifyUtf8PipeBoundariesAsync()
    {
        foreach (int length in new[] { 4096, 8192 })
        {
            string pipeName = "upcam-ci-" + Guid.NewGuid().ToString("N");
            await using var server = new System.IO.Pipes.NamedPipeServerStream(pipeName,
                System.IO.Pipes.PipeDirection.Out, 1, System.IO.Pipes.PipeTransmissionMode.Byte,
                System.IO.Pipes.PipeOptions.Asynchronous | System.IO.Pipes.PipeOptions.CurrentUserOnly);
            await using var client = new System.IO.Pipes.NamedPipeClientStream(".", pipeName,
                System.IO.Pipes.PipeDirection.In, System.IO.Pipes.PipeOptions.Asynchronous);
            await Task.WhenAll(server.WaitForConnectionAsync(), client.ConnectAsync());
            const string prefix = "{\"id\":\"ci\",\"ok\":true,\"result\":{\"value\":\"中";
            const string suffix = "\"}}\n";
            int padding = length - System.Text.Encoding.UTF8.GetByteCount(prefix + suffix);
            byte[] bytes = System.Text.Encoding.UTF8.GetBytes(prefix + new string('x', padding) + suffix);
            Task<string?> read = WorkerClient.ReadBoundedLineAsync(client);
            await server.WriteAsync(bytes);
            await server.FlushAsync();
            string? line = await read.WaitAsync(TimeSpan.FromSeconds(3));
            Assert(line is not null && JsonNode.Parse(line)?["result"]?["value"]?.GetValue<string>().StartsWith("中") == true,
                $"UTF-8 named pipe line {length} bytes completes before writer closes");
        }
    }

    private void AssertInside(FrameworkElement control, string description)
    {
        Rect bounds = control.TransformToVisual(RootGrid).TransformBounds(new Rect(0, 0, control.ActualWidth, control.ActualHeight));
        Assert(bounds.Left >= -2 && bounds.Top >= -2 && bounds.Right <= RootGrid.ActualWidth + 2 &&
            bounds.Bottom <= RootGrid.ActualHeight + 2, description + " inside window");
    }

    private T? Find<T>(string id) where T : DependencyObject
    {
        RootGrid.UpdateLayout();
        T? found = FindIn<T>(RootGrid, x => AutomationProperties.GetAutomationId(x) == id);
        if (found is not null) return found;
        foreach (var popup in VisualTreeHelper.GetOpenPopupsForXamlRoot(RootGrid.XamlRoot))
            if (popup.Child is not null && FindIn<T>(popup.Child, x => AutomationProperties.GetAutomationId(x) == id) is T match) return match;
        return null;
    }

    private static T? FindIn<T>(DependencyObject root, Func<DependencyObject, bool> predicate) where T : DependencyObject
    {
        if (root is T item && predicate(item)) return item;
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
            if (FindIn<T>(VisualTreeHelper.GetChild(root, i), predicate) is T found) return found;
        return null;
    }

    private async Task<Button> WaitForPrimaryAsync()
    {
        Button? button = null;
        await WaitForAsync(() =>
        {
            foreach (var popup in VisualTreeHelper.GetOpenPopupsForXamlRoot(RootGrid.XamlRoot))
                if (popup.Child is not null)
                    button ??= FindIn<Button>(popup.Child, x => x is FrameworkElement fe && fe.Name == "PrimaryButton");
            return button is not null;
        });
        return button!;
    }

    private static void Invoke(Button button)
    {
        if (button is null) throw new InvalidOperationException("The required native button has not been realized.");
        var peer = new ButtonAutomationPeer(button);
        ((IInvokeProvider)peer.GetPattern(PatternInterface.Invoke)).Invoke();
    }

    private static async Task WaitForAsync(Func<bool> ready)
    {
        for (int i = 0; i < 240; i++)
        {
            await Task.Delay(50);
            if (ready()) return;
        }
        throw new TimeoutException("UI interaction did not complete.");
    }

    private async Task CaptureAsync(string path)
    {
        var bitmap = new RenderTargetBitmap();
        // System Mica is outside the XAML visual tree. Use a neutral dark base
        // for the exported XAML snapshot so translucent brushes remain readable.
        Brush? background = RootGrid.Background;
        try
        {
            RootGrid.Background = new SolidColorBrush(Windows.UI.Color.FromArgb(255, 32, 32, 32));
            await bitmap.RenderAsync(RootGrid);
        }
        finally { RootGrid.Background = background; }
        var pixels = await bitmap.GetPixelsAsync();
        using var reader = DataReader.FromBuffer(pixels);
        byte[] bytes = new byte[pixels.Length];
        reader.ReadBytes(bytes);
        StorageFolder folder = await StorageFolder.GetFolderFromPathAsync(Path.GetDirectoryName(path)!);
        StorageFile file = await folder.CreateFileAsync(Path.GetFileName(path), CreationCollisionOption.ReplaceExisting);
        using var stream = await file.OpenAsync(FileAccessMode.ReadWrite);
        var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
            (uint)bitmap.PixelWidth, (uint)bitmap.PixelHeight, 96, 96, bytes);
        await encoder.FlushAsync();
    }

    private void FinishSmoke(bool success, string message)
    {
        string output = Path.Combine(AppContext.BaseDirectory, "smoke-output");
        Directory.CreateDirectory(output);
        File.WriteAllText(Path.Combine(output, "result.txt"), message +
            "\nCurrent page: " + _page + "\nCompleted steps:\n" + string.Join("\n", _smokeSteps));
        Environment.ExitCode = success ? 0 : 1;
        _exitRequested = true;
        Close();
    }
}
