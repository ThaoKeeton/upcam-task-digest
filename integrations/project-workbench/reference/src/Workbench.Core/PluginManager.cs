using System.Collections.Concurrent;
using System.Reflection;
using Workbench.Sdk;

namespace Workbench.Core;

/// <summary>
/// Discovers local packages and runs explicitly trusted plugins in this process.
/// Load contexts isolate dependency resolution, not security or process failures.
/// </summary>
public sealed class PluginManager : IAsyncDisposable
{
    private readonly PluginManagerOptions _options;
    private readonly string _pluginsRoot;
    private readonly string _userRoot;
    private readonly string[] _discoveryRoots;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private Entry[] _entries = [];
    private readonly ConcurrentDictionary<string, JsonPluginSettings> _settings = new(StringComparer.Ordinal);
    private readonly Assembly[] _sharedAssemblies;
    private readonly string[] _sharedAssemblyNames;
    private readonly PluginStateStore _stateStore;
    private readonly PluginRuntimeState _runtimeState;
    private readonly ISecretProtector _secretProtector;
    private bool _disposed;
    private PluginContributions _contributions = new(
        Array.AsReadOnly(Array.Empty<PluginViewRegistration>()),
        Array.AsReadOnly(Array.Empty<PluginCommandRegistration>()),
        Array.AsReadOnly(Array.Empty<PluginSettingRegistration>()));

    public PluginManager(PluginManagerOptions options) : this(options, new WindowsSecretProtector()) { }

