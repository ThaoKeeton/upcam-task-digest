using System.Diagnostics;
using System.Globalization;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Automation.Peers;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Markup;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Imaging;
using Windows.Graphics.Imaging;
using Windows.Storage.Streams;

namespace Workbench.Digest.Acceptance;

internal sealed class NativeHost
{
    private readonly int _uiThread = Environment.CurrentManagedThreadId;
    private readonly Window _window;
    private readonly ContentControl _content;
    private readonly Grid _shell;
    private readonly CancellationToken _lifetime;

    internal NativeHost(Window window, CancellationToken lifetime)
    {
        _window = window;
        _lifetime = lifetime;
        _shell = new Grid { RowSpacing = 8, Padding = new Thickness(12) };
        _shell.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
        _shell.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
        _shell.Children.Add(new TextBlock { Text = "UPCAM native acceptance · isolated local fixtures", FontSize = 16 });
        _content = new ContentControl
        {
            HorizontalContentAlignment = HorizontalAlignment.Stretch,
            VerticalContentAlignment = VerticalAlignment.Stretch
        };
        Grid.SetRow(_content, 1);
        _shell.Children.Add(_content);
        _window.Content = _shell;
        _window.Title = "UPCAM Workbench native acceptance";
        Resize(1280, 940);
    }

    internal void AssertUiThread()
    {
        Require(Environment.CurrentManagedThreadId == _uiThread && _shell.DispatcherQueue.HasThreadAccess,
            "A native factory or UI assertion ran outside the WinUI thread.");
    }

    internal void AssertStartup()
    {
        AssertUiThread();
        Require(SynchronizationContext.Current is Microsoft.UI.Dispatching.DispatcherQueueSynchronizationContext,
            "SDK generated startup did not install the WinUI synchronization context.");
        Require(Application.Current is IXamlMetadataProvider, "The application has no generated WinUI metadata provider.");
        var metadata = (IXamlMetadataProvider)Application.Current;
        Require(metadata.GetXamlType(typeof(TabView)) is not null &&
            metadata.GetXamlType("Microsoft.UI.Xaml.Controls.TabView") is not null,
            "Application resources did not resolve native TabView metadata.");
        Require(NativeProbes.VisibleWindowCount() == 1, "The acceptance process must own exactly one visible native window.");
    }

    internal async Task AttachAsync(FrameworkElement element)
    {
        AssertUiThread();
        Detach();
        var loaded = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnLoaded(object sender, RoutedEventArgs args) => loaded.TrySetResult();
        element.Loaded += OnLoaded;
        try
        {
            _content.Content = element;
            if (element.IsLoaded) loaded.TrySetResult();
            await loaded.Task.WaitAsync(TimeSpan.FromSeconds(30), _lifetime);
            element.UpdateLayout();
            await WaitUntilAsync(() => element.XamlRoot is not null && element.ActualWidth > 0 && element.ActualHeight > 0);
        }
        finally { element.Loaded -= OnLoaded; }
        Require(NativeProbes.VisibleWindowCount() == 1, "Attaching a plugin factory created an additional visible native window.");
    }

    internal void Detach()
    {
        AssertUiThread();
        _content.Content = null;
    }

    internal async Task WaitReadyAsync(FrameworkElement root, bool synthetic)
    {
        var expected = synthetic ? "Ready · CI synthetic data" : "Ready · local profile";
        await WaitUntilAsync(() => TryFind<TextBlock>(root, "plugin-status")?.Text == expected);
        Require(NativeProbes.VisibleWindowCount() == 1, "Plugin activation created an additional visible native window.");
    }

    internal async Task SelectTabAsync(FrameworkElement root, string id, int selected)
    {
        AssertUiThread();
        var tabs = Find<TabView>(root, id);
        Require(selected >= 0 && selected < tabs.TabItems.Count, "The requested native local tab is absent.");
        tabs.SelectedIndex = selected;
        root.UpdateLayout();
        await Task.Yield();
        root.UpdateLayout();
        Require(tabs.SelectedIndex == selected, "The native tab did not retain the selected index.");
    }

    internal async Task InvokeAsync(FrameworkElement root, string buttonId)
    {
        AssertUiThread();
        var before = ReadRevision(Find<TextBlock>(root, "operation-result").Text);
        var button = Find<Button>(root, buttonId);
        Require(button.IsEnabled, "The requested native business button is disabled.");
        // Invoke the real control; do not reflect into plugin state or call a plugin helper.
        new ButtonAutomationPeer(button).Invoke();
        await WaitUntilAsync(() => TryFind<TextBlock>(root, "operation-result") is { } text && ReadRevision(text.Text) > before,
            TimeSpan.FromSeconds(90));
        var result = Find<TextBlock>(root, "operation-result").Text;
        Require(result.Contains(" · " + buttonId + " · succeeded", StringComparison.Ordinal),
            "The requested native business operation did not report success; raw result suppressed.");
        await WaitReadyAsync(root, synthetic: true);
    }

