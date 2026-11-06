using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Workbench.Core;
using Workbench.Sdk;
using Workbench.WinUI.Sdk;
using static Workbench.Digest.Acceptance.NativeHost;

namespace Workbench.Digest.Acceptance;

/// <summary>Actual host 0.2 runtime and Windows secret services, using independently installed synthetic packages.</summary>
internal sealed class RuntimeAcceptanceRunner(NativeHost host, RunOptions options, AcceptanceReport report, CancellationToken lifetime)
{
    private const string Id = "com.upcam.task-digest.acceptance";
    private const string TaskId = "digest-scheduler";
    private const string SecretId = "upcam.resume-binding-key";
    private const string BindingKey = "schedule-binding-v1";
    private int _factoryCalls;
    private string? _initialFingerprint;
    private FrameworkElement _settings = null!;
    internal static readonly string[] ExpectedChecks =
    [
        "runtime-fixture-preparation", "runtime-default-stopped-no-page-loop", "runtime-explicit-start-no-resume",
        "runtime-generated-secret-private", "runtime-resume-without-view", "runtime-user-stop-revokes-running",
        "runtime-changed-authorization-rejects-restore", "runtime-missing-secret-rejects-restore",
        "runtime-profile-switch-revokes-resume", "runtime-disable-running-cleanup", "runtime-dispose-cleanup"
    ];

