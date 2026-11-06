using System.Diagnostics;
using System.Globalization;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Automation.Peers;
using Microsoft.UI.Xaml.Automation.Provider;
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
        // Explicit real CI parent theme/backing: RenderTargetBitmap otherwise preserves transparent shell pixels.
        // This styles the dedicated test host only; plugin production theme inheritance is unchanged.
        _shell = new Grid
        {
            RowSpacing = 8, Padding = new Thickness(12), RequestedTheme = ElementTheme.Light,
            Background = new SolidColorBrush(Microsoft.UI.Colors.White)
        };
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
        NativeProbes.VerifyWindowClassification();
        Require(SynchronizationContext.Current is Microsoft.UI.Dispatching.DispatcherQueueSynchronizationContext,
            "SDK generated startup did not install the WinUI synchronization context.");
        Require(Application.Current is IXamlMetadataProvider, "The application has no generated WinUI metadata provider.");
        var metadata = (IXamlMetadataProvider)Application.Current;
        Require(metadata.GetXamlType(typeof(TabView)) is not null &&
            metadata.GetXamlType("Microsoft.UI.Xaml.Controls.TabView") is not null,
            "Application resources did not resolve native TabView metadata.");
        AssertSingleWindow("The acceptance process must own exactly one visible native window.");
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
        AssertSingleWindow("Attaching a plugin factory created an additional visible native window.");
    }

    internal void Detach()
    {
        AssertUiThread();
        _content.Content = null;
    }

    internal async Task WaitReadyAsync(FrameworkElement root, bool synthetic)
    {
        var expected = synthetic ? "Ready · CI synthetic data" : "Ready · local profile";
        TabView? previousHost = null;
        FrameworkElement? previousContent = null;
        await WaitForUiAsync(root, () =>
        {
            root.UpdateLayout();
            var tabs = CurrentTabHost(root, PrimaryTabId(root));
            tabs?.ApplyTemplate();
            root.UpdateLayout();
            if (TryFind<TextBlock>(root, "plugin-status")?.Text != expected ||
                !TryRealizedSelection(root, tabs, out var content) || !NestedSelectionRealized(root, content))
            {
                previousHost = null;
                previousContent = null;
                return false;
            }
            var stable = ReferenceEquals(tabs, previousHost) && ReferenceEquals(content, previousContent);
            previousHost = tabs;
            previousContent = content;
            return stable;
        }, "Backend ready status did not converge with the current realized native tab content.");
        AssertSingleWindow("Plugin activation created an additional visible native window.");
    }

    internal void AssertSingleWindow(string failure)
    {
        var main = WinRT.Interop.WindowNative.GetWindowHandle(_window);
        var windows = NativeProbes.VisibleWindows();
        var application = new List<NativeWindowInfo>();
        foreach (var window in windows)
        {
            if (NativeProbes.IsVerifiedFrameworkPopup(window, main, (uint)Environment.ProcessId))
                NativeProbes.RecordIgnoredPopup(window, main);
            else application.Add(window);
        }
        if (application.Count != 1 || application[0].Handle != main)
            throw new AcceptanceFailure(failure + " " + NativeProbes.VisibleWindowInventory(windows, main));
    }

    internal async Task SelectTabAsync(FrameworkElement root, string id, int selected)
    {
        AssertUiThread();
        TabView? selectionTarget = null;
        TabView? previousHost = null;
        FrameworkElement? previousContent = null;
        await WaitForUiAsync(root, () =>
        {
            root.UpdateLayout();
            var tabs = CurrentTabHost(root, id);
            if (tabs is null)
            {
                previousHost = null;
                previousContent = null;
                return false;
            }
            tabs.ApplyTemplate();
            Require(selected >= 0 && selected < tabs.TabItems.Count, "The requested native local tab is absent.");
            if (!ReferenceEquals(tabs, selectionTarget))
            {
                // A foreground state notification can replace the whole tab host. Apply this user selection
                // to its current replacement once; never repeatedly force a reset on the same live host.
                tabs.SelectedIndex = selected;
                selectionTarget = tabs;
                previousHost = null;
                previousContent = null;
            }
            root.UpdateLayout();
            if (!ReferenceEquals(CurrentTabHost(root, id), tabs) || tabs.SelectedIndex != selected ||
                !TryRealizedSelection(root, tabs, out var content) || !NestedSelectionRealized(root, content))
            {
                previousHost = null;
                previousContent = null;
                return false;
            }
            var stable = ReferenceEquals(tabs, previousHost) && ReferenceEquals(content, previousContent);
            previousHost = tabs;
            previousContent = content;
            return stable;
        }, "The requested native tab selection did not produce stable realized content: " + id + ".");
    }

    private static string PrimaryTabId(FrameworkElement root) => AutomationProperties.GetAutomationId(root) switch
    {
        "settings-page" => "settings-tabs",
        "digest-page" => "digest-tabs",
        _ => throw new AcceptanceFailure("The attached native root has no known primary tab host.")
    };

    private static TabView? CurrentTabHost(FrameworkElement root, string id)
    {
        // Resolve the live ContentControl.Content object before consulting the visual tree. After Render,
        // a visual predecessor can still exist briefly while the new logical body is not realized yet.
        var primaryId = PrimaryTabId(root);
        TabView? primary = null;
        foreach (var owner in Descendants<ContentControl>(root))
        {
            if (owner.Content is not TabView candidate || AutomationProperties.GetAutomationId(candidate) != primaryId) continue;
            if (primary is not null && !ReferenceEquals(primary, candidate)) return null;
            primary = candidate;
        }
        for (var current = primary; current is not null; current = (current.SelectedItem as TabViewItem)?.Content as TabView)
            if (AutomationProperties.GetAutomationId(current) == id) return current;
        return null;
    }

    private static bool TryRealizedSelection(FrameworkElement root, TabView? tabs,
        [System.Diagnostics.CodeAnalysis.NotNullWhen(true)] out FrameworkElement? content)
    {
        content = (tabs?.SelectedItem as TabViewItem)?.Content as FrameworkElement;
        if (tabs is null || content is null || !root.IsLoaded || root.XamlRoot is null ||
            !tabs.IsLoaded || !content.IsLoaded || tabs.ActualWidth <= 0 || tabs.ActualHeight <= 0 ||
            content.ActualWidth <= 0 || content.ActualHeight <= 0 ||
            !ReferenceEquals(tabs.XamlRoot, root.XamlRoot) || !ReferenceEquals(content.XamlRoot, root.XamlRoot)) return false;
        var id = AutomationProperties.GetAutomationId(tabs);
        var selectedContent = content;
        return ReferenceEquals(CurrentTabHost(root, id), tabs) && ReferenceEquals(TryFind<TabView>(root, id), tabs) &&
            Descendants<FrameworkElement>(tabs).Any(element => ReferenceEquals(element, selectedContent));
    }

    private static bool NestedSelectionRealized(FrameworkElement root, FrameworkElement content)
    {
        // Accounts is itself a native TabView selected as the settings page's content.
        // Its selected service page must also finish realization before its controls can be used.
        while (content is TabView nested)
        {
            nested.ApplyTemplate();
            root.UpdateLayout();
            if (!TryRealizedSelection(root, nested, out var selected)) return false;
            content = selected;
        }
        return true;
    }

    private async Task WaitForUiAsync(FrameworkElement root, Func<bool> predicate, string failure)
    {
        try { await WaitUntilAsync(predicate); }
        catch (AcceptanceFailure)
        {
            throw new AcceptanceFailure(failure + " " + ControlInventory(root));
        }
    }

    internal async Task<string> InvokeAsync(FrameworkElement root, string buttonId, bool expectSuccess = true,
        string? operationId = null, bool expectReady = true)
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
        var expected = expectSuccess ? "succeeded" : "failed";
        Require(result.Contains(" · " + (operationId ?? buttonId) + " · " + expected, StringComparison.Ordinal),
            "The requested native business operation reported an unexpected outcome; raw result suppressed.");
        if (expectReady) await WaitReadyAsync(root, synthetic: true);
        return result;
    }

    internal async Task SetCheckedAsync(FrameworkElement root, string id, bool value)
    {
        AssertUiThread();
        var box = Find<CheckBox>(root, id);
        Require(box.IsEnabled && !box.IsThreeState, "The native consent checkbox is disabled or has an ambiguous state.");
        if (box.IsChecked == value) return;
        var provider = new CheckBoxAutomationPeer(box).GetPattern(PatternInterface.Toggle) as IToggleProvider;
        Require(provider is not null, "The native consent checkbox does not expose its Toggle automation pattern.");
        provider.Toggle();
        await WaitUntilAsync(() => box.IsChecked == value);
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

    internal async Task WaitUntilAsync(Func<Task<bool>> predicate, TimeSpan? timeout = null)
    {
        var clock = Stopwatch.StartNew();
        var deadline = timeout ?? TimeSpan.FromSeconds(35);
        while (!await predicate())
        {
            _lifetime.ThrowIfCancellationRequested();
            if (clock.Elapsed >= deadline) throw new AcceptanceFailure("An actual runtime service readiness predicate timed out.");
            await Task.Delay(25, _lifetime);
        }
        AssertUiThread();
    }

    internal static T Find<T>(DependencyObject root, string id) where T : DependencyObject =>
        TryFind<T>(root, id) ?? throw new AcceptanceFailure("An expected native control is absent or ambiguous: " + id + ". " + ControlInventory(root));

    private static string ControlInventory(DependencyObject root)
    {
        // No Text, Header, Name, Password, settings, paths or data values. IDs, types, counts and layout-presence flags only.
        var elements = Descendants<DependencyObject>(root).ToArray();
        var groups = elements
            .Select(element => new { Id = AutomationProperties.GetAutomationId(element), Type = element.GetType().Name })
            .GroupBy(element => (Id: SafeIdentifier(element.Id), element.Type))
            .OrderBy(group => group.Key.Id, StringComparer.Ordinal).ThenBy(group => group.Key.Type, StringComparer.Ordinal)
            .Take(80).Select(group => group.Key.Id + "/" + group.Key.Type + "=" + group.Count());
        var logical = elements.OfType<ContentControl>().Select(owner => owner.Content).OfType<TabView>()
            .Select(tabs =>
            {
                var selected = (tabs.SelectedItem as TabViewItem)?.Content as FrameworkElement;
                return SafeIdentifier(AutomationProperties.GetAutomationId(tabs)) + "/TabView" +
                    "[loaded=" + (tabs.IsLoaded ? 1 : 0) + ", selected-content-type=" + (selected?.GetType().Name ?? "none") +
                    ", selected-loaded=" + (selected?.IsLoaded == true ? 1 : 0) +
                    ", selected-measured=" + (selected is { ActualWidth: > 0, ActualHeight: > 0 } ? 1 : 0) + "]";
            })
            .Distinct(StringComparer.Ordinal).Take(12);
        return "Native control inventory (id/type=count): " + string.Join(", ", groups) +
            ". Logical tab contents: " + string.Join(", ", logical) + ".";
    }

    private static string SafeIdentifier(string value) => value.Length is > 0 and <= 80 &&
        value.All(character => character is >= 'a' and <= 'z' or >= '0' and <= '9' or '-' or '_') ? value : "<no-or-other-id>";

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

    internal static int ReadRevision(string text)
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