    internal async Task EditAsync(TextBox editor, string value)
    {
        AssertUiThread();
        if (editor.Text == value) return;
        var changed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnChanged(object sender, TextChangedEventArgs args) => changed.TrySetResult();
        editor.TextChanged += OnChanged;
        try
        {
            editor.Text = value;
            await changed.Task.WaitAsync(TimeSpan.FromSeconds(10), _lifetime);
        }
        finally { editor.TextChanged -= OnChanged; }
    }

    internal void Resize(int width, int height)
    {
        AssertUiThread();
        _window.AppWindow.Resize(new Windows.Graphics.SizeInt32(width, height));
    }

    internal async Task CaptureAsync(AcceptanceReport report, string name)
    {
        AssertUiThread();
        _shell.UpdateLayout();
        var bitmap = new RenderTargetBitmap();
        await bitmap.RenderAsync(_shell);
        Require(bitmap.PixelWidth > 0 && bitmap.PixelHeight > 0, "The native screenshot has no rendered pixels.");
        var pixels = await bitmap.GetPixelsAsync();
        var data = new byte[checked((int)pixels.Length)];
        using (var pixelReader = DataReader.FromBuffer(pixels)) pixelReader.ReadBytes(data);
        using var stream = new InMemoryRandomAccessStream();
        var encoder = await BitmapEncoder.CreateAsync(BitmapEncoder.PngEncoderId, stream);
        encoder.SetPixelData(BitmapPixelFormat.Bgra8, BitmapAlphaMode.Premultiplied,
            (uint)bitmap.PixelWidth, (uint)bitmap.PixelHeight, 96, 96, data);
        await encoder.FlushAsync();
        var bytes = new byte[checked((int)stream.Size)];
        using (var reader = new DataReader(stream.GetInputStreamAt(0)))
        {
            await reader.LoadAsync((uint)bytes.Length);
            reader.ReadBytes(bytes);
        }
        var directory = System.IO.Path.Combine(System.IO.Path.GetDirectoryName(report.Path)!,
            System.IO.Path.GetFileNameWithoutExtension(report.Path) + " screenshots");
        OwnedStore.RejectLinks(directory);
        Directory.CreateDirectory(directory);
        var file = System.IO.Path.Combine(directory, name + ".png");
        OwnedStore.RejectLinks(file);
        await File.WriteAllBytesAsync(file, bytes, _lifetime);
        report.AddArtifact(new("native-png", file, bytes.Length, (uint)bitmap.PixelWidth, (uint)bitmap.PixelHeight));
    }

    internal async Task WaitUntilAsync(Func<bool> predicate, TimeSpan? timeout = null)
    {
        var clock = Stopwatch.StartNew();
        var deadline = timeout ?? TimeSpan.FromSeconds(35);
        while (!predicate())
        {
            _lifetime.ThrowIfCancellationRequested();
            if (clock.Elapsed >= deadline) throw new AcceptanceFailure("A native readiness, operation, or cleanup predicate timed out.");
            await Task.Delay(25, _lifetime);
        }
        AssertUiThread();
    }

    internal static T Find<T>(DependencyObject root, string id) where T : DependencyObject =>
        TryFind<T>(root, id) ?? throw new AcceptanceFailure("An expected native control is absent or ambiguous: " + id + ".");

    internal static T? TryFind<T>(DependencyObject root, string id) where T : DependencyObject
    {
        var found = Descendants<T>(root).Where(element => AutomationProperties.GetAutomationId(element) == id).ToArray();
        return found.Length == 1 ? found[0] : null;
    }

    internal static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        if (root is T match) yield return match;
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
            foreach (var child in Descendants<T>(VisualTreeHelper.GetChild(root, index))) yield return child;
    }

    private static int ReadRevision(string text)
    {
        var separator = text.IndexOf(" · ", StringComparison.Ordinal);
        return int.TryParse(separator < 0 ? text : text[..separator], NumberStyles.None, CultureInfo.InvariantCulture, out var value)
            ? value : 0;
    }

    internal static void Require([System.Diagnostics.CodeAnalysis.DoesNotReturnIf(false)] bool condition, string message)
    {
        if (!condition) throw new AcceptanceFailure(message);
    }
}
