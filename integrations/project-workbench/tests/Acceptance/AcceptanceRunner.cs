using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Automation;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Workbench.Core;
using Workbench.Sdk;
using Workbench.WinUI.Sdk;
using static Workbench.Digest.Acceptance.NativeHost;

namespace Workbench.Digest.Acceptance;

internal sealed class AcceptanceRunner(NativeHost host, RunOptions options, AcceptanceReport report, CancellationToken lifetime)
{
    private const string AcceptanceId = "com.upcam.task-digest.acceptance";
    private const string ProductionId = "com.upcam.task-digest";
    private WinUiView _digest = null!;
    private WinUiView _settings = null!;
    private FrameworkElement _digestRoot = null!;
    private FrameworkElement _settingsRoot = null!;

    private static readonly string[] AcceptanceChecks =
    [
        "acceptance-package-contract", "fresh-catalog", "install-disabled-untrusted", "untrusted-gate-no-code", "synthetic-second-profile-fixture",
        "initial-enable-local-only", "activation-persistence-rollback", "committed-enable", "native-fresh-factories",
        "first-view-activation", "settings-draft-retention", "range-and-tab-retention", "local-preview-command",
        "native-layout-and-focus", "synthetic-project-probe", "synthetic-explicit-save", "synthetic-model-refresh",
        "synthetic-read-only-diagnose", "detailed-export-existing-target-failure", "detailed-export-new-target-success",
        "synthetic-explicit-manual-send", "synthetic-history", "scheduled-noop-preserves-focused-draft",
        "profile-binding-persistence-rollback", "profile-adoption-keeps-owned-state", "profile-return-retains-drafts-and-history", "disable-cleanup",
        "trusted-reenable", "shutdown-preserves-enabled-intent", "restart-exact-package-restoration",
        "package-mutation-revokes-trust", "acceptance-host-settings-only-binding", "acceptance-dispose-cleanup"
    ];
    private static readonly string[] ProductionChecks =
    [
        "production-package-contract", "production-install-trust-enable", "production-local-status-before-view",
        "production-local-setup", "production-empty-secret-controls", "production-no-authorization-expansion",
        "production-empty-history-and-ledger", "production-local-files-only", "production-host-settings-only-binding",
        "production-dispose-cleanup"
    ];

    internal async Task RunAsync()
    {
        if (!await report.CheckAsync("native-sdk-startup", () => { host.AssertStartup(); return Task.CompletedTask; }))
        {
            SkipRemaining("Native SDK startup failed.");
            return;
        }
        if (!await report.CheckAsync("exclusive-backend-precondition", () =>
        {
            Require(!NativeProbes.SingletonExists(), "An existing standalone/plugin singleton prevents isolated acceptance. Its owner was not touched.");
            Require(!AppDomain.CurrentDomain.GetAssemblies().Any(a => a.GetName().Name == "UPCAM.Workbench"),
                "The plugin was already loaded before real manager installation.");
            return Task.CompletedTask;
        }))
        {
            SkipRemaining("The backend is already owned or the plugin was preloaded.");
            return;
        }

        await RunAcceptanceAsync();
        if (!NativeProbes.SingletonExists())
            await new RuntimeAcceptanceRunner(host, options, report, lifetime).RunAsync();
        else
            foreach (var id in RuntimeAcceptanceRunner.ExpectedChecks) report.Skip(id, "The previous backend still owns the singleton.");
        // This independent phase still runs after a synthetic business assertion failed, once cleanup is observed.
        if (options.ProductionPackageDirectory is null)
            foreach (var id in ProductionChecks) report.Skip(id, "No --production-package-directory supplied.");
        else if (await report.CheckAsync("production-exclusive-backend-precondition", () =>
        {
            Require(!NativeProbes.SingletonExists(), "Prior cleanup did not release the backend singleton; production smoke was not started.");
            return Task.CompletedTask;
        }))
            await RunProductionAsync(options.ProductionPackageDirectory);
        else
            foreach (var id in ProductionChecks) report.Skip(id, "The backend singleton remains occupied.");
    }