    internal PluginManager(PluginManagerOptions options, ISecretProtector secretProtector)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (options.StopTimeout <= TimeSpan.Zero || options.StopTimeout == Timeout.InfiniteTimeSpan)
            throw new ArgumentOutOfRangeException(nameof(options), "StopTimeout must be a positive finite duration.");
        _options = options;
        _secretProtector = secretProtector;
        _sharedAssemblies = options.SharedAssemblies.Distinct().Where(a => a != typeof(IWorkbenchPlugin).Assembly).ToArray();
        _sharedAssemblyNames = options.SharedAssemblyNames.Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        if (_sharedAssemblyNames.Any(n => string.IsNullOrWhiteSpace(n) || n.Contains('/') || n.Contains('\\') ||
            n.Contains(',') || n.EndsWith(".dll", StringComparison.OrdinalIgnoreCase)))
            throw new ArgumentException("Shared assembly names must be simple assembly names, without a path or extension.", nameof(options));
        _pluginsRoot = Path.GetFullPath(options.PluginsRoot);
        _userRoot = Path.GetFullPath(options.UserRoot);
        _stateStore = new(_userRoot);
        _runtimeState = new(_userRoot);
        _discoveryRoots = new[] { _pluginsRoot }.Concat(options.AdditionalDiscoveryRoots.Select(Path.GetFullPath))
            .Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
        FileSystemGuard.RejectLinks(_pluginsRoot);
        FileSystemGuard.RejectLinks(_userRoot);
        Directory.CreateDirectory(_pluginsRoot);
    }

    public IReadOnlyList<PluginSnapshot> Plugins => Array.AsReadOnly(Volatile.Read(ref _entries).Select(e => e.Snapshot).ToArray());
    public PluginContributions Contributions => Volatile.Read(ref _contributions);
    public IReadOnlyList<PluginDiagnostic> Diagnostics => _stateStore.Diagnostics;

    public bool HasPendingStops => Plugins.Any(p => p.HasPendingStop);

    public IReadOnlyList<PluginBackgroundTaskSnapshot> GetBackgroundTaskSnapshots(string pluginId)
        => FindEntry(pluginId)?.Context is { IsCommitted: true } context ? context.BackgroundTasks.GetSnapshots() : [];
    public Task<PluginTaskOperationResult> StartBackgroundTaskAsync(string pluginId, string id, CancellationToken cancellationToken = default)
        => ActiveContext(pluginId)?.BackgroundTasks.StartFromHostAsync(id, cancellationToken) ?? Task.FromResult(TaskUnavailable());
    public Task<PluginTaskOperationResult> StopBackgroundTaskAsync(string pluginId, string id, CancellationToken cancellationToken = default)
        => ActiveContext(pluginId)?.BackgroundTasks.StopAsync(id, cancellationToken) ?? Task.FromResult(TaskUnavailable());
    public Task<PluginTaskOperationResult> SetBackgroundTaskResumeOnStartupAsync(string pluginId, string id, bool enabled, CancellationToken cancellationToken = default)
        => ActiveContext(pluginId)?.BackgroundTasks.SetResumeOnStartupAsync(id, enabled, cancellationToken) ?? Task.FromResult(TaskUnavailable());
    public IReadOnlyList<PluginSecretDefinition> GetSecretDefinitions(string pluginId)
        => FindEntry(pluginId)?.Context is { IsCommitted: true } context ? context.Secrets.GetDefinitions() : [];
    public bool IsSecretStorageSupported(string pluginId) => ActiveContext(pluginId)?.Secrets.IsSupported == true;
    public Task<string?> ReadSecretAsync(string pluginId, string id, CancellationToken cancellationToken = default)
        => SecretStore(pluginId).ReadAsync(id, cancellationToken);
    public Task WriteSecretAsync(string pluginId, string id, string value, CancellationToken cancellationToken = default)
        => SecretStore(pluginId).WriteAsync(id, value, cancellationToken);
    public Task DeleteSecretAsync(string pluginId, string id, CancellationToken cancellationToken = default)
        => SecretStore(pluginId).DeleteAsync(id, cancellationToken);

    private PluginRegistrationContext? ActiveContext(string pluginId)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        var entry = FindEntry(pluginId);
        return entry?.Snapshot is { State: PluginState.Enabled, IsTrusted: true } ? entry.Context : null;
    }
    private PluginSecretStore SecretStore(string pluginId) => ActiveContext(pluginId)?.Secrets
        ?? throw new PluginSecretStoreException("SecretStoreUnavailable");
    private static PluginTaskOperationResult TaskUnavailable() => new(false, "The plugin is not enabled and trusted.");

    /// <summary>After discovery, restores only unchanged, trusted packages with saved enabled intent.</summary>
    public async Task<IReadOnlyList<PluginOperationResult>> RestoreEnabledAsync(CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            List<PluginOperationResult> results = [];
            foreach (var entry in Volatile.Read(ref _entries))
            {
                var status = entry.Snapshot;
                if (status.DesiredEnabled && status.IsTrusted && status.State is not PluginState.Enabled and not PluginState.Invalid and not PluginState.Stopping)
                    results.Add(await EnableCoreAsync(entry.Package.Manifest!.Id, confirmTrust: false, cancellationToken, restoreTasks: true));
            }
            return Array.AsReadOnly(results.ToArray());
        }
        finally { _gate.Release(); }
    }

    /// <summary>Stops every live instance without changing the saved enabled preference.</summary>
    public async Task ShutdownAsync(CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            foreach (var entry in Volatile.Read(ref _entries))
                await StopRuntimeAsync(entry, PluginStopReason.HostShutdown, cancellationToken);
            if (HasPendingStops) throw new PluginShutdownException();
        }
        finally { _gate.Release(); }
    }

    /// <summary>Reads and hashes packages without loading assemblies or executing plugin code.</summary>
    public async Task<IReadOnlyList<PluginSnapshot>> DiscoverAsync(CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            await _stateStore.LoadAsync(cancellationToken);
            List<Entry> entries = [];
            var previous = Volatile.Read(ref _entries);
            foreach (var root in _discoveryRoots)
            {
                FileSystemGuard.RejectLinks(root);
                if (!Directory.Exists(root))
                    continue;
                foreach (var path in Directory.EnumerateDirectories(root).Order(StringComparer.Ordinal))
                {
                    var package = await PackageValidator.ValidateAsync(path, _options.HostVersion, cancellationToken);
                    var existing = previous.SingleOrDefault(e => e.Package.Path == package.Path);
                    if (existing is not null && existing.Package.Fingerprint == package.Fingerprint && package.IsValid && existing.Snapshot.State != PluginState.Invalid)
                        entries.Add(existing);
                    else if (existing is not null && existing.Snapshot.IsLoaded)
                    {
                        MarkPackageChanged(existing);
                        entries.Add(existing);
                    }
                    else
                    {
                        var added = new Entry(package);
                        ApplySavedState(added);
                        entries.Add(added);
                    }
                }
            }
            foreach (var missing in previous.Where(e => e.Snapshot.IsLoaded && entries.All(n => n.Package.Path != e.Package.Path)))
            {
                MarkPackageChanged(missing);
                entries.Add(missing);
            }
            foreach (var group in entries.Where(e => e.Package.Manifest is not null).GroupBy(e => e.Package.Manifest!.Id, StringComparer.Ordinal))
            {
                if (group.Count() < 2)
                    continue;
                foreach (var entry in group)
                {
                    entry.Update(s => s with { State = PluginState.Invalid });
                    entry.AddDiagnostic(new(PluginDiagnosticCode.DuplicatePluginId, $"More than one package declares '{group.Key}'."));
                }
            }
            foreach (var changed in entries.Where(e => e.Package.Manifest is not null
                && e.Snapshot.Diagnostics.Any(d => d.Code == PluginDiagnosticCode.PackageChanged)))
                await ClearTaskIntentAsync(changed, removeConsent: true, cancellationToken);
            Volatile.Write(ref _entries, entries.ToArray());
            PublishContributions();
            return Plugins;
        }
        finally { _gate.Release(); }
    }

    /// <summary>Copies a new local package into PluginsRoot without executing or approving it. Call DiscoverAsync afterward.</summary>
    public async Task<PluginOperationResult> InstallAsync(string packageDirectory, CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        string? staging = null;
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            var package = await PackageValidator.ValidateAsync(packageDirectory, _options.HostVersion, cancellationToken);
            if (!package.IsValid)
                return new(false, string.Join(" ", package.Diagnostics.Select(d => d.Message)));
            var id = package.Manifest!.Id;
            if (Volatile.Read(ref _entries).Any(e => e.Package.Manifest?.Id == id && e.Snapshot.IsLoaded))
                return new(false, $"Plugin '{id}' has already been loaded. Exit and restart before changing or replacing its package.");
            // Inspect disk as well as the current catalog: installation never needs a
            // prior discovery call, and bundled packages reserve their IDs too.
            foreach (var root in _discoveryRoots)
            {
                FileSystemGuard.RejectLinks(root);
                if (!Directory.Exists(root))
                    continue;
                foreach (var path in Directory.EnumerateDirectories(root))
                {
                    var existing = await PackageValidator.ValidateAsync(path, _options.HostVersion, cancellationToken);
                    if (existing.Manifest?.Id == id)
                        return new(false, $"Plugin '{id}' already exists. Updates/removal require exit and restart; packages are never replaced.");
                }
            }
            var target = FileSystemGuard.ContainedPath(_pluginsRoot, id);
            if (Directory.Exists(target) || File.Exists(target))
                return new(false, $"Installation target '{target}' already exists; replacement is not supported.");
            staging = FileSystemGuard.ContainedPath(_pluginsRoot, $".install-{Guid.NewGuid():N}");
            Directory.CreateDirectory(staging);
            foreach (var file in package.Files)
            {
                cancellationToken.ThrowIfCancellationRequested();
                FileSystemGuard.EnsureRegularFile(file.FullPath);
                var destination = FileSystemGuard.ContainedPath(staging, file.RelativePath);
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                await using var source = new FileStream(file.FullPath, FileMode.Open, FileAccess.Read, FileShare.Read, 81920, useAsync: true);
                await using var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, useAsync: true);
                await source.CopyToAsync(output, cancellationToken);
            }
            // Validate the copy again before making it visible, including its hash.
            var copied = await PackageValidator.ValidateAsync(staging, _options.HostVersion, cancellationToken);
            if (!copied.IsValid || copied.Fingerprint != package.Fingerprint)
                return new(false, "The package changed while it was being installed; the staged copy was rejected.");
            cancellationToken.ThrowIfCancellationRequested();
            Directory.Move(staging, target);
            staging = null;
            return new(true, $"Installed '{id}'. It is disabled and requires explicit trust before activation.");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException)
        {
            return new(false, $"Installation failed: {error.Message}");
        }
        finally
        {
            try
            {
                if (staging is not null && Directory.Exists(staging))
                    Directory.Delete(staging, recursive: true);
            }
            finally { _gate.Release(); }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
            return;
        await _gate.WaitAsync();
        try
        {
            if (_disposed)
                return;
            foreach (var entry in Volatile.Read(ref _entries))
                await StopRuntimeAsync(entry, PluginStopReason.HostShutdown, CancellationToken.None);
            if (HasPendingStops) throw new PluginShutdownException();
            _disposed = true;
        }
        finally { _gate.Release(); }
    }

    /// <summary>confirmTrust must represent an explicit user approval of the discovered package.</summary>
    public async Task<PluginOperationResult> EnableAsync(string pluginId, bool confirmTrust = false, CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            return await EnableCoreAsync(pluginId, confirmTrust, cancellationToken);
        }
        finally { _gate.Release(); }
    }

    private async Task<PluginOperationResult> EnableCoreAsync(string pluginId, bool confirmTrust, CancellationToken cancellationToken, bool restoreTasks = false)
    {
        var entry = FindEntry(pluginId);
        if (entry is null || entry.Snapshot.State == PluginState.Invalid)
            return new(false, $"Plugin '{pluginId}' is absent, invalid or ambiguous.");
        if (entry.Snapshot.State == PluginState.Enabled)
            return new(true, $"Plugin '{pluginId}' is already enabled.");
        // Rediscovery may invalidate catalog metadata, but it must not execute
        // StopAsync or surrender ownership of an existing runtime. Only an
        // explicit stop can make these references safe to replace.
        if (entry.Context is not null || entry.Instance is not null)
            return new(false, $"Plugin '{pluginId}' still owns a live runtime. Disable it or shut down before enabling another instance.");
        var current = await PackageValidator.ValidateAsync(entry.Package.Path, _options.HostVersion, cancellationToken);
        if (!current.IsValid || current.Fingerprint != entry.Package.Fingerprint)
        {
            MarkPackageChanged(entry);
            await ClearTaskIntentAsync(entry, removeConsent: true, cancellationToken);
            await PersistStateAsync(entry, enabled: false, fingerprint: null, cancellationToken);
            return new(false, $"Package '{pluginId}' changed after discovery. Rediscover before trusting it; a loaded package requires exit and restart.");
        }
        if (!entry.Snapshot.IsTrusted && !confirmTrust)
        {
            entry.AddDiagnostic(new(PluginDiagnosticCode.Untrusted, $"Plugin '{pluginId}' requires explicit user trust before running in this process."));
            return new(false, "Explicit trust confirmation is required before enabling this package.");
        }
        if (!entry.Snapshot.IsTrusted)
        {
            if (!await PersistStateAsync(entry, enabled: false, entry.Package.Fingerprint, cancellationToken))
                return new(false, "Could not save package approval; the plugin was not activated.");
            entry.Update(s => s with { IsTrusted = true });
        }
        var initializing = false;
        try
        {
            var dataDirectory = FileSystemGuard.ContainedPath(_userRoot, Path.Combine("data", pluginId));
            Directory.CreateDirectory(dataDirectory);
            FileSystemGuard.RejectLinks(dataDirectory);
            var savedTasks = await _runtimeState.LoadPluginAsync(pluginId, entry.Package.Fingerprint!, entry.AddDiagnostic, cancellationToken);
            var background = new PluginBackgroundTasks(savedTasks,
                (id, intent, token) => _runtimeState.SaveAsync(pluginId, entry.Package.Fingerprint!, id, intent, entry.AddDiagnostic, token),
                entry.AddDiagnostic, _options.StopTimeout);
            var context = new PluginRegistrationContext(pluginId, dataDirectory, GetSettings(pluginId), background,
                new PluginSecretStore(_userRoot, pluginId, _secretProtector, entry.AddDiagnostic));
            entry.FailedInitialization = false;
            entry.Context = context;
            entry.Update(s => s with { HasRuntime = true });
            entry.LoadContext ??= new PluginLoadContext(entry.Package, _sharedAssemblies, _sharedAssemblyNames);
            entry.Assembly ??= entry.LoadContext.LoadFromAssemblyPath(FileSystemGuard.ContainedPath(entry.Package.Path, entry.Package.Manifest!.EntryAssembly));
            entry.Update(s => s with { IsLoaded = true });
            var type = entry.Assembly.GetType(entry.Package.Manifest!.EntryType, throwOnError: true)!;
            if (type.IsAbstract || !typeof(IWorkbenchPlugin).IsAssignableFrom(type))
                throw new InvalidOperationException("The entry type must be a concrete IWorkbenchPlugin with a public parameterless constructor.");
            entry.Instance = (IWorkbenchPlugin?)Activator.CreateInstance(type)
                ?? throw new InvalidOperationException("The plugin constructor returned no instance.");
            initializing = true;
            await entry.Instance.InitializeAsync(context, cancellationToken);
            cancellationToken.ThrowIfCancellationRequested();
            if (!await background.PrepareCommitAsync(cancellationToken))
                throw new IOException("Could not clear background task resume intent.");
            if (!await PersistStateAsync(entry, enabled: true, entry.Package.Fingerprint, cancellationToken))
                throw new IOException("Could not save enabled state; activation was rolled back.");
            context.Commit();
            entry.Update(s => s with { State = PluginState.Enabled, DesiredEnabled = true });
            PublishContributions();
            if (restoreTasks) await background.RestoreAsync(CancellationToken.None);
            return new(true, $"Enabled '{pluginId}'.");
        }
        catch (Exception error)
        {
            entry.FailedInitialization = true;
            await StopRuntimeAsync(entry, PluginStopReason.InitializationFailed, CancellationToken.None);
            entry.Update(s => s with { State = s.HasPendingStop ? PluginState.Stopping : PluginState.Failed, DesiredEnabled = false });
            await PersistStateAsync(entry, enabled: false, entry.Package.Fingerprint, CancellationToken.None);
            var code = initializing ? PluginDiagnosticCode.InitializationFailed : PluginDiagnosticCode.LoadFailed;
            var message = $"{code} ({FailureCategory(error)}).";
            entry.AddDiagnostic(new(code, message));
            if (error is OperationCanceledException && cancellationToken.IsCancellationRequested)
                throw;
            return new(false, message);
        }
    }

    /// <summary>Revokes contributions, cancels Lifetime and stops the instance; assemblies stay loaded until process exit.</summary>
    public async Task<PluginOperationResult> DisableAsync(string pluginId, CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        await _gate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            if (!PackageValidator.IsSafePluginId(pluginId))
                return new(false, "A safe namespaced plugin ID is required; invalid packages cannot change saved preferences.");
            var matching = Volatile.Read(ref _entries).Where(e => e.Package.Manifest?.Id == pluginId).ToArray();
            if (matching.Length == 0)
                return new(false, $"Plugin '{pluginId}' was not discovered.");
            var success = true;
            foreach (var entry in matching)
            {
                var invalid = entry.Snapshot.State == PluginState.Invalid;
                entry.Update(s => s with { DesiredEnabled = false });
                var stopping = StopRuntimeAsync(entry, PluginStopReason.PluginDisabled, cancellationToken);
                success &= await PersistStateAsync(entry, enabled: false,
                    entry.Snapshot.IsTrusted ? entry.Package.Fingerprint : null, CancellationToken.None);
                success &= await ClearTaskIntentAsync(entry, removeConsent: false, CancellationToken.None);
                success &= (await stopping).Succeeded;
                if (invalid && !entry.Snapshot.HasPendingStop)
                    entry.Update(s => s with { State = PluginState.Invalid });
            }
            return new(success, success ? $"Disabled '{pluginId}'. Assemblies remain loaded until exit." : $"Plugin '{pluginId}' has stop or persistence errors; inspect its state before retrying.");
        }
        finally { _gate.Release(); }
    }

    public async Task<PluginCommandResult> ExecuteCommandAsync(string pluginId, string commandId, CancellationToken cancellationToken = default)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        Entry? entry;
        PluginCommand? command;
        CancellationTokenSource linked;
        await _gate.WaitAsync(cancellationToken);
        try
        {
            ObjectDisposedException.ThrowIf(_disposed, this);
            entry = FindEntry(pluginId);
            command = entry?.Snapshot.State == PluginState.Enabled ? entry.Context?.FindCommand(commandId) : null;
            if (command is null)
                return new(false, $"Command '{pluginId}/{commandId}' is unavailable; its plugin may be disabled.");
            linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, entry!.Context!.Lifetime);
        }
        finally { _gate.Release(); }
        using (linked)
        {
            try
            {
                linked.Token.ThrowIfCancellationRequested();
                var result = await command.ExecuteAsync(linked.Token)
                    ?? throw new InvalidOperationException("The command returned no result.");
                linked.Token.ThrowIfCancellationRequested();
                return result;
            }
            catch (Exception error)
            {
                var message = error is OperationCanceledException ? "Command was canceled." : $"CommandFailed ({FailureCategory(error)}).";
                entry!.AddDiagnostic(new(PluginDiagnosticCode.CommandFailed, $"Command '{pluginId}/{commandId}' failed: {message}"));
                return new(false, message);
            }
        }
    }

    private async Task<PluginOperationResult> StopRuntimeAsync(Entry entry, PluginStopReason reason, CancellationToken cancellationToken)
    {
        // A later explicit disable can revoke business intent during a pending shutdown.
        // Keep the original Stop Task, while the readable reason is upgraded before cancellation cleanup ends.
        entry.Context?.Close(reason);
        if (entry.StopTask is null)
        {
            if (entry.Context is null && entry.Instance is null) return new(true, "Stopped.");
            entry.PreserveInvalid = entry.Snapshot.State == PluginState.Invalid;
            entry.Update(s => s with { State = PluginState.Stopping, HasPendingStop = true });
            PublishContributions();
            // Plugin delegates and cancellation callbacks may have a synchronous prefix.
            // Keep it off the caller/dispatcher, and retain this single Task until it really ends.
            var stop = entry.StopTask = Task.Run(() => StopOwnedWorkAsync(entry));
            _ = CleanupAfterStopAsync(entry, stop);
        }
        try { await entry.StopTask.WaitAsync(_options.StopTimeout, cancellationToken); }
        catch (TimeoutException)
        {
            entry.AddDiagnostic(new(PluginDiagnosticCode.StopTimedOut, "PluginStopPending: known work is still stopping; ownership is retained."));
            return new(false, "PluginStopPending: known work is still stopping.");
        }
        var result = entry.StopTask.Result;
        CompleteStop(entry);
        return result;
    }

    private static async Task<PluginOperationResult> StopOwnedWorkAsync(Entry entry)
    {
        async Task<bool> ObserveAsync(Func<Task> action, string code)
        {
            try { await action(); return true; }
            catch (Exception)
            {
                entry.AddDiagnostic(new(PluginDiagnosticCode.StopFailed, code));
                return false;
            }
        }
        var context = entry.Context;
        var instance = entry.Instance;
        var cancellation = ObserveAsync(() => context?.CancelLifetimeAsync() ?? Task.CompletedTask, "LifetimeCancellationFailed");
        var background = context?.BackgroundTasks.StopAllAsync() ?? Task.FromResult(true);
        var plugin = ObserveAsync(() => Task.Run(async () =>
        {
            if (instance is not null) await instance.StopAsync(CancellationToken.None);
        }), "PluginStopFailed");
        var results = await Task.WhenAll(cancellation, background, plugin);
        return results.All(success => success) ? new(true, "Stopped.") : new(false, "StopFailed: known work completed with errors.");
    }

    private async Task CleanupAfterStopAsync(Entry entry, Task<PluginOperationResult> stop)
    {
        await stop;
        await _gate.WaitAsync();
        try
        {
            if (ReferenceEquals(entry.StopTask, stop)) CompleteStop(entry);
        }
        finally { _gate.Release(); }
    }

    // Lifecycle gate is held, including by the asynchronous late-completion observer.
    private void CompleteStop(Entry entry)
    {
        if (entry.StopTask is not { IsCompletedSuccessfully: true } stop) return;
        var failed = !stop.Result.Succeeded || entry.FailedInitialization;
        entry.Context?.Dispose();
        entry.Context = null;
        entry.Instance = null;
        entry.StopTask = null;
        entry.Update(s => s with
        {
            HasRuntime = false,
            HasPendingStop = false,
            State = entry.PreserveInvalid ? PluginState.Invalid : failed ? PluginState.Failed : PluginState.Disabled
        });
        PublishContributions();
    }

    private static string FailureCategory(Exception error)
    {
        // Do not invoke virtual Exception.Message/GetBaseException/ToString on plugin-defined types.
        while (error is TargetInvocationException or TypeInitializationException && error.InnerException is { } inner)
            error = inner;
        return error switch
        {
            OperationCanceledException => "Canceled",
            FileNotFoundException or FileLoadException => "DependencyLoadFailed",
            IOException or UnauthorizedAccessException => "StorageFailed",
            _ => "PluginException"
        };
    }

    private Entry? FindEntry(string pluginId)
    {
        var entries = Volatile.Read(ref _entries).Where(e => e.Package.Manifest?.Id == pluginId).ToArray();
        return entries.Length == 1 ? entries[0] : null;
    }

    private void PublishContributions()
    {
        var contributions = Volatile.Read(ref _entries).Where(e => e.Snapshot.State == PluginState.Enabled && e.Context is not null)
            .Select(e => e.Context!.Snapshot()).ToArray();
        Volatile.Write(ref _contributions, new(
            Array.AsReadOnly(contributions.SelectMany(c => c.Views).ToArray()),
            Array.AsReadOnly(contributions.SelectMany(c => c.Commands).ToArray()),
            Array.AsReadOnly(contributions.SelectMany(c => c.Settings).ToArray())));
    }

    private static void MarkPackageChanged(Entry entry)
    {
        entry.Update(s => s with { State = s.HasPendingStop ? PluginState.Stopping : PluginState.Failed, IsTrusted = false, DesiredEnabled = false });
        entry.AddDiagnostic(new(PluginDiagnosticCode.PackageChanged,
            "Package contents changed or disappeared. Trust was revoked; loaded assemblies require exit and restart before replacement."));
    }

    private void ApplySavedState(Entry entry)
    {
        if (!entry.Package.IsValid)
            return;
        var saved = _stateStore.Get(entry.Package.Manifest!.Id);
        if (saved?.TrustedFingerprint is { } fingerprint)
        {
            if (string.Equals(fingerprint, entry.Package.Fingerprint, StringComparison.OrdinalIgnoreCase))
                entry.Update(s => s with { IsTrusted = true, DesiredEnabled = saved.Enabled });
            else
                entry.AddDiagnostic(new(PluginDiagnosticCode.PackageChanged, "Saved trust does not match this package's contents. Review and explicitly trust the changed package before enabling it."));
        }
        else if (saved?.Enabled == true)
            entry.AddDiagnostic(new(PluginDiagnosticCode.Untrusted, "Saved enabled preference was ignored because this package has no explicit trust approval."));
    }

    private async Task<bool> PersistStateAsync(Entry entry, bool enabled, string? fingerprint, CancellationToken cancellationToken)
    {
        try
        {
            await _stateStore.SaveAsync(entry.Package.Manifest!.Id, enabled, fingerprint, cancellationToken);
            return true;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            entry.AddDiagnostic(new(PluginDiagnosticCode.StatePersistenceFailed, "Could not persist plugin preference (StatePersistenceFailed)."));
            return false;
        }
    }

    private async Task<bool> ClearTaskIntentAsync(Entry entry, bool removeConsent, CancellationToken cancellationToken)
    {
        try
        {
            await _runtimeState.ClearAsync(entry.Package.Manifest!.Id, removeConsent, entry.AddDiagnostic, cancellationToken);
            return true;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            entry.AddDiagnostic(new(PluginDiagnosticCode.RuntimeStatePersistenceFailed, "Background task intent could not be cleared."));
            return false;
        }
    }

    public IPluginSettings GetSettings(string pluginId)
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (!PackageValidator.IsSafePluginId(pluginId))
            throw new ArgumentException("A safe namespaced plugin ID is required.", nameof(pluginId));
        var entry = Volatile.Read(ref _entries).SingleOrDefault(e => e.Package.Manifest?.Id == pluginId)
            ?? throw new KeyNotFoundException($"Plugin '{pluginId}' was not discovered.");
        if (entry.Snapshot.State == PluginState.Invalid)
            throw new InvalidOperationException($"Plugin '{pluginId}' has an invalid or ambiguous package.");
        return _settings.GetOrAdd(pluginId, id => new JsonPluginSettings(Path.Combine(_userRoot, "settings", id),
            diagnostic =>
            {
                foreach (var current in Volatile.Read(ref _entries).Where(e => e.Package.Manifest?.Id == id))
                    current.AddDiagnostic(diagnostic);
            }));
    }

    private sealed class Entry(ValidatedPackage package)
    {
        private readonly object _sync = new();
        private PluginSnapshot _snapshot = new(package.Path, package.Manifest,
            package.IsValid ? PluginState.Disabled : PluginState.Invalid,
            false, false, false, package.Diagnostics);
        public ValidatedPackage Package { get; } = package;
        public PluginLoadContext? LoadContext { get; set; }
        public Assembly? Assembly { get; set; }
        public IWorkbenchPlugin? Instance { get; set; }
        public PluginRegistrationContext? Context { get; set; }
        public Task<PluginOperationResult>? StopTask { get; set; }
        public bool FailedInitialization { get; set; }
        public bool PreserveInvalid { get; set; }
        public PluginSnapshot Snapshot { get { lock (_sync) return _snapshot; } }
        public void Update(Func<PluginSnapshot, PluginSnapshot> update) { lock (_sync) _snapshot = update(_snapshot); }
        public void AddDiagnostic(PluginDiagnostic diagnostic) => Update(s => s with
        {
            Diagnostics = Array.AsReadOnly(s.Diagnostics.Append(diagnostic).ToArray())
        });
    }
}