    internal async Task RunAsync()
    {
        OwnedStore? store = null;
        try
        {
            store = new OwnedStore("runtime 0.2", report);
            var profile = store.Profile(Id);
            var second = Path.Combine(store.Root, "second existing synthetic profile");
            var installed = Path.Combine(store.PluginsRoot, Id);
            await RequiredAsync("runtime-fixture-preparation", async () =>
            {
                await VerifyAdmissionPolicyAsync();
                await PrepareProfileAsync(profile, automatic: true);
                await PrepareProfileAsync(second, automatic: false);
                await NoWorkerAsync(options.PackageDirectory);
                Require((await store.Manager.InstallAsync(options.PackageDirectory, lifetime)).Succeeded, "Runtime fixture installation failed.");
                await store.Manager.DiscoverAsync(lifetime);
                Require((await store.Manager.EnableAsync(Id, confirmTrust: true, cancellationToken: lifetime)).Succeeded,
                    "Runtime fixture trusted enable failed.");
                AssertStopped(store.Manager, resume: false);
                Require(store.Manager.GetSecretDefinitions(Id).Count == 1 && store.Manager.GetSecretDefinitions(Id)[0].Id == SecretId &&
                    store.Manager.IsSecretStorageSupported(Id), "The actual runtime did not declare only the supported generated binding secret.");
                Require(await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) is null,
                    "Initialization generated a binding key before an explicit authorized start.");
                await NoWorkerAsync(installed);
            });
            await RequiredAsync("runtime-default-stopped-no-page-loop", async () =>
            {
                await OpenSettingsAsync(store.Manager);
                await host.SelectTabAsync(_settings, "settings-tabs", 5);
                Require(Find<ToggleSwitch>(_settings, "auto-run").IsOn && !Find<ToggleSwitch>(_settings, "scheduler-resume").IsOn,
                    "The prepared saved automatic grant or default-off host resume preference was not represented correctly.");
                var before = ReadRevision(Find<TextBlock>(_settings, "last-schedule-check").Text);
                var elapsed = System.Diagnostics.Stopwatch.StartNew();
                await host.WaitUntilAsync(() => elapsed.Elapsed >= TimeSpan.FromSeconds(16));
                AssertStopped(store.Manager, resume: false);
                Require(ReadRevision(Find<TextBlock>(_settings, "last-schedule-check").Text) == before,
                    "Opening a page ran a private schedule loop despite the real host task being stopped.");
                Require(await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) is null,
                    "Page load minted a restore binding key without explicit start.");
                AssertNoDeliveries(profile);
            });
            await RequiredAsync("runtime-explicit-start-no-resume", async () =>
            {
                // Exercise the real host/plugin start overlap, with no caller-owned callback waiter or fake task service.
                var genericStart = store.Manager.StartBackgroundTaskAsync(Id, TaskId, lifetime);
                var nativeStart = host.InvokeAsync(_settings, "scheduler-start").WaitAsync(TimeSpan.FromSeconds(45), lifetime);
                await Task.WhenAll(genericStart, nativeStart);
                Require((await genericStart).Succeeded, "The overlapping actual generic host Start failed.");
                await WaitRunningBoundAsync(store.Manager);
                var task = Snapshot(store.Manager);
                Require(!task.RequiresPluginStart && task.DesiredRunning && !task.ResumeOnStartup,
                    "Explicit authorized start changed default-off host resume or required an unavailable confirmation bridge.");
                AssertNoDeliveries(profile);
            });
            string? generated = null;
            JsonObject? originalBinding = null;
            await RequiredAsync("runtime-generated-secret-private", async () =>
            {
                generated = await store.Manager.ReadSecretAsync(Id, SecretId, lifetime);
                Require(!string.IsNullOrEmpty(generated), "Explicit authorized start did not create the generated host secret.");
                var bytes = Convert.FromBase64String(generated);
                originalBinding = await ReadBindingAsync(store.Manager);
                Require(originalBinding is not null, "Explicit start did not persist non-secret binding metadata.");
                try
                {
                    Require(bytes.Length == 32 && _initialFingerprint is not null, "The generated binding key or fixture authorization fingerprint is invalid.");
                    var directory = Upcam.Workbench.PluginWorkerClient.CanonicalizeDirectory(profile, mustExist: true);
                    Require(originalBinding["schema"]?.GetValue<int>() == 1 && originalBinding.Count == 3 &&
                        originalBinding["profileDirectory"]?.GetValue<string>() == directory,
                        "The actual runtime binding metadata has an unexpected schema or canonical profile.");
                    var expected = HMACSHA256.HashData(bytes, Encoding.UTF8.GetBytes(directory + "\n" + _initialFingerprint));
                    var actual = Convert.FromHexString(originalBinding["proof"]!.GetValue<string>());
                    Require(CryptographicOperations.FixedTimeEquals(expected, actual), "The persisted proof does not bind the canonical profile and exact backend authorization fingerprint.");
                }
                finally { CryptographicOperations.ZeroMemory(bytes); }
                AssertNoPlaintextKey(store, generated);
                Require(Directory.EnumerateFiles(Path.Combine(store.Root, "secrets", Id), "*.bin").Any(),
                    "The real Windows secret service did not create encrypted storage.");
            });
            await RequiredAsync("runtime-resume-without-view", async () =>
            {
                // Toggle the real native preference; it is separate from saved automatic sending and host Start.
                Find<ToggleSwitch>(_settings, "scheduler-resume").IsOn = true;
                await host.WaitUntilAsync(() => Snapshot(store.Manager).ResumeOnStartup);
                await host.WaitReadyAsync(_settings, synthetic: true);
                var factories = _factoryCalls;
                host.Detach();
                var lifecycleFaults = BackgroundFaultCount(store.Manager);
                await store.Manager.ShutdownAsync(lifetime);
                Require(BackgroundFaultCount(store.Manager) == lifecycleFaults,
                    "Normal HostShutdown added a BackgroundTaskFailed diagnostic.");
                await NoWorkerAsync(installed);
                await RestartRestoreAsync(store);
                await WaitRunningBoundAsync(store.Manager);
                Require(_factoryCalls == factories && NativeProbes.OwnedWorkers(installed).Count > 0 &&
                    Snapshot(store.Manager).ResumeOnStartup && Snapshot(store.Manager).DesiredRunning,
                    "Host restore did not start the managed scheduler and worker without any view factory.");
                Require(await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) == generated &&
                    JsonNode.DeepEquals(await ReadBindingAsync(store.Manager), originalBinding),
                    "Restore minted or changed the existing secret/binding instead of validating it.");
                AssertNoDeliveries(profile);
                AssertNoPlaintextKey(store, generated!);
            });
            await RequiredAsync("runtime-user-stop-revokes-running", async () =>
            {
                await OpenSettingsAsync(store.Manager);
                await host.SelectTabAsync(_settings, "settings-tabs", 5);
                var idleWorkers = NativeProbes.OwnedWorkers(installed).Order().ToArray();
                Require(idleWorkers.Length > 0, "The UserStop fixture has no idle session-owned worker.");
                await host.InvokeAsync(_settings, "scheduler-stop");
                await host.WaitUntilAsync(() => Snapshot(store.Manager).State == PluginBackgroundTaskState.Stopped);
                Require(!Snapshot(store.Manager).DesiredRunning, "UserStop retained desired running intent.");
                Require((await store.Manager.StopBackgroundTaskAsync(Id, TaskId, lifetime)).Succeeded, "The actual host could not observe the stopped task idempotently.");
                // A stopped execution token must not later invalidate an unrelated manual backend operation.
                Require((await store.Manager.ExecuteCommandAsync(Id, "preview-sample", lifetime)).Succeeded,
                    "Stopping the managed task killed or poisoned the subsequent local manual session.");
                Require(NativeProbes.OwnedWorkers(installed).Order().SequenceEqual(idleWorkers),
                    "An idle scheduler stop cancelled or replaced the plugin-session-owned worker.");
                AssertNoDeliveries(profile);
            });
            await RequiredAsync("runtime-changed-authorization-rejects-restore", async () =>
            {
                await StartFromActualHostAsync(store.Manager);
                Require((await store.Manager.SetBackgroundTaskResumeOnStartupAsync(Id, TaskId, true, lifetime)).Succeeded,
                    "The actual host could not save explicit resume intent.");
                var key = await store.Manager.ReadSecretAsync(Id, SecretId, lifetime);
                var binding = await ReadBindingAsync(store.Manager);
                host.Detach();
                await store.Manager.ShutdownAsync(lifetime);
                await NoWorkerAsync(installed);
                var path = Path.Combine(profile, "settings.json");
                var settings = JsonNode.Parse(await File.ReadAllTextAsync(path, lifetime))!.AsObject();
                // This remains locally valid and future-only, but changes the non-UI authorization fingerprint.
                settings["morning_run_at"] = DateTime.Now.AddHours(3).ToString("HH:mm", System.Globalization.CultureInfo.InvariantCulture);
                await File.WriteAllTextAsync(path, settings.ToJsonString(), lifetime);
                await RestartRestoreAsync(store);
                await WaitFaultedAsync(store.Manager);
                Require(await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) == key &&
                    JsonNode.DeepEquals(await ReadBindingAsync(store.Manager), binding),
                    "Changed-authorization restore minted replacement authorization or secret material.");
                AssertNoDeliveries(profile);
            });
            await RequiredAsync("runtime-missing-secret-rejects-restore", async () =>
            {
                await StartFromActualHostAsync(store.Manager);
                Require((await store.Manager.SetBackgroundTaskResumeOnStartupAsync(Id, TaskId, true, lifetime)).Succeeded,
                    "Explicit resume setup failed before the missing-secret fixture.");
                host.Detach();
                await store.Manager.ShutdownAsync(lifetime);
                await NoWorkerAsync(installed);
                var filename = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(SecretId))) + ".bin";
                var keyPath = Path.Combine(store.Root, "secrets", Id, filename);
                Require(File.Exists(keyPath), "The missing-secret fixture has no existing owned encrypted key file.");
                File.Delete(keyPath); // Only this run's generated, encrypted test key; never a business credential.
                await RestartRestoreAsync(store);
                await WaitFaultedAsync(store.Manager);
                Require(await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) is null && !File.Exists(keyPath),
                    "Restore generated a replacement key after the existing restore key was removed.");
                AssertNoDeliveries(profile);
            });
            await RequiredAsync("runtime-profile-switch-revokes-resume", async () =>
            {
                await StartFromActualHostAsync(store.Manager);
                Require((await store.Manager.SetBackgroundTaskResumeOnStartupAsync(Id, TaskId, true, lifetime)).Succeeded,
                    "Explicit resume setup failed before native profile switch.");
                await OpenSettingsAsync(store.Manager);
                await host.SelectTabAsync(_settings, "settings-tabs", 0);
                await host.EditAsync(Find<TextBox>(_settings, "profile-folder"), second);
                await host.InvokeAsync(_settings, "adopt-profile", operationId: "bind-profile");
                AssertStopped(store.Manager, resume: false);
                Require(await ReadBindingAsync(store.Manager) is null, "Native profile switch retained the previous profile's restore binding.");
                Require(await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) is null,
                    "Native profile switch retained the previous profile's generated restore key.");
                AssertNoDeliveries(profile);
                AssertNoDeliveries(second);
                // RequiresPluginStart=false exposes generic Start, but the real callback must still reject auto-off business settings.
                Require((await store.Manager.StartBackgroundTaskAsync(Id, TaskId, lifetime)).Succeeded,
                    "The host could not invoke the declared callback for the negative authorization fixture.");
                await WaitFaultedAsync(store.Manager);
                Require(await ReadBindingAsync(store.Manager) is null && await store.Manager.ReadSecretAsync(Id, SecretId, lifetime) is null,
                    "A generic host start bypassed saved-auto authorization or minted a key before validation.");
                AssertNoDeliveries(second);
            });
            await RequiredAsync("runtime-disable-running-cleanup", async () =>
            {
                await host.InvokeAsync(_settings, "default-profile", operationId: "bind-profile");
                await StartFromActualHostAsync(store.Manager);
                host.Detach();
                var lifecycleFaults = BackgroundFaultCount(store.Manager);
                Require((await store.Manager.DisableAsync(Id, lifetime)).Succeeded, "Disabling a running managed task failed.");
                Require(BackgroundFaultCount(store.Manager) == lifecycleFaults, "Normal running Disable added a BackgroundTaskFailed diagnostic.");
                Require(store.Manager.GetBackgroundTaskSnapshots(Id).Count == 0 && store.Manager.Contributions.Views.Count == 0 &&
                    !store.Manager.HasPendingStops, "Disable retained runtime contributions or known pending work.");
                await NoWorkerAsync(installed);
                Require((await store.Manager.EnableAsync(Id, cancellationToken: lifetime)).Succeeded, "The explicitly disabled package could not be re-enabled.");
                AssertStopped(store.Manager, resume: false);
                // Queue real authorization and disable immediately. The exact instruction ordering is SDK-owned;
                // this probes the published lifecycle path without adding a production pause/test hook.
                lifecycleFaults = BackgroundFaultCount(store.Manager);
                Require((await store.Manager.StartBackgroundTaskAsync(Id, TaskId, lifetime)).Succeeded, "The lifecycle overlap Start was not accepted.");
                Require((await store.Manager.DisableAsync(Id, lifetime)).Succeeded, "Disable did not finish the queued-start lifecycle overlap.");
                Require(BackgroundFaultCount(store.Manager) == lifecycleFaults && !store.Manager.HasPendingStops,
                    "Queued-start Disable added a false background failure or retained known work.");
                await NoWorkerAsync(installed);
                Require((await store.Manager.EnableAsync(Id, cancellationToken: lifetime)).Succeeded, "The lifecycle overlap fixture could not be re-enabled.");
                AssertStopped(store.Manager, resume: false);
                AssertNoDeliveries(profile);
            });
        }
        catch (StoppedPhase) { }
        catch (Exception error) { report.Fail("runtime-phase", $"Unexpected {error.GetType().Name}; raw exception suppressed."); }
        finally
        {
            host.Detach();
            if (store is not null)
                await report.CheckAsync("runtime-dispose-cleanup", async () =>
                {
                    await store.DisposeAsync();
                    await NoWorkerAsync(Path.Combine(store.PluginsRoot, Id));
                });
            foreach (var id in ExpectedChecks) report.Skip(id, "A prerequisite in the actual runtime phase failed.");
        }
    }

    private async Task PrepareProfileAsync(string profile, bool automatic)
    {
        await using var client = await Upcam.Workbench.PluginWorkerClient.StartAsync(options.PackageDirectory, profile, true, lifetime);
        var initial = await client.SendAsync("initialize", cancellationToken: lifetime);
        var end = DateTime.Now.AddMinutes(-1);
        var window = new JsonObject
        {
            ["start"] = end.AddHours(-2).ToString("yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture),
            ["end"] = end.ToString("yyyy-MM-dd HH:mm", System.Globalization.CultureInfo.InvariantCulture)
        };
        await client.SendAsync("probe_project", window, lifetime);
        var saved = await client.SendAsync("save", new JsonObject { ["settings"] = initial["settings"]!.DeepClone(), ["verification_window"] = window.DeepClone() }, lifetime);
        await client.SendAsync("refresh_models", cancellationToken: lifetime);
        var settings = saved["settings"]!.DeepClone().AsObject();
        settings["auto_send_enabled"] = automatic;
        settings["morning_run_at"] = DateTime.Now.AddHours(2).ToString("HH:mm", System.Globalization.CultureInfo.InvariantCulture);
        settings["afternoon_run_at"] = settings["morning_run_at"]!.DeepClone();
        await client.SendAsync("save", new JsonObject { ["settings"] = settings, ["verification_window"] = window.DeepClone() }, lifetime);
        if (automatic)
        {
            var authorization = await client.SendAsync("plugin_auto_authorization", cancellationToken: lifetime);
            var fingerprint = authorization["authorization_fingerprint"]?.GetValue<string>();
            Require(authorization["authorized"]?.GetValue<bool>() == true && fingerprint is { Length: 64 } &&
                fingerprint.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f'),
                "The prepared synthetic fixture did not pass the local, value-free automatic authorization operation.");
            _initialFingerprint = fingerprint;
        }
    }

    private async Task VerifyAdmissionPolicyAsync()
    {
        var policy = new Upcam.Workbench.SchedulerAdmission();
        var captured = new TaskCompletionSource<long>(TaskCreationOptions.RunContinuationsAsynchronously);
        var resume = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var observer = Task.Run(async () =>
        {
            var old = policy.Capture();
            captured.SetResult(old);
            await resume.Task;
            return (OldRejected: Rejected(() => policy.Validate(old)), CaptureRejected: Rejected(() => policy.Capture()));
        });
        var previous = await captured.Task.WaitAsync(TimeSpan.FromSeconds(10), lifetime);
        try
        {
            using (policy.BeginMutation())
            {
                Require(Rejected(() => policy.BeginMutation()), "The actual admission policy allowed overlapping mutations.");
                resume.SetResult();
                var result = await observer.WaitAsync(TimeSpan.FromSeconds(10), lifetime);
                Require(result.OldRejected && result.CaptureRejected,
                    "A controlled callback crossed the active production mutation fence.");
            }
        }
        finally { resume.TrySetResult(); }
        Require(Rejected(() => policy.Validate(previous)), "Disposing a mutation revived an old captured authorization epoch.");
        var current = policy.Capture();
        policy.Validate(current);
        Require(current > previous, "The production admission generation did not advance after mutation.");
    }

    private static bool Rejected(Action action)
    {
        try { action(); return false; }
        catch (InvalidOperationException) { return true; }
    }

    private static int BackgroundFaultCount(PluginManager manager) => manager.Plugins.Sum(plugin =>
        plugin.Diagnostics.Count(diagnostic => diagnostic.Code == PluginDiagnosticCode.BackgroundTaskFailed));

    private async Task OpenSettingsAsync(PluginManager manager)
    {
        host.AssertUiThread();
        var view = manager.Contributions.Views.Where(v => v.PluginId == Id).Select(v => v.View).OfType<WinUiView>()
            .Single(v => v.Kind == WinUiViewKind.Settings);
        _settings = view.CreateView();
        _factoryCalls++;
        await host.AttachAsync(_settings);
        await host.WaitReadyAsync(_settings, synthetic: true);
    }

    private async Task StartFromActualHostAsync(PluginManager manager)
    {
        var previous = await ReadBindingAsync(manager);
        Require((await manager.StartBackgroundTaskAsync(Id, TaskId, lifetime)).Succeeded,
            "The actual generic host Start rejected the declared host-startable task.");
        await WaitRunningBoundAsync(manager);
        await host.WaitUntilAsync(async () => !JsonNode.DeepEquals(await ReadBindingAsync(manager), previous));
    }

    private Task WaitRunningBoundAsync(PluginManager manager) => host.WaitUntilAsync(async () =>
    {
        var task = Snapshot(manager);
        if (task.State != PluginBackgroundTaskState.Running || task.ErrorCode is not null || await ReadBindingAsync(manager) is null) return false;
        // SDK Running is set before the delegate starts. Read the plugin's normal local status command to
        // observe an actually completed authorization/check, without creating or attaching any view.
        var command = await manager.ExecuteCommandAsync(Id, "scheduler-status", lifetime);
        if (!command.Succeeded || JsonNode.Parse(command.Message) is not JsonObject status) return false;
        var operation = status["lastCheckOperation"]?.GetValue<string>();
        return status["state"]?.GetValue<string>() == "Running" && status["checkRevision"]?.GetValue<long>() > 0 &&
            status["lastCheckSucceeded"]?.GetValue<bool>() == true && (operation is "authorize-scheduler" or "scheduled-run");
    });

    private Task WaitFaultedAsync(PluginManager manager) => host.WaitUntilAsync(() =>
        Snapshot(manager).State == PluginBackgroundTaskState.Faulted && Snapshot(manager).ErrorCode == "BackgroundTaskFailed");

    private async Task RestartRestoreAsync(OwnedStore store)
    {
        await store.RestartManagerAsync();
        await store.Manager.DiscoverAsync(lifetime);
        var restored = await store.Manager.RestoreEnabledAsync(lifetime);
        Require(restored.Count == 1 && restored[0].Succeeded, "The actual manager did not restore the trusted runtime package.");
    }

    private Task<JsonObject?> ReadBindingAsync(PluginManager manager) => manager.GetSettings(Id).GetAsync<JsonObject?>(BindingKey, null, lifetime);

    private static PluginBackgroundTaskSnapshot Snapshot(PluginManager manager)
    {
        var snapshots = manager.GetBackgroundTaskSnapshots(Id);
        Require(snapshots.Count == 1 && snapshots[0].Id == TaskId, "The actual manager did not expose exactly the registered digest task.");
        return snapshots[0];
    }

    private static void AssertStopped(PluginManager manager, bool resume)
    {
        var task = Snapshot(manager);
        Require(task.State == PluginBackgroundTaskState.Stopped && !task.DesiredRunning && task.ResumeOnStartup == resume && !task.RequiresPluginStart,
            "The actual task snapshot does not have the expected stopped intent and explicit resume preference.");
    }

    private Task NoWorkerAsync(string package) => host.WaitUntilAsync(() => NativeProbes.OwnedWorkers(package).Count == 0 && !NativeProbes.SingletonExists());

    private static void AssertNoDeliveries(string profile)
    {
        Require(NativeProbes.SqlCount(Path.Combine(profile, "history.sqlite3"), "SELECT COUNT(*) FROM digest_runs;") == 0 &&
            NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM deliveries;") == 0 &&
            NativeProbes.SqlCount(Path.Combine(profile, "delivery_ledger.sqlite3"), "SELECT COUNT(*) FROM manual_attempts;") == 0,
            "A future-only runtime fixture unexpectedly created a business run or delivery claim.");
    }

    private static void AssertNoPlaintextKey(OwnedStore store, string generated)
    {
        var files = Directory.EnumerateFiles(store.Root, "*.json", SearchOption.TopDirectoryOnly)
            .Concat(Directory.Exists(Path.Combine(store.Root, "settings"))
                ? Directory.EnumerateFiles(Path.Combine(store.Root, "settings"), "*.json", SearchOption.AllDirectories) : []);
        foreach (var file in files)
            Require(!File.ReadAllText(file).Contains(generated, StringComparison.Ordinal), "Generated secret plaintext appeared in owned host JSON.");
    }

    private async Task RequiredAsync(string id, Func<Task> action)
    {
        if (!await report.CheckAsync(id, action)) throw new StoppedPhase();
    }
    private sealed class StoppedPhase : Exception { }
}
