using Microsoft.UI.Xaml;
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

    private async Task RunSmokeAsync()
    {
        try
        {
            ShowMainWindow();
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
            Invoke(Find<Button>("probe-project")!);
            await WaitForAsync(() => !_busy);
            Assert(_state.CandidateOwners.Contains("demo-person"), "Read-only synthetic project probe");
            var check = Find<CheckBox>("confirm-scope")!;
            ((IToggleProvider)new CheckBoxAutomationPeer(check).GetPattern(PatternInterface.Toggle)).Toggle();
            Invoke(SaveButton);
            await WaitForAsync(() => !_busy);
            Assert(_state.ScopeConfirmed, "Save recomputed scope proof and legacy settings format");

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
            Invoke(Find<Button>("run-manual")!);
            await WaitForAsync(() => !_busy);
            Assert(_state.History.Count == 1, "Synthetic workflow and local history");

            string output = Path.Combine(AppContext.BaseDirectory, "smoke-output");
            Directory.CreateDirectory(output);
            foreach (var size in new[] { new SizeInt32(1180, 800), new SizeInt32(900, 700), new SizeInt32(760, 620), new SizeInt32(640, 540) })
            {
                AppWindow.Resize(size);
                await Task.Delay(150);
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
                        Assert(textbox.ActualHeight >= 60, $"Diagnostic result has usable space {size.Width}x{size.Height}");
                        AssertInside(textbox, "Diagnostic output");
                        foreach (string id in new[] { "diagnose", "run-manual", "preview-sample", "export-diagnostic" })
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
            if (_native?.TrayReady == true)
            {
                Close();
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
                syntheticOnly = true, steps = _smokeSteps
            }, new JsonSerializerOptions { WriteIndented = true }));
            FinishSmoke(true, "passed");
        }
        catch (Exception ex)
        {
            // This mode uses isolated synthetic fixtures only; no production values exist.
            FinishSmoke(false, ex.GetType().Name + ": " + ex.Message);
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
        await bitmap.RenderAsync(RootGrid);
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
        File.WriteAllText(Path.Combine(output, "result.txt"), message);
        Environment.ExitCode = success ? 0 : 1;
        _exitRequested = true;
        Close();
    }
}
