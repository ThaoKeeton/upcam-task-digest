using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Automation.Peers;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Markup;
using Microsoft.UI.Xaml.Media;
using Workbench.Core;
using Workbench.WinUI.Sdk;

namespace Workbench.App;

/// <summary>Runs only inside the real WinUI process on its UI thread, with isolated local state.</summary>
internal static class NativeSmoke
{
    private const string SummarySavedDraft = "Native smoke user draft: keep these edits.\n已编辑的本地摘要，不得由示例生成覆盖。";
    private const string SummaryUnsavedDraft = "Native smoke unsaved edit: discard only on explicit disable or process exit.";

    public static async Task RunAsync(MainWindow window, RunOptions options)
    {
        List<string> checks = [];
        void Record(string check)
        {
            checks.Add(check);
            options.WriteReport(new { succeeded = false, stage = "running", userRoot = options.UserRoot, checks, startupStages = options.StartupStages });
        }
        window.AssertUiThread();
        Require(SynchronizationContext.Current is Microsoft.UI.Dispatching.DispatcherQueueSynchronizationContext,
            "SDK-generated startup did not install the WinUI synchronization context.");
        Record("WinUI Application.Start and UI synchronization context initialized");
        Require(Application.Current is IXamlMetadataProvider, "SDK-generated Application did not expose XAML metadata.");
        var metadata = (IXamlMetadataProvider)Application.Current;
        Require(metadata.GetXamlType(typeof(NavigationView)) is not null
            && metadata.GetXamlType("Microsoft.UI.Xaml.Controls.NavigationView") is not null,
            "Application could not resolve WinUI control metadata by type and full name.");
        Record("Application resolved WinUI control XAML metadata by type and full name");
        var manager = window.SmokeManager;
        Require(manager.Plugins.Count == 2, "Smoke must discover exactly the two curated bundled packages.");
        Require(manager.Plugins.All(p => p.Manifest is not null && RunOptions.SampleIds.Contains(p.Manifest.Id)
            && !p.IsLoaded && !p.IsTrusted && !p.DesiredEnabled), "Fresh smoke packages must be disabled and untrusted.");
        Require(manager.Diagnostics.Count == 0 && manager.Plugins.All(p => p.Diagnostics.Count == 0), "Discovery produced diagnostics.");
        Record("Only curated samples discovered; no real user state loaded");
        try
        {
            using var duplicate = HostRuntime.AcquireLease(options.UserRoot);
            throw new InvalidOperationException("Another owner acquired the same user store.");
        }
        catch (UserStoreInUseException) { Record("Actual lease acquisition rejects a second owner with a Windows sharing/lock violation"); }
        await VerifyOccupiedStartupAsync(options);
        Record("Occupied-store startup shows a manager-independent page, blocks Settings/Home navigation and closes safely");
        await window.SmokeVerifyLiveCardThemesAsync();
        Record("Same native host card re-resolved actual theme and fill/stroke across live Light/Dark/Light changes");

        foreach (var id in RunOptions.SampleIds)
        {
            // Explicitly authorized by --smoke-test, never used by ordinary application startup.
            var result = await manager.EnableAsync(id, confirmTrust: true);
            Require(result.Succeeded, $"Enable {id}: {result.Message}");
        }
        await AttachAllViewsAsync(window);
        Record("Both independent samples supplied shared WinUiView page/settings identities and attached native controls");
        foreach (var theme in new[] { ElementTheme.Default, ElementTheme.Light, ElementTheme.Dark })
            window.SmokeSetTheme(theme);
        Record("System/light/dark requested themes applied to the native root (visual appearance requires manual review)");
        Require(manager.Contributions.Commands.Count == 2, "Expected one local command per sample.");
        foreach (var command in manager.Contributions.Commands)
        {
            var result = await manager.ExecuteCommandAsync(command.PluginId, command.Id);
            Require(result.Succeeded, $"Command {command.Id}: {result.Message}");
        }
        var files = new[]
        {
            Path.Combine(options.UserRoot, "data", RunOptions.SampleIds[0], "summary-draft.txt"),
            Path.Combine(options.UserRoot, "data", RunOptions.SampleIds[1], "sample-test-cases.csv")
        };
        Require(files.All(file => File.Exists(file) && new FileInfo(file).Length > 0), "Local commands did not write isolated sample files.");
        Record("Local commands wrote nonempty draft and CSV under isolated DataDirectory paths");
        var savedSummaryDraft = await VerifySummaryDraftAsync(window, files[0]);
        Record("Real Summary editor retained unsaved text through fresh Home/Commands/settings navigation, saved edits and refused destructive regeneration");
        foreach (var id in RunOptions.SampleIds)
        {
            window.SmokeDetach();
            var disabled = await manager.DisableAsync(id);
            Require(disabled.Succeeded, $"Disable {id}: {disabled.Message}");
            Require(manager.Contributions.Views.All(v => v.PluginId != id)
                && manager.Contributions.Commands.All(c => c.PluginId != id), "Disabled plugin still has registrations.");
            var enabled = await manager.EnableAsync(id);
            Require(enabled.Succeeded, $"Re-enable {id}: {enabled.Message}");
            if (id == RunOptions.SampleIds[0])
            {
                var page = await OpenSummaryAsync(window);
                Require(FindNamed<TextBox>(page, "工作摘要示例草稿").Text == savedSummaryDraft,
                    "Summary re-enable did not reload the saved draft after discarding unsaved session edits.");
                Require(await File.ReadAllTextAsync(files[0]) == savedSummaryDraft,
                    "Summary disable/re-enable changed the saved draft file.");
            }
        }
        await AttachAllViewsAsync(window);
        Record("Detached controls, disabled and re-enabled both samples, then attached fresh native factories");
        Record("Summary disable discarded only unsaved session edits; re-enable loaded the unchanged saved draft");
        await window.SmokeStopAsync();
        Require(manager.Contributions.Views.Count == 0 && manager.Contributions.Commands.Count == 0, "Shutdown retained active contributions.");
        Require(manager.Plugins.All(p => p.Diagnostics.Count == 0), "Lifecycle produced diagnostics.");
        using var state = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(options.UserRoot, "plugin-state.json")));
        foreach (var id in RunOptions.SampleIds)
            Require(state.RootElement.GetProperty("plugins").GetProperty(id).GetProperty("enabled").GetBoolean(), "Shutdown changed desired enabled preference.");
        Record("Shutdown removed registrations and preserved desired-enabled preferences");
        options.WriteReport(new
        {
            succeeded = true,
            stage = "completed",
            userRoot = options.UserRoot,
            uiThreadId = Environment.CurrentManagedThreadId,
            checks,
            startupStages = options.StartupStages,
            files = files.Select(path => new { path, bytes = new FileInfo(path).Length }),
            completedUtc = DateTimeOffset.UtcNow
        });
        await window.FinishSmokeAsync();
    }

    private static async Task<string> VerifySummaryDraftAsync(MainWindow window, string path)
    {
        var manager = window.SmokeManager;
        var generatedText = await File.ReadAllTextAsync(path);
        var page = await OpenSummaryAsync(window);
        var edited = FindNamed<TextBox>(page, "工作摘要示例草稿");
        List<string> editEvents = [];
        await EditAsync(edited, SummarySavedDraft, editEvents);
        var afterEdit = edited.Text;
        // WinUI canonicalizes LF input to CR. Validate content once, then require exact native text everywhere.
        Require(NormalizeNewlines(afterEdit) == NormalizeNewlines(SummarySavedDraft),
            $"Summary editor changed the entered content. {DescribeText("literal", SummarySavedDraft)}; "
            + $"{DescribeText("after edit", afterEdit)}; events={JsonSerializer.Serialize(editEvents)}");

        List<(string Page, string? PluginId, string? ViewId)> routes =
            [("home", null, null), ("commands", null, null), ("settings", null, null)];
        routes.AddRange(manager.Contributions.Views
            .Where(v => v.View is WinUiView { Kind: WinUiViewKind.Settings })
            .Select(v => ("view", (string?)v.PluginId, (string?)v.View.Id)));
        foreach (var route in routes)
        {
            var previous = page;
            await window.SmokeNavigateAsync(route.Page, route.PluginId, route.ViewId);
            page = await OpenSummaryAsync(window);
            Require(!ReferenceEquals(previous, page), "Summary navigation must create a fresh native view.");
            var actual = FindNamed<TextBox>(page, "工作摘要示例草稿").Text;
            Require(actual == afterEdit,
                $"Summary unsaved draft was lost after navigating through {route.Page}. "
                + $"{DescribeText("literal", SummarySavedDraft)}; {DescribeText("after edit", afterEdit)}; "
                + $"{DescribeText("old.Text now", edited.Text)}; {DescribeText("fresh.Text", actual)}; "
                + $"{DescribeDifference(afterEdit, actual)}; events={JsonSerializer.Serialize(editEvents)}");
        }

        var command = manager.Contributions.Commands.Single(c => c.PluginId == RunOptions.SampleIds[0]);
        await window.SmokeNavigateAsync("commands");
        var unsavedGeneration = await manager.ExecuteCommandAsync(command.PluginId, command.Id);
        Require(!unsavedGeneration.Succeeded && !string.IsNullOrWhiteSpace(unsavedGeneration.Message),
            "Summary generation must clearly refuse to replace an edited draft.");
        Require(await File.ReadAllTextAsync(path) == generatedText, "Summary regeneration replaced the existing file before Save.");
        page = await OpenSummaryAsync(window);
        Require(FindNamed<TextBox>(page, "工作摘要示例草稿").Text == afterEdit,
            "Summary regeneration replaced unsaved editor text.");

        var status = Descendants<TextBlock>(page).Single(text => text.Text.Contains(path, StringComparison.Ordinal));
        var saved = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var subscription = status.RegisterPropertyChangedCallback(TextBlock.TextProperty, (_, _) => saved.TrySetResult());
        try
        {
            // Invoke the real native button and wait for its async UI result, not a plugin helper.
            new ButtonAutomationPeer(FindNamed<Button>(page, "保存工作摘要示例草稿")).Invoke();
            await saved.Task.WaitAsync(TimeSpan.FromSeconds(15));
        }
        finally { status.UnregisterPropertyChangedCallback(TextBlock.TextProperty, subscription); }
        Require(await File.ReadAllTextAsync(path) == afterEdit, "Summary Save did not persist the edited draft.");

        await window.SmokeNavigateAsync("commands");
        var savedGeneration = await manager.ExecuteCommandAsync(command.PluginId, command.Id);
        Require(!savedGeneration.Succeeded && !string.IsNullOrWhiteSpace(savedGeneration.Message),
            "Summary generation must clearly refuse to replace a saved draft.");
        Require(await File.ReadAllTextAsync(path) == afterEdit, "Summary regeneration replaced the user's saved draft file.");
        page = await OpenSummaryAsync(window);
        var editor = FindNamed<TextBox>(page, "工作摘要示例草稿");
        Require(editor.Text == afterEdit, "Summary regeneration replaced the saved draft model.");
        // The existing lifecycle round trip below must restore the saved value, not this session-only edit.
        await EditAsync(editor, SummaryUnsavedDraft);
        return afterEdit;
    }

    private static Task<FrameworkElement> OpenSummaryAsync(MainWindow window)
    {
        var view = window.SmokeManager.Contributions.Views.Single(v => v.PluginId == RunOptions.SampleIds[0]
            && v.View is WinUiView { Kind: WinUiViewKind.Page });
        return window.SmokeNavigateAsync("view", view.PluginId, view.View.Id);
    }

    private static async Task EditAsync(TextBox editor, string text, List<string>? trace = null)
    {
        var changed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnChanging(TextBox sender, TextBoxTextChangingEventArgs args) =>
            trace?.Add(DescribeText($"TextChanging(contentChanging={args.IsContentChanging})", editor.Text));
        void OnChanged(object sender, TextChangedEventArgs args)
        {
            trace?.Add(DescribeText("TextChanged", editor.Text));
            changed.TrySetResult();
        }
        editor.TextChanging += OnChanging;
        editor.TextChanged += OnChanged;
        try
        {
            trace?.Add(DescribeText("before Text setter", editor.Text));
            editor.Text = text;
            trace?.Add(DescribeText("after Text setter", editor.Text));
            await changed.Task.WaitAsync(TimeSpan.FromSeconds(15));
            trace?.Add(DescribeText("after TextChanged await", editor.Text));
        }
        finally
        {
            editor.TextChanging -= OnChanging;
            editor.TextChanged -= OnChanged;
        }
    }

    // Only isolated smoke literals/sample data are included in these failure diagnostics.
    private static string DescribeText(string label, string text) => $"{label}: length={text.Length}, text={JsonSerializer.Serialize(text)}";

    private static string NormalizeNewlines(string text) => text.Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n');

    private static string DescribeDifference(string expected, string actual)
    {
        var index = 0;
        while (index < expected.Length && index < actual.Length && expected[index] == actual[index]) index++;
        if (index == expected.Length && index == actual.Length) return "no character difference";
        static string CodeAt(string text, int at) => at < text.Length ? $"U+{(int)text[at]:X4}" : "end";
        return $"first difference at {index}: expected {CodeAt(expected, index)}, actual {CodeAt(actual, index)}";
    }

    private static T FindNamed<T>(DependencyObject root, string name) where T : DependencyObject =>
        Descendants<T>(root).SingleOrDefault(element => AutomationProperties.GetName(element) == name)
        ?? throw new InvalidOperationException($"Native view did not expose {typeof(T).Name} '{name}'.");

    private static IEnumerable<T> Descendants<T>(DependencyObject root) where T : DependencyObject
    {
        if (root is T match) yield return match;
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(root); index++)
            foreach (var child in Descendants<T>(VisualTreeHelper.GetChild(root, index))) yield return child;
    }

    private static async Task VerifyOccupiedStartupAsync(RunOptions options)
    {
        var failed = new MainWindow(options with { SmokeTest = false, ReportPath = null });
        var closed = new TaskCompletionSource();
        failed.Closed += (_, _) => closed.TrySetResult();
        try
        {
            failed.Activate();
            await failed.InitializeAsync();
            Require(failed.SmokeStartupFailure is UserStoreInUseException, "Second host did not report the actual occupied-store failure.");
            Require(!failed.SmokeNavigationAvailable, "Navigation/settings remained available after startup failed.");
            failed.SmokeTryStartupNavigation();
        }
        finally
        {
            await failed.FinishSmokeAsync();
            await closed.Task.WaitAsync(TimeSpan.FromSeconds(15));
        }
    }

    private static async Task AttachAllViewsAsync(MainWindow window)
    {
        var views = window.SmokeManager.Contributions.Views;
        Require(views.Count == 4, "Expected two native pages and two native settings factories.");
        foreach (var id in RunOptions.SampleIds)
        {
            var native = views.Where(v => v.PluginId == id).Select(v => v.View).OfType<WinUiView>().ToArray();
            Require(native.Length == 2 && native.Any(v => v.Kind == WinUiViewKind.Page)
                && native.Any(v => v.Kind == WinUiViewKind.Settings), "Shared native view contract identity or kinds failed.");
            foreach (var view in native) await window.SmokeAttachAsync(view);
        }
    }

    private static void Require(bool condition, string message)
    {
        if (!condition) throw new InvalidOperationException(message);
    }
}

internal sealed partial class MainWindow
{
    // Native test plumbing uses the ordinary navigation/factory path without caching a control.
    internal async Task<FrameworkElement> SmokeNavigateAsync(string page, string? pluginId = null, string? viewId = null)
    {
        AssertUiThread();
        Navigate(new(page, pluginId, viewId));
        var element = _content.Content as FrameworkElement ?? throw new InvalidOperationException("Navigation did not supply a native page.");
        var loaded = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnLoaded(object sender, RoutedEventArgs args) => loaded.TrySetResult();
        element.Loaded += OnLoaded;
        try
        {
            if (!element.IsLoaded) await loaded.Task.WaitAsync(TimeSpan.FromSeconds(15));
            if (element.XamlRoot is null) throw new InvalidOperationException("Navigated native page was not attached to the host XamlRoot.");
            return element;
        }
        finally { element.Loaded -= OnLoaded; }
    }

}