    private async Task RunAcceptanceAsync()
    {
        OwnedStore? store = null;
        try
        {
            await RequiredAsync("acceptance-package-contract", () => VerifyManifestAsync(options.PackageDirectory, AcceptanceId));
            store = new OwnedStore("synthetic", report);
            var manager = store.Manager;
            var installed = Path.Combine(store.PluginsRoot, AcceptanceId);
            await RequiredAsync("fresh-catalog", async () =>
            {
                Require(store.Root.Contains(' ') && Directory.EnumerateFileSystemEntries(store.Root).Count() == 3,
                    "Startup did not use a fresh, explicitly owned temporary user root with spaces.");
                Require((await manager.DiscoverAsync(lifetime)).Count == 0, "Fresh manager discovery was not empty.");
                Require((await manager.RestoreEnabledAsync(lifetime)).Count == 0, "Fresh manager unexpectedly restored a plugin.");
                Require(ContributionsEmpty(manager), "Fresh manager has contributions.");
            });
            await RequiredAsync("install-disabled-untrusted", async () =>
            {
                Require((await manager.InstallAsync(options.PackageDirectory, lifetime)).Succeeded, "Actual manager installation failed; raw diagnostic suppressed.");
                await manager.DiscoverAsync(lifetime);
                var plugin = OnlyPlugin(manager, AcceptanceId);
                Require(plugin.State == PluginState.Disabled && !plugin.IsLoaded && !plugin.IsTrusted && !plugin.DesiredEnabled,
                    "Installation did not leave the package disabled, untrusted and unloaded.");
                Require(ContributionsEmpty(manager) && !PluginAssemblyLoaded(), "Installation/discovery loaded plugin code or exposed contributions.");
                AssertNoBackend(installed);
            });
            await RequiredAsync("untrusted-gate-no-code", async () =>
            {
                Require(!(await manager.EnableAsync(AcceptanceId, cancellationToken: lifetime)).Succeeded,
                    "Untrusted activation unexpectedly succeeded.");
                Require(!(await manager.ExecuteCommandAsync(AcceptanceId, "status", lifetime)).Succeeded,
                    "A disabled untrusted command was executable.");
                Require(!OnlyPlugin(manager, AcceptanceId).IsLoaded && !PluginAssemblyLoaded() && ContributionsEmpty(manager),
                    "The trust gate loaded code or committed contributions.");
                AssertNoBackend(installed);
            });
            await RequiredAsync("synthetic-second-profile-fixture", async () =>
            {
                // The only source-linked implementation is the real process transport. It prepares an explicit
                // second synthetic profile before any plugin instance runs, then releases its worker and singleton.
                await using (var fixture = await Upcam.Workbench.PluginWorkerClient.StartAsync(options.PackageDirectory,
                    SecondProfile(store), synthetic: true, cancellationToken: lifetime))
                {
                    var initialized = await fixture.SendAsync("initialize", cancellationToken: lifetime);
                    Require(initialized["application_version"]?.GetValue<string>() == "0.4.6" &&
                        initialized["settings"]?["tenant_id"]?.GetValue<string>() == "demo-org" &&
                        initialized["settings"]?["auto_send_enabled"]?.GetValue<bool>() == false,
                        "The fixture worker did not initialize an unconfigured-for-scheduling synthetic second profile.");
                }
                await WaitNoBackendAsync(options.PackageDirectory);
                Require(!PluginAssemblyLoaded() && ContributionsEmpty(manager), "Fixture preparation loaded the plugin entry assembly or contributions.");
                Require(NativeProbes.SqlCount(Path.Combine(SecondProfile(store), "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;") == 0 &&
                    NativeProbes.SqlCount(Path.Combine(SecondProfile(store), "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == 0,
                    "The second synthetic fixture unexpectedly contains a business run or delivery claim.");
            });
            await RequiredAsync("initial-enable-local-only", async () =>
            {
                // The explicit CLI invocation authorizes trust of this isolated acceptance fixture only.
                Require((await manager.EnableAsync(AcceptanceId, confirmTrust: true, cancellationToken: lifetime)).Succeeded,
                    "Explicitly trusted activation failed.");
                AssertEnabled(manager, AcceptanceId);
                AssertSavedState(store.Root, AcceptanceId, enabled: true, trusted: true);
                await AssertLocalStatusAsync(manager, AcceptanceId, started: false);
                AssertNoBackend(installed);
                Require((await manager.DisableAsync(AcceptanceId, lifetime)).Succeeded, "Setup disable failed.");
                AssertNoBackend(installed);
            });
            await RequiredAsync("activation-persistence-rollback", async () =>
            {
                // Windows FileShare.Read deliberately rejects atomic replacement of this owned fixture file.
                // Saved approval already exists, so the failure occurs when enabled intent is committed after InitializeAsync.
                using (var blockedState = new FileStream(Path.Combine(store.Root, "plugin-state.json"), FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    Require(!(await manager.EnableAsync(AcceptanceId, cancellationToken: lifetime)).Succeeded,
                        "Activation succeeded despite the deliberate enabled-state persistence failure.");
                }
                var plugin = OnlyPlugin(manager, AcceptanceId);
                Require(plugin.State == PluginState.Failed && !plugin.DesiredEnabled && ContributionsEmpty(manager),
                    "Failed activation retained enabled intent or staged contributions.");
                Require(plugin.Diagnostics.Any(d => d.Code == PluginDiagnosticCode.StatePersistenceFailed),
                    "The intended persistence failure did not occur.");
                AssertSavedState(store.Root, AcceptanceId, enabled: false, trusted: true);
                AssertNoBackend(installed);
            });
            await RequiredAsync("committed-enable", async () =>
            {
                Require((await manager.EnableAsync(AcceptanceId, cancellationToken: lifetime)).Succeeded,
                    "Activation did not recover after the fixture state lock was released.");
                AssertEnabled(manager, AcceptanceId);
                AssertSavedState(store.Root, AcceptanceId, enabled: true, trusted: true);
                ReadFactories(manager, AcceptanceId);
                Require(manager.Contributions.Commands.Any(c => c.PluginId == AcceptanceId && c.Id == "status") &&
                    manager.Contributions.Commands.Any(c => c.PluginId == AcceptanceId && c.Id == "preview-sample"),
                    "The actual manager command catalog lacks local status or sample preview.");
                await AssertLocalStatusAsync(manager, AcceptanceId, started: false);
                AssertNoBackend(installed);
            });
            await RequiredAsync("native-fresh-factories", () =>
            {
                foreach (var pair in new[] { (_digest, "digest-page"), (_settings, "settings-page") })
                {
                    var first = CreateNative(pair.Item1, pair.Item2);
                    var second = CreateNative(pair.Item1, pair.Item2);
                    Require(!ReferenceEquals(first, second) && first.Parent is null && second.Parent is null,
                        "A native factory reused or pre-attached a page control.");
                }
                AssertNoBackend(installed);
                return Task.CompletedTask;
            });
            await RequiredAsync("first-view-activation", async () =>
            {
                _settingsRoot = CreateNative(_settings, "settings-page");
                await host.AttachAsync(_settingsRoot);
                await host.WaitReadyAsync(_settingsRoot, synthetic: true);
                Require(NativeProbes.OwnedWorkers(installed).Count > 0 && NativeProbes.SingletonExists(),
                    "The first committed page did not start and own the packaged backend.");
                var marker = Path.Combine(store.Profile(AcceptanceId), ".plugin-synthetic.json");
                Require(File.Exists(marker), "The acceptance profile lacks the synthetic-only marker.");
                using var document = JsonDocument.Parse(await File.ReadAllTextAsync(marker, lifetime));
                Require(document.RootElement.GetProperty("format").GetString() == "upcam-plugin-synthetic-v1",
                    "The profile did not confirm synthetic initialization.");
                await AssertLocalStatusAsync(manager, AcceptanceId, started: true);
            });

            await VerifyDraftNavigationAsync();
            await report.CheckAsync("local-preview-command", async () =>
            {
                var result = await manager.ExecuteCommandAsync(AcceptanceId, "preview-sample", lifetime).WaitAsync(TimeSpan.FromSeconds(45), lifetime);
                Require(result.Succeeded, "The registered local sample command failed; raw result suppressed.");
                await host.WaitReadyAsync(_digestRoot, synthetic: true);
                Require(NativeProbes.SqlCount(Path.Combine(store.Profile(AcceptanceId), "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;") == 0 &&
                    NativeProbes.SqlCount(Path.Combine(store.Profile(AcceptanceId), "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == 0,
                    "Local sample preview unexpectedly created a delivery or history run.");
            });
            await report.CheckAsync("native-layout-and-focus", async () =>
            {
                await host.SelectTabAsync(_digestRoot, "digest-tabs", 1);
                var editor = Find<TextBox>(_digestRoot, "window-start");
                Require(editor.Focus(FocusState.Programmatic), "Native range editing could not receive focus.");
                var originalWidth = _digestRoot.ActualWidth;
                host.Resize(900, 720);
                await host.WaitUntilAsync(() => _digestRoot.ActualWidth > 0 && _digestRoot.ActualWidth < originalWidth - 20);
                Require(Find<TabView>(_digestRoot, "digest-tabs").TabItems.Count == 3,
                    "The digest did not expose its three local native tabs.");
                host.AssertSingleWindow("Resizing opened another native window.");
                host.Resize(1280, 940);
                await host.WaitUntilAsync(() => Math.Abs(_digestRoot.ActualWidth - originalWidth) < 3);
            });
            await report.CheckAsync("screenshot-synthetic-range", () => host.CaptureAsync(report, "synthetic-range"), required: false);
            await VerifySyntheticBusinessAsync(store);
            var historyCount = NativeProbes.SqlCount(Path.Combine(store.Profile(AcceptanceId), "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;");
            var manualClaimCount = NativeProbes.SqlCount(Path.Combine(store.Profile(AcceptanceId), "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;");

            await RequiredAsync("disable-cleanup", async () =>
            {
                host.Detach();
                Require((await manager.DisableAsync(AcceptanceId, lifetime)).Succeeded, "Actual manager disable failed.");
                Require(ContributionsEmpty(manager) && !OnlyPlugin(manager, AcceptanceId).DesiredEnabled,
                    "Disabled plugin retained contributions or desired enabled intent.");
                Require(!(await manager.ExecuteCommandAsync(AcceptanceId, "status", lifetime)).Succeeded,
                    "A disabled plugin command remained executable.");
                await WaitNoBackendAsync(installed);
                AssertSavedState(store.Root, AcceptanceId, enabled: false, trusted: true);
            });
            await RequiredAsync("trusted-reenable", async () =>
            {
                Require((await manager.EnableAsync(AcceptanceId, cancellationToken: lifetime)).Succeeded, "Unchanged approved package could not be re-enabled.");
                AssertEnabled(manager, AcceptanceId);
                ReadFactories(manager, AcceptanceId);
                AssertNoBackend(installed);
                Require((await manager.ExecuteCommandAsync(AcceptanceId, "preview-sample", lifetime).WaitAsync(TimeSpan.FromSeconds(45), lifetime)).Succeeded,
                    "The registered sample command did not activate the committed re-enabled backend.");
                Require(NativeProbes.OwnedWorkers(installed).Count > 0 && NativeProbes.SingletonExists(),
                    "The first business command did not own the re-enabled backend session.");
                _digestRoot = CreateNative(_digest, "digest-page");
                await host.AttachAsync(_digestRoot);
                await host.WaitReadyAsync(_digestRoot, synthetic: true);
                Require(NativeProbes.OwnedWorkers(installed).Count > 0, "Re-enable's command-created backend stopped during page attachment.");
                AssertCanonicalContinuity(store.Profile(AcceptanceId), historyCount, manualClaimCount);
            });
            await RequiredAsync("shutdown-preserves-enabled-intent", async () =>
            {
                host.Detach();
                await manager.ShutdownAsync(lifetime);
                Require(ContributionsEmpty(manager) && OnlyPlugin(manager, AcceptanceId).DesiredEnabled,
                    "Shutdown retained contributions or discarded saved enabled intent.");
                AssertSavedState(store.Root, AcceptanceId, enabled: true, trusted: true);
                await WaitNoBackendAsync(installed);
            });
            await RequiredAsync("restart-exact-package-restoration", async () =>
            {
                await store.RestartManagerAsync();
                manager = store.Manager;
                await manager.DiscoverAsync(lifetime);
                var discovered = OnlyPlugin(manager, AcceptanceId);
                Require(discovered.IsTrusted && discovered.DesiredEnabled && !discovered.IsLoaded && discovered.State == PluginState.Disabled,
                    "A fresh manager did not restore unchanged trust and enabled intent without loading code at discovery.");
                var restored = await manager.RestoreEnabledAsync(lifetime);
                Require(restored.Count == 1 && restored.All(r => r.Succeeded), "Fresh manager restoration did not enable the exact trusted package.");
                AssertEnabled(manager, AcceptanceId);
                AssertNoBackend(installed);
                await AssertLocalStatusAsync(manager, AcceptanceId, started: false);
                ReadFactories(manager, AcceptanceId);
                _digestRoot = CreateNative(_digest, "digest-page");
                await host.AttachAsync(_digestRoot);
                await host.WaitReadyAsync(_digestRoot, synthetic: true);
                Require(NativeProbes.OwnedWorkers(installed).Count > 0, "Restored page did not start the owned backend.");
                Require(Directory.Exists(store.Profile(AcceptanceId)), "Restart changed the explicitly owned canonical profile.");
                AssertCanonicalContinuity(store.Profile(AcceptanceId), historyCount, manualClaimCount);
                host.Detach();
                await manager.ShutdownAsync(lifetime);
                await WaitNoBackendAsync(installed);
            });
            await RequiredAsync("package-mutation-revokes-trust", async () =>
            {
                // Mutate only the manager-installed copy inside this run's fresh temp store.
                await File.WriteAllTextAsync(Path.Combine(installed, "acceptance mutation.txt"), "Non-executable fingerprint mutation fixture.\n", lifetime);
                await manager.DiscoverAsync(lifetime);
                var changed = OnlyPlugin(manager, AcceptanceId);
                Require(!changed.IsTrusted && !changed.DesiredEnabled && changed.State == PluginState.Failed && ContributionsEmpty(manager) &&
                    changed.Diagnostics.Any(d => d.Code == PluginDiagnosticCode.PackageChanged),
                    "Rediscovery failed to revoke a changed loaded package's trust.");
                Require(!(await manager.EnableAsync(AcceptanceId, cancellationToken: lifetime)).Succeeded,
                    "A changed package ran without fresh explicit approval.");
                await store.RestartManagerAsync();
                manager = store.Manager;
                await manager.DiscoverAsync(lifetime);
                var fresh = OnlyPlugin(manager, AcceptanceId);
                Require(!fresh.IsTrusted && !fresh.DesiredEnabled && !fresh.IsLoaded,
                    "A fresh manager retained approval for mutated package contents.");
                Require((await manager.RestoreEnabledAsync(lifetime)).Count == 0 &&
                    !(await manager.EnableAsync(AcceptanceId, cancellationToken: lifetime)).Succeeded && ContributionsEmpty(manager),
                    "Mutated package restoration bypassed trust.");
                AssertNoBackend(installed);
            });
            await report.CheckAsync("acceptance-host-settings-only-binding", () =>
            {
                AssertHostSettings(store, AcceptanceId);
                return Task.CompletedTask;
            });
        }
        catch (StoppedPhase) { }
        catch (Exception error) { report.Fail("acceptance-phase", $"Unexpected {error.GetType().Name}; raw exception suppressed."); }
        finally
        {
            host.Detach();
            if (store is not null)
            {
                var owned = store;
                await report.CheckAsync("acceptance-dispose-cleanup", async () =>
                {
                    await owned.DisposeAsync();
                    await WaitNoBackendAsync(Path.Combine(owned.PluginsRoot, AcceptanceId));
                });
            }
            foreach (var id in AcceptanceChecks) report.Skip(id, "A prerequisite in the acceptance phase failed.");
        }
    }

    private async Task VerifyDraftNavigationAsync()
    {
        string? rangeStart = null;
        string? rangeEnd = null;
        await RequiredAsync("settings-draft-retention", async () =>
        {
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            Require(Find<TabView>(_settingsRoot, "settings-tabs").TabItems.Count == 6, "Settings does not expose six local native tabs.");
            var tenant = Find<TextBox>(_settingsRoot, "tenant-id");
            Require(tenant.Text == "demo-org", "The acceptance profile did not use synthetic tenant settings.");
            await host.EditAsync(tenant, "acceptance unsaved tenant draft");
            _digestRoot = CreateNative(_digest, "digest-page");
            await host.AttachAsync(_digestRoot);
            await host.WaitReadyAsync(_digestRoot, synthetic: true);
            await host.SelectTabAsync(_digestRoot, "digest-tabs", 1);
            var end = DateTime.Now.AddMinutes(-1);
            await host.EditAsync(Find<TextBox>(_digestRoot, "window-start"), end.AddHours(-2).ToString("yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture));
            await host.EditAsync(Find<TextBox>(_digestRoot, "window-end"), end.ToString("yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture));
            // Capture while the selected content is still attached and realized. Unloaded TabView templates
            // may release their visual descendants; navigation retention must compare the new factory to values.
            rangeStart = Find<TextBox>(_digestRoot, "window-start").Text;
            rangeEnd = Find<TextBox>(_digestRoot, "window-end").Text;
            _settingsRoot = CreateNative(_settings, "settings-page");
            await host.AttachAsync(_settingsRoot);
            await host.WaitReadyAsync(_settingsRoot, synthetic: true);
            Require(Find<TabView>(_settingsRoot, "settings-tabs").SelectedIndex == 2 &&
                Find<TextBox>(_settingsRoot, "tenant-id").Text == "acceptance unsaved tenant draft",
                "Factory recreation lost the unsaved settings document or selected local tab.");
            // Restore only this known synthetic draft before explicit business save.
            await host.EditAsync(Find<TextBox>(_settingsRoot, "tenant-id"), "demo-org");
        });
        await report.CheckAsync("screenshot-synthetic-settings", () => host.CaptureAsync(report, "synthetic-settings"), required: false);
        await RequiredAsync("range-and-tab-retention", async () =>
        {
            var previous = _digestRoot;
            Require(rangeStart is not null && rangeEnd is not null, "The attached digest range was not captured before navigation.");
            _digestRoot = CreateNative(_digest, "digest-page");
            await host.AttachAsync(_digestRoot);
            await host.WaitReadyAsync(_digestRoot, synthetic: true);
            Require(!ReferenceEquals(previous, _digestRoot) && Find<TabView>(_digestRoot, "digest-tabs").SelectedIndex == 1 &&
                Find<TextBox>(_digestRoot, "window-start").Text == rangeStart && Find<TextBox>(_digestRoot, "window-end").Text == rangeEnd,
                "Fresh digest factory lost the manual range draft or selected local tab.");
        });
    }

    private async Task VerifySyntheticBusinessAsync(OwnedStore store)
    {
        _settingsRoot = CreateNative(_settings, "settings-page");
        await host.AttachAsync(_settingsRoot);
        await host.WaitReadyAsync(_settingsRoot, synthetic: true);
        await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
        var probed = await report.CheckAsync("synthetic-project-probe", () => host.InvokeAsync(_settingsRoot, "probe-project"));
        var saved = false;
        if (probed)
            saved = await report.CheckAsync("synthetic-explicit-save", async () =>
            {
                await host.InvokeAsync(_settingsRoot, "save-settings");
                using var document = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(store.Profile(AcceptanceId), "settings.json"), lifetime));
                var settings = document.RootElement;
                Require(!settings.GetProperty("auto_send_enabled").GetBoolean() && settings.GetProperty("auto_send_enabled_since_local").GetString() == "",
                    "Explicit synthetic setup save expanded automatic sending authorization.");
                Require(settings.GetProperty("view").GetProperty("verified").GetBoolean(), "The actual project probe/save did not persist verified scope.");
            });
        else report.Skip("synthetic-explicit-save", "The native project probe failed.");
        await host.SelectTabAsync(_settingsRoot, "settings-tabs", 1);
        await host.SelectTabAsync(_settingsRoot, "account-tabs", 2);
        var models = await report.CheckAsync("synthetic-model-refresh", () => host.InvokeAsync(_settingsRoot, "refresh-models"));

        _digestRoot = CreateNative(_digest, "digest-page");
        await host.AttachAsync(_digestRoot);
        await host.WaitReadyAsync(_digestRoot, synthetic: true);
        await host.SelectTabAsync(_digestRoot, "digest-tabs", 1);
        var diagnosed = false;
        if (saved)
            diagnosed = await report.CheckAsync("synthetic-read-only-diagnose", async () =>
            {
                await host.InvokeAsync(_digestRoot, "diagnose");
                Require(!string.IsNullOrWhiteSpace(Find<TextBox>(_digestRoot, "diagnostic-output").Text),
                    "The native read-only diagnosis produced no visible output.");
                Require(Find<TextBox>(_digestRoot, "diagnostic-output").IsReadOnly, "The diagnostic output is editable.");
            });
        else report.Skip("synthetic-read-only-diagnose", "The explicit synthetic setup save did not succeed.");
        if (diagnosed) await VerifyDetailedExportsAsync(store);
        else
        {
            report.Skip("detailed-export-existing-target-failure", "The synthetic read-only diagnosis did not succeed.");
            report.Skip("detailed-export-new-target-success", "The synthetic read-only diagnosis did not succeed.");
        }
        var sent = false;
        if (saved && models)
            sent = await report.CheckAsync("synthetic-explicit-manual-send", async () =>
            {
                // Only the acceptance-ID package and its transport-free synthetic profile reach this action.
                Require(File.Exists(Path.Combine(store.Profile(AcceptanceId), ".plugin-synthetic.json")),
                    "The manual synthetic action requires a synthetic-only profile marker.");
                await host.InvokeAsync(_digestRoot, "run-manual");
                Require(NativeProbes.SqlCount(Path.Combine(store.Profile(AcceptanceId), "history.sqlite3"),
                    "SELECT COUNT(*) FROM digest_runs WHERE state='sent';") == 1,
                    "The synthetic manual operation did not persist exactly one confirmed history run.");
                Require(NativeProbes.SqlCount(Path.Combine(store.Profile(AcceptanceId), "delivery_ledger.sqlite3"),
                    "SELECT COUNT(*) FROM manual_attempts WHERE state='confirmed';") == 1,
                    "The synthetic manual operation did not persist its confirmed manual delivery claim.");
            });
        else report.Skip("synthetic-explicit-manual-send", "Synthetic save and model refresh are required before manual action.");
        await host.SelectTabAsync(_digestRoot, "digest-tabs", 2);
        await report.CheckAsync("synthetic-history", async () =>
        {
            await host.InvokeAsync(_digestRoot, "refresh-history");
            Require(sent && Find<ListView>(_digestRoot, "history-list").Items.Count == 1,
                "Native history did not show the one explicitly confirmed synthetic manual run.");
        });
        await report.CheckAsync("screenshot-synthetic-history", () => host.CaptureAsync(report, "synthetic-history"), required: false);
        if (saved && models && sent)
            await report.CheckAsync("scheduled-noop-preserves-focused-draft", () => VerifyNoOpScheduleAsync(store));
        else report.Skip("scheduled-noop-preserves-focused-draft", "A completed synthetic setup and manual history baseline are required.");
        if (saved && sent) await VerifyProfileNavigationAsync(store);
        else
        {
            report.Skip("profile-binding-persistence-rollback", "A saved synthetic default profile and history baseline are required.");
            report.Skip("profile-adoption-keeps-owned-state", "A saved synthetic default profile and history baseline are required.");
            report.Skip("profile-return-retains-drafts-and-history", "A saved synthetic default profile and history baseline are required.");
        }
    }

    private async Task VerifyProfileNavigationAsync(OwnedStore store)
    {
        var defaultProfile = store.Profile(AcceptanceId);
        var second = SecondProfile(store);
        var defaultIdentity = SyntheticLedgerIdentity(defaultProfile);
        var secondIdentity = SyntheticLedgerIdentity(second);
        Require(defaultIdentity != secondIdentity, "The independently initialized synthetic profiles share a delivery identity.");
        var history = NativeProbes.SqlCount(Path.Combine(defaultProfile, "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;");
        var claims = NativeProbes.SqlCount(Path.Combine(defaultProfile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;");
        const string defaultDraft = "default profile unsaved tenant draft";
        const string secondDraft = "adopted profile unsaved tenant draft";
        await RequiredAsync("profile-binding-persistence-rollback", async () =>
        {
            await OpenProfileSettingsAsync();
            // A same-path native default action establishes a real binding file for the sharing-failure fixture.
            await host.InvokeAsync(_settingsRoot, "default-profile", operationId: "bind-profile");
            AssertProfileBinding(store, defaultProfile);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            await host.EditAsync(Find<TextBox>(_settingsRoot, "tenant-id"), defaultDraft);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 0);
            await host.EditAsync(Find<TextBox>(_settingsRoot, "profile-folder"), second);
            var binding = Path.Combine(store.Root, "settings", AcceptanceId, "settings.json");
            var original = await File.ReadAllTextAsync(binding, lifetime);
            var oldWorkers = NativeProbes.OwnedWorkers(Path.Combine(store.PluginsRoot, AcceptanceId)).Order().ToArray();
            Require(oldWorkers.Length > 0, "The binding persistence fixture has no current owned worker to preserve.");
            using (var blocked = new FileStream(binding, FileMode.Open, FileAccess.Read, FileShare.None))
                await host.InvokeAsync(_settingsRoot, "adopt-profile", expectSuccess: false, operationId: "bind-profile", expectReady: false);
            Require(await File.ReadAllTextAsync(binding, lifetime) == original, "The failed native profile bind changed the saved host binding.");
            AssertProfileBinding(store, defaultProfile);
            Require(NativeProbes.OwnedWorkers(Path.Combine(store.PluginsRoot, AcceptanceId)).Order().SequenceEqual(oldWorkers) &&
                NativeProbes.SingletonExists(), "Failed precommit revocation closed/replaced the old worker or opened a target worker.");
            var task = store.Manager.GetBackgroundTaskSnapshots(AcceptanceId).Single();
            Require(task.State == PluginBackgroundTaskState.Stopped && !task.DesiredRunning && !task.ResumeOnStartup,
                "The failed binding precommit retained running intent or automatic restore.");
            await AssertLocalStatusAsync(store.Manager, AcceptanceId, started: true);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            Require(Find<TextBox>(_settingsRoot, "tenant-id").Text == defaultDraft,
                "Failed binding did not restore the previous profile and unsaved document.");
            AssertCanonicalContinuity(defaultProfile, history, claims);
            Require(SyntheticLedgerIdentity(second) == secondIdentity, "Failed binding replaced the existing second profile's ledger identity.");
        });
        await RequiredAsync("profile-adoption-keeps-owned-state", async () =>
        {
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 0);
            await host.EditAsync(Find<TextBox>(_settingsRoot, "profile-folder"), second);
            await host.InvokeAsync(_settingsRoot, "adopt-profile", operationId: "bind-profile");
            AssertProfileBinding(store, second);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            Require(Find<TextBox>(_settingsRoot, "tenant-id").Text == "demo-org", "Adoption copied the default profile's unsaved document into the second profile.");
            await host.EditAsync(Find<TextBox>(_settingsRoot, "tenant-id"), secondDraft);
            _digestRoot = CreateNative(_digest, "digest-page");
            await host.AttachAsync(_digestRoot);
            await host.WaitReadyAsync(_digestRoot, synthetic: true);
            await host.SelectTabAsync(_digestRoot, "digest-tabs", 2);
            Require(Find<ListView>(_digestRoot, "history-list").Items.Count == 0, "Adoption copied the default profile's history.");
            AssertCanonicalContinuity(second, 0, 0);
            Require(NativeProbes.SqlCount(Path.Combine(second, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == 0,
                "Adoption copied delivery claims into the independent existing profile.");
            Require(SyntheticLedgerIdentity(second) == secondIdentity && SyntheticLedgerIdentity(defaultProfile) == defaultIdentity,
                "Adoption replaced a profile's existing delivery ledger identity.");
        });
        await RequiredAsync("profile-return-retains-drafts-and-history", async () =>
        {
            await OpenProfileSettingsAsync();
            await host.InvokeAsync(_settingsRoot, "default-profile", operationId: "bind-profile");
            AssertProfileBinding(store, defaultProfile);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            Require(Find<TextBox>(_settingsRoot, "tenant-id").Text == defaultDraft, "Returning to default lost its separate unsaved document.");
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 0);
            await host.EditAsync(Find<TextBox>(_settingsRoot, "profile-folder"), second);
            await host.InvokeAsync(_settingsRoot, "adopt-profile", operationId: "bind-profile");
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            Require(Find<TextBox>(_settingsRoot, "tenant-id").Text == secondDraft, "Re-adoption lost the second profile's separate unsaved document.");
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 0);
            await host.InvokeAsync(_settingsRoot, "default-profile", operationId: "bind-profile");
            AssertProfileBinding(store, defaultProfile);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
            Require(Find<TextBox>(_settingsRoot, "tenant-id").Text == defaultDraft, "The final default return lost its retained unsaved draft.");
            // Restore the known synthetic tenant and explicitly save so the pre-existing lifecycle cases keep their setup baseline.
            await host.EditAsync(Find<TextBox>(_settingsRoot, "tenant-id"), "demo-org");
            await host.InvokeAsync(_settingsRoot, "save-settings");
            AssertCanonicalContinuity(defaultProfile, history, claims);
            AssertCanonicalContinuity(second, 0, 0);
            Require(NativeProbes.SqlCount(Path.Combine(second, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == 0,
                "Returning between profiles changed the second profile's original empty delivery ledger.");
            Require(SyntheticLedgerIdentity(second) == secondIdentity && SyntheticLedgerIdentity(defaultProfile) == defaultIdentity,
                "Profile navigation changed either existing delivery ledger identity.");
            _digestRoot = CreateNative(_digest, "digest-page");
            await host.AttachAsync(_digestRoot);
            await host.WaitReadyAsync(_digestRoot, synthetic: true);
            await host.SelectTabAsync(_digestRoot, "digest-tabs", 2);
            Require(Find<ListView>(_digestRoot, "history-list").Items.Count == history,
                "Returning to default did not restore its original local history.");
        });
    }

    private async Task OpenProfileSettingsAsync()
    {
        _settingsRoot = CreateNative(_settings, "settings-page");
        await host.AttachAsync(_settingsRoot);
        await host.WaitReadyAsync(_settingsRoot, synthetic: true);
        await host.SelectTabAsync(_settingsRoot, "settings-tabs", 0);
    }

    private static string SecondProfile(OwnedStore store) => Path.Combine(store.Root, "existing synthetic standalone profile");

    private static string SyntheticLedgerIdentity(string profile)
    {
        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(profile, ".plugin-synthetic.json")));
        Require(document.RootElement.GetProperty("format").GetString() == "upcam-plugin-synthetic-v1",
            "Profile identity assertions require an explicit synthetic-only marker.");
        return document.RootElement.GetProperty("delivery_ledger_id").GetString()
            ?? throw new AcceptanceFailure("The synthetic profile has no independent delivery ledger identity.");
    }

    private static void AssertProfileBinding(OwnedStore store, string expected)
    {
        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(store.Root, "settings", AcceptanceId, "settings.json")));
        var value = document.RootElement.GetProperty("profile-directory").GetString();
        Require(value is not null && string.Equals(Upcam.Workbench.PluginWorkerClient.CanonicalizeDirectory(value, mustExist: true),
            Upcam.Workbench.PluginWorkerClient.CanonicalizeDirectory(expected, mustExist: true), StringComparison.OrdinalIgnoreCase),
            "The actual native profile action did not persist the expected canonical directory binding.");
    }

    private async Task VerifyDetailedExportsAsync(OwnedStore store)
    {
        var directory = Path.Combine(store.Root, "explicit synthetic detailed exports");
        Directory.CreateDirectory(directory);
        var existing = Path.Combine(directory, "existing sentinel.json");
        const string sentinel = "{\"owned_acceptance_sentinel\":true}\n";
        await File.WriteAllTextAsync(existing, sentinel, lifetime);
        var before = Find<TextBox>(_digestRoot, "diagnostic-output").Text;
        await report.CheckAsync("detailed-export-existing-target-failure", async () =>
        {
            await OpenDetailedExportAsync();
            await host.EditAsync(Find<TextBox>(_digestRoot, "diagnostic-export-path"), existing);
            await host.SetCheckedAsync(_digestRoot, "confirm-detailed-export", true);
            var status = await host.InvokeAsync(_digestRoot, "export-detailed-diagnostic", expectSuccess: false);
            Require(status.Contains("权限", StringComparison.Ordinal) && status.Contains("空间", StringComparison.Ordinal),
                "A failed detailed file save did not show the actionable permissions/space message.");
            Require(await File.ReadAllTextAsync(existing, lifetime) == sentinel,
                "The detailed export failure replaced the owned existing-target sentinel.");
            AssertRetainedDiagnosis(before);
        });
        await report.CheckAsync("detailed-export-new-target-success", async () =>
        {
            // A new name in the same real parent takes the actual successful writer path.
            var target = Path.Combine(directory, "new synthetic detailed export.json");
            Require(!File.Exists(target), "The fresh detailed export success target already exists.");
            await OpenDetailedExportAsync();
            await host.EditAsync(Find<TextBox>(_digestRoot, "diagnostic-export-path"), target);
            await host.SetCheckedAsync(_digestRoot, "confirm-detailed-export", true);
            var status = await host.InvokeAsync(_digestRoot, "export-detailed-diagnostic");
            Require(status.Contains("详细诊断已保存", StringComparison.Ordinal),
                "A confirmed detailed export did not show its explicit saved result.");
            Require(File.Exists(target) && new FileInfo(target).Length > 0,
                "The confirmed detailed export did not create a nonempty owned output file.");
            AssertRetainedDiagnosis(before);
        });
        // A factory redraw may collapse the expander after either operation; retain ordinary explicit consent semantics.
        await OpenDetailedExportAsync();
        await host.SetCheckedAsync(_digestRoot, "confirm-detailed-export", false);
        Find<Expander>(_digestRoot, "diagnostic-export").IsExpanded = false;
    }

    private async Task OpenDetailedExportAsync()
    {
        Find<Expander>(_digestRoot, "diagnostic-export").IsExpanded = true;
        _digestRoot.UpdateLayout();
        await host.WaitUntilAsync(() => TryFind<TextBox>(_digestRoot, "diagnostic-export-path") is not null);
    }

    private void AssertRetainedDiagnosis(string expected)
    {
        var output = Find<TextBox>(_digestRoot, "diagnostic-output");
        Require(!string.IsNullOrWhiteSpace(expected) && output.IsReadOnly && output.Text == expected,
            "Detailed export cleared or replaced the successful read-only diagnosis instead of retaining it.");
    }

    private async Task VerifyNoOpScheduleAsync(OwnedStore store)
    {
        var profile = store.Profile(AcceptanceId);
        var history = NativeProbes.SqlCount(Path.Combine(profile, "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;");
        var deliveries = NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;");
        var attempts = NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;");
        try
        {
            _settingsRoot = CreateNative(_settings, "settings-page");
            await host.AttachAsync(_settingsRoot);
            await host.WaitReadyAsync(_settingsRoot, synthetic: true);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 5);
            // Save explicitly grants only this synthetic fixture authorization. Both slots are two hours ahead;
            // normal enabled-since handling excludes earlier occurrences, including a clock wrap to tomorrow.
            var future = DateTime.Now.AddHours(2).ToString("HH:mm", System.Globalization.CultureInfo.InvariantCulture);
            await host.EditAsync(Find<TextBox>(_settingsRoot, "morning-run-at"), future);
            await host.EditAsync(Find<TextBox>(_settingsRoot, "afternoon-run-at"), future);
            Find<ToggleSwitch>(_settingsRoot, "auto-run").IsOn = true;
            await host.InvokeAsync(_settingsRoot, "save-settings");
            await host.WaitUntilAsync(() => store.Manager.GetBackgroundTaskSnapshots(AcceptanceId).Single().State == PluginBackgroundTaskState.Running);
            var managed = store.Manager.GetBackgroundTaskSnapshots(AcceptanceId).Single();
            Require(managed.Id == "digest-scheduler" && managed.DesiredRunning && !managed.ResumeOnStartup && !managed.RequiresPluginStart,
                "Explicit synthetic business Save did not start the actual managed task with default-off host resume.");
            using (var document = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(profile, "settings.json"), lifetime)))
            {
                var settings = document.RootElement;
                Require(settings.GetProperty("auto_send_enabled").GetBoolean() &&
                    !string.IsNullOrWhiteSpace(settings.GetProperty("auto_send_enabled_since_local").GetString()) &&
                    settings.GetProperty("morning_run_at").GetString() == future && settings.GetProperty("afternoon_run_at").GetString() == future,
                    "The normal synthetic save did not persist explicit future-only schedule authorization.");
            }
            _digestRoot = CreateNative(_digest, "digest-page");
            await host.AttachAsync(_digestRoot);
            await host.WaitReadyAsync(_digestRoot, synthetic: true);
            await host.SelectTabAsync(_digestRoot, "digest-tabs", 1);
            var revision = ReadRevision(Find<TextBlock>(_digestRoot, "last-schedule-check").Text);
            var foregroundResult = Find<TextBlock>(_digestRoot, "operation-result").Text;
            var editor = Find<TextBox>(_digestRoot, "window-start");
            var draft = DateTime.Now.AddHours(-3).ToString("yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture);
            await host.EditAsync(editor, draft);
            Require(editor.Focus(FocusState.Keyboard), "The native schedule regression editor could not receive focus.");
            editor.Select(5, 4);
            var selectionStart = editor.SelectionStart;
            var selectionLength = editor.SelectionLength;
            Require(ReferenceEquals(FocusManager.GetFocusedElement(_digestRoot.XamlRoot), editor),
                "The native range editor did not own focus before the scheduled check.");
            var elapsed = System.Diagnostics.Stopwatch.StartNew();
            await host.WaitUntilAsync(() =>
                elapsed.Elapsed >= TimeSpan.FromSeconds(16) &&
                ReadRevision(Find<TextBlock>(_digestRoot, "last-schedule-check").Text) > revision, TimeSpan.FromSeconds(35));
            var scheduled = Find<TextBlock>(_digestRoot, "last-schedule-check").Text;
            Require(scheduled.Contains(" · succeeded · ", StringComparison.Ordinal) && scheduled.Contains("本次没有启动到期任务", StringComparison.Ordinal),
                "The real schedule poll did not confirm a successful no-op check.");
            Require(Find<TextBlock>(_digestRoot, "operation-result").Text == foregroundResult,
                "A background schedule poll changed the foreground operation result or completion revision.");
            Require(ReferenceEquals(Find<TextBox>(_digestRoot, "window-start"), editor) &&
                ReferenceEquals(FocusManager.GetFocusedElement(_digestRoot.XamlRoot), editor) &&
                editor.Text == draft && editor.SelectionStart == selectionStart && editor.SelectionLength == selectionLength && editor.IsEnabled,
                "A real no-op schedule poll replaced or disabled the editor, or lost its focus, selection or draft.");
            Require(NativeProbes.SqlCount(Path.Combine(profile, "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;") == history &&
                NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == deliveries &&
                NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;") == attempts,
                "The future-only no-op poll unexpectedly created a run or delivery claim.");
        }
        finally
        {
            _settingsRoot = CreateNative(_settings, "settings-page");
            await host.AttachAsync(_settingsRoot);
            await host.WaitReadyAsync(_settingsRoot, synthetic: true);
            await host.SelectTabAsync(_settingsRoot, "settings-tabs", 5);
            Find<ToggleSwitch>(_settingsRoot, "auto-run").IsOn = false;
            await host.InvokeAsync(_settingsRoot, "save-settings");
            await host.WaitUntilAsync(() => store.Manager.GetBackgroundTaskSnapshots(AcceptanceId).Single().State == PluginBackgroundTaskState.Stopped);
            Require(!store.Manager.GetBackgroundTaskSnapshots(AcceptanceId).Single().DesiredRunning &&
                !store.Manager.GetBackgroundTaskSnapshots(AcceptanceId).Single().ResumeOnStartup,
                "Disabling the fixture automatic grant did not stop/revoke actual managed task intent.");
            using var document = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(profile, "settings.json"), lifetime));
            Require(!document.RootElement.GetProperty("auto_send_enabled").GetBoolean() &&
                document.RootElement.GetProperty("auto_send_enabled_since_local").GetString() == "",
                "The regression fixture did not revoke its explicit synthetic schedule authorization.");
        }
    }

    private async Task RunProductionAsync(string packageDirectory)
    {
        OwnedStore? store = null;
        try
        {
            await RequiredAsync("production-package-contract", () => VerifyManifestAsync(packageDirectory, ProductionId));
            store = new OwnedStore("production unconfigured", report);
            var manager = store.Manager;
            var installed = Path.Combine(store.PluginsRoot, ProductionId);
            await RequiredAsync("production-install-trust-enable", async () =>
            {
                Require((await manager.InstallAsync(packageDirectory, lifetime)).Succeeded, "Production fixture installation failed.");
                await manager.DiscoverAsync(lifetime);
                Require(!OnlyPlugin(manager, ProductionId).IsTrusted && !OnlyPlugin(manager, ProductionId).IsLoaded,
                    "Production installation did not retain the real trust gate.");
                Require((await manager.EnableAsync(ProductionId, confirmTrust: true, cancellationToken: lifetime)).Succeeded,
                    "Explicit production fixture activation failed.");
                AssertEnabled(manager, ProductionId);
                ReadFactories(manager, ProductionId);
                AssertNoBackend(installed);
            });
            await RequiredAsync("production-local-status-before-view", async () =>
            {
                await AssertLocalStatusAsync(manager, ProductionId, started: false);
                AssertNoBackend(installed);
            });
            await RequiredAsync("production-local-setup", async () =>
            {
                _settingsRoot = CreateNative(_settings, "settings-page");
                await host.AttachAsync(_settingsRoot);
                await host.WaitReadyAsync(_settingsRoot, synthetic: false);
                await host.SelectTabAsync(_settingsRoot, "settings-tabs", 2);
                Require(Find<TextBox>(_settingsRoot, "tenant-id").Text == "" && Find<TextBox>(_settingsRoot, "project-id").Text == "",
                    "Fresh production setup inherited a tenant/project binding.");
                await AssertLocalStatusAsync(manager, ProductionId, started: true);
            });
            await report.CheckAsync("screenshot-production-setup", () => host.CaptureAsync(report, "production-unconfigured-project"), required: false);
            await report.CheckAsync("production-empty-secret-controls", async () =>
            {
                await host.SelectTabAsync(_settingsRoot, "settings-tabs", 1);
                var groups = new[]
                {
                    new[] { "teambition_app_id", "teambition_app_secret" },
                    new[] { "dingtalk_webhook", "dingtalk_signing_secret" },
                    new[] { "openai_api_key" }
                };
                for (var index = 0; index < groups.Length; index++)
                {
                    await host.SelectTabAsync(_settingsRoot, "account-tabs", index);
                    foreach (var id in groups[index])
                    {
                        var box = Find<PasswordBox>(_settingsRoot, "secret-" + id);
                        Require(box.Password.Length == 0 && box.PlaceholderText == "Not saved locally",
                            "Fresh production setup has a saved/drafted external credential.");
                    }
                }
            });
            await report.CheckAsync("production-no-authorization-expansion", async () =>
            {
                await host.SelectTabAsync(_settingsRoot, "settings-tabs", 5);
                Require(!Find<ToggleSwitch>(_settingsRoot, "auto-run").IsOn, "Production startup enabled automatic sending.");
                var settings = Path.Combine(store.Profile(ProductionId), "settings.json");
                if (File.Exists(settings))
                {
                    using var document = JsonDocument.Parse(await File.ReadAllTextAsync(settings, lifetime));
                    Require(!document.RootElement.GetProperty("auto_send_enabled").GetBoolean() &&
                        document.RootElement.GetProperty("auto_send_enabled_since_local").GetString() == "" &&
                        !document.RootElement.GetProperty("api_contract_verified").GetBoolean(),
                        "Production setup expanded automatic sending or API verification authorization.");
                }
            });
            await report.CheckAsync("production-empty-history-and-ledger", async () =>
            {
                _digestRoot = CreateNative(_digest, "digest-page");
                await host.AttachAsync(_digestRoot);
                await host.WaitReadyAsync(_digestRoot, synthetic: false);
                await host.SelectTabAsync(_digestRoot, "digest-tabs", 2);
                Require(Find<ListView>(_digestRoot, "history-list").Items.Count == 0, "Fresh production history is not empty.");
                var profile = store.Profile(ProductionId);
                Require(NativeProbes.SqlCount(Path.Combine(profile, "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;") == 0 &&
                    NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == 0 &&
                    NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;") == 0,
                    "Production local setup created a business run or delivery claim.");
            });
            await report.CheckAsync("production-local-files-only", () =>
            {
                var profile = store.Profile(ProductionId);
                Require(!File.Exists(Path.Combine(profile, ".plugin-synthetic.json")) &&
                    !File.Exists(Path.Combine(profile, "openai_models.json")),
                    "Production local setup created synthetic state or a fetched model cache.");
                // The production adapter provisions local DEFAULT_SETTINGS on a fresh handshake so the same profile
                // can be reopened before a first explicit business save. Verify unconfigured defaults at runtime.
                using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(profile, "settings.json")));
                var settings = document.RootElement;
                Require(settings.GetProperty("tenant_id").GetString() == "" && settings.GetProperty("project_id").GetString() == "" &&
                    settings.GetProperty("owner_names").EnumerateObject().Count() == 0 &&
                    settings.GetProperty("status_names").EnumerateObject().Count() == 0 &&
                    !settings.GetProperty("auto_send_enabled").GetBoolean(),
                    "Production local settings contain adopted business scope, mappings or automatic sending authorization.");
                return Task.CompletedTask;
            });
            await report.CheckAsync("production-host-settings-only-binding", () =>
            {
                AssertHostSettings(store, ProductionId);
                return Task.CompletedTask;
            });
        }
        catch (StoppedPhase) { }
        catch (Exception error) { report.Fail("production-phase", $"Unexpected {error.GetType().Name}; raw exception suppressed."); }
        finally
        {
            host.Detach();
            if (store is not null)
            {
                var owned = store;
                await report.CheckAsync("production-dispose-cleanup", async () =>
                {
                    await owned.DisposeAsync();
                    await WaitNoBackendAsync(Path.Combine(owned.PluginsRoot, ProductionId));
                });
            }
            foreach (var id in ProductionChecks) report.Skip(id, "A prerequisite in the production phase failed.");
        }
    }

    private async Task RequiredAsync(string id, Func<Task> action)
    {
        if (!await report.CheckAsync(id, action)) throw new StoppedPhase();
    }

    private static async Task VerifyManifestAsync(string package, string expectedId)
    {
        OwnedStore.RejectLinks(package);
        using var document = JsonDocument.Parse(await File.ReadAllTextAsync(Path.Combine(package, "plugin.json")));
        var manifest = document.RootElement;
        Require(manifest.GetProperty("id").GetString() == expectedId && manifest.GetProperty("version").GetString() == "0.1.1" &&
            manifest.GetProperty("apiVersion").GetInt32() == 1 && manifest.GetProperty("minimumHostVersion").GetString() == "0.2.0" &&
            manifest.GetProperty("entryType").GetString() == "Upcam.Workbench.DigestPlugin",
            "The supplied package does not match the expected independent build ID/version/API/entry point.");
        Require(File.Exists(Path.Combine(package, "worker", "UPCAMPluginWorker.exe")), "The package lacks its private plugin worker.");
        var forbidden = new[] { "Workbench.Sdk.dll", "Workbench.Core.dll", "Workbench.WinUI.Sdk.dll", "Microsoft.WinUI.dll",
            "Microsoft.WindowsAppRuntime.Bootstrap.dll", "WinRT.Runtime.dll", "UPCAMTaskDigest.dll", "coreclr.dll", "hostfxr.dll" };
        // The private Python worker may contain native Python dependencies; only the plugin's managed root is checked here.
        Require(!Directory.EnumerateFiles(package, "*.dll").Any(file => forbidden.Contains(Path.GetFileName(file), StringComparer.OrdinalIgnoreCase)),
            "The plugin managed root bundles a host SDK/framework or standalone window assembly.");
    }

    private static PluginSnapshot OnlyPlugin(PluginManager manager, string id)
    {
        Require(manager.Plugins.Count == 1 && manager.Plugins[0].Manifest?.Id == id, "The isolated manager did not discover exactly the requested package.");
        return manager.Plugins[0];
    }

    private static void AssertEnabled(PluginManager manager, string id)
    {
        var plugin = OnlyPlugin(manager, id);
        Require(plugin.State == PluginState.Enabled && plugin.IsLoaded && plugin.IsTrusted && plugin.DesiredEnabled,
            "The real manager did not commit a loaded/trusted/enabled runtime.");
    }

    private void ReadFactories(PluginManager manager, string id)
    {
        var views = manager.Contributions.Views.Where(v => v.PluginId == id).Select(v => v.View).ToArray();
        Require(views.Length == 2 && views.All(view => view is WinUiView), "The real manager did not publish two shared-identity native view factories.");
        _digest = views.OfType<WinUiView>().Single(v => v.Kind == WinUiViewKind.Page);
        _settings = views.OfType<WinUiView>().Single(v => v.Kind == WinUiViewKind.Settings);
    }

    private FrameworkElement CreateNative(WinUiView view, string expectedId)
    {
        host.AssertUiThread();
        var element = view.CreateView();
        Require(element is not null && element.DispatcherQueue.HasThreadAccess && AutomationProperties.GetAutomationId(element) == expectedId,
            "The factory did not return the expected fresh FrameworkElement on the host UI thread.");
        return element;
    }

    private static bool ContributionsEmpty(PluginManager manager) => manager.Contributions.Views.Count == 0 &&
        manager.Contributions.Commands.Count == 0 && manager.Contributions.Settings.Count == 0;

    private static bool PluginAssemblyLoaded() => AppDomain.CurrentDomain.GetAssemblies().Any(a => a.GetName().Name == "UPCAM.Workbench");

    private static void AssertNoBackend(string installed)
    {
        Require(NativeProbes.OwnedWorkers(installed).Count == 0 && !NativeProbes.SingletonExists(),
            "Plugin code started a worker or retained the singleton outside the committed first-view/command session.");
    }

    private Task WaitNoBackendAsync(string installed) => host.WaitUntilAsync(() =>
        NativeProbes.OwnedWorkers(installed).Count == 0 && !NativeProbes.SingletonExists(), TimeSpan.FromSeconds(25));

    private static async Task AssertLocalStatusAsync(PluginManager manager, string id, bool started)
    {
        var status = await manager.ExecuteCommandAsync(id, "status");
        Require(status.Succeeded && status.Message == (started ? "UPCAM backend ready." : "UPCAM backend not started."),
            "The real manager's local status command returned an unexpected state.");
    }

    private static void AssertSavedState(string root, string id, bool enabled, bool trusted)
    {
        using var document = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "plugin-state.json")));
        var value = document.RootElement.GetProperty("plugins").GetProperty(id);
        Require(value.GetProperty("enabled").GetBoolean() == enabled, "Saved enabled intent differs from the lifecycle operation.");
        var fingerprint = value.GetProperty("trustedFingerprint");
        Require(trusted ? fingerprint.ValueKind == JsonValueKind.String && fingerprint.GetString()?.Length == 64 : fingerprint.ValueKind == JsonValueKind.Null,
            "Saved exact-package trust differs from the lifecycle operation.");
    }

    private static void AssertHostSettings(OwnedStore store, string id)
    {
        var file = Path.Combine(store.Root, "settings", id, "settings.json");
        if (!File.Exists(file)) return;
        using var document = JsonDocument.Parse(File.ReadAllText(file));
        Require(document.RootElement.ValueKind == JsonValueKind.Object && document.RootElement.EnumerateObject().All(property =>
            property.Name == "schedule-binding-v1" && (property.Value.ValueKind == JsonValueKind.Null || RuntimeBindingMetadata(property.Value)) ||
            property.Name == "profile-directory" && property.Value.ValueKind == JsonValueKind.String &&
            string.Equals(Upcam.Workbench.PluginWorkerClient.CanonicalizeDirectory(property.Value.GetString()!, mustExist: true),
                Upcam.Workbench.PluginWorkerClient.CanonicalizeDirectory(store.Profile(id), mustExist: true), StringComparison.OrdinalIgnoreCase)),
            "The host JSON settings contain data beyond non-secret profile/runtime binding metadata.");
    }

    private static bool RuntimeBindingMetadata(JsonElement binding) => binding.ValueKind == JsonValueKind.Object &&
        binding.EnumerateObject().Count() == 3 && binding.TryGetProperty("schema", out var schema) && schema.GetInt32() == 1 &&
        binding.TryGetProperty("profileDirectory", out var path) && path.ValueKind == JsonValueKind.String &&
        binding.TryGetProperty("proof", out var proof) && proof.ValueKind == JsonValueKind.String && proof.GetString() is { Length: 64 } hash &&
        hash.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f');

    private static void AssertCanonicalContinuity(string profile, long expectedHistory, long expectedClaims)
    {
        Require(NativeProbes.SqlCount(Path.Combine(profile, "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;") == expectedHistory &&
            NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;") == expectedClaims,
            "Reopening the canonical profile changed its history or manual delivery ledger counts.");
    }

    private void SkipRemaining(string reason)
    {
        foreach (var id in AcceptanceChecks.Concat(ProductionChecks).Concat(RuntimeAcceptanceRunner.ExpectedChecks)) report.Skip(id, reason);
    }

    private sealed class StoppedPhase : Exception { }
}
