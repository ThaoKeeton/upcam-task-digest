using Workbench.Sdk;

namespace Workbench.Core;

/// <summary>One observed worker and one cooperative stop per registered task execution.</summary>
internal sealed class PluginBackgroundTasks(
    IReadOnlyDictionary<string, SavedTaskIntent> saved,
    Func<string, SavedTaskIntent, CancellationToken, Task> persist,
    Action<PluginDiagnostic> diagnostic,
    TimeSpan stopTimeout) : IPluginBackgroundTasks, IDisposable
{
    private readonly object _sync = new();
    private readonly SemaphoreSlim _operations = new(1, 1);
    private readonly Dictionary<string, Registration> _tasks = new(StringComparer.Ordinal);
    private bool _open = true;
    private bool _active;
    private volatile PluginStopReason _closeReason;

    public void Register(PluginBackgroundTask task)
    {
        ArgumentNullException.ThrowIfNull(task);
        PluginRegistrationContext.ValidateMetadata(task.Id, task.Title);
        ArgumentNullException.ThrowIfNull(task.RunAsync);
        lock (_sync)
        {
            if (!_open) throw new InvalidOperationException("Background tasks can only be registered during InitializeAsync.");
            if (!_tasks.TryAdd(task.Id, new(task, saved.GetValueOrDefault(task.Id) ?? new(false, false))))
                throw new InvalidOperationException("Duplicate background task ID.");
        }
    }

    public IReadOnlyList<PluginBackgroundTaskSnapshot> GetSnapshots()
    {
        lock (_sync) return Array.AsReadOnly(_tasks.Values.Select(t => new PluginBackgroundTaskSnapshot(
            t.Definition.Id, t.Definition.Title, t.State, t.Intent.ResumeOnStartup, t.Intent.DesiredRunning,
            t.StartedAt, t.FinishedAt, t.ErrorCode)
        { RequiresPluginStart = t.Definition.RequiresPluginStart }).ToArray());
    }

    internal void Commit() { lock (_sync) { _open = false; _active = true; } }

    internal async Task<bool> PrepareCommitAsync(CancellationToken cancellationToken)
    {
        await _operations.WaitAsync(cancellationToken);
        try
        {
            foreach (var task in _tasks.Values.Where(t => t.Definition.RequiresPluginStart
                && (t.Intent.ResumeOnStartup || t.Intent.DesiredRunning)))
                if (!await SaveAsync(task, new(false, false), cancellationToken)) return false;
            return true;
        }
        finally { _operations.Release(); }
    }
    internal void Close(PluginStopReason reason)
    {
        lock (_sync)
        {
            _open = false;
            _active = false;
            if (_closeReason == PluginStopReason.None || _closeReason == PluginStopReason.HostShutdown && reason != PluginStopReason.HostShutdown)
                _closeReason = reason;
            foreach (var task in _tasks.Values)
            {
                if (_closeReason != PluginStopReason.HostShutdown) task.Intent = task.Intent with { DesiredRunning = false };
                if (task.Work is { IsCompleted: false } || task.Stop is { IsCompleted: false })
                {
                    if (task.Execution!.StopReason == PluginStopReason.None
                        || task.Execution.StopReason == PluginStopReason.HostShutdown && _closeReason != PluginStopReason.HostShutdown)
                        task.Execution.SetStopReason(_closeReason);
                    task.State = PluginBackgroundTaskState.Stopping;
                }
            }
        }
    }

    public Task<PluginTaskOperationResult> StartAsync(string id, CancellationToken cancellationToken = default)
        => StartCoreAsync(id, PluginTaskStartReason.UserRequest, cancellationToken);

    internal Task<PluginTaskOperationResult> StartFromHostAsync(string id, CancellationToken cancellationToken)
        => StartCoreAsync(id, PluginTaskStartReason.UserRequest, cancellationToken, fromHost: true);

    private async Task<PluginTaskOperationResult> StartCoreAsync(string id, PluginTaskStartReason reason, CancellationToken cancellationToken, bool fromHost = false)
    {
        await _operations.WaitAsync(cancellationToken);
        try
        {
            Registration? task;
            SavedTaskIntent intent;
            lock (_sync)
            {
                if (!_active || !_tasks.TryGetValue(id, out task)) return Unavailable();
                if (task.Definition.RequiresPluginStart && (fromHost || reason == PluginTaskStartReason.Restore))
                    return PluginStartRequired();
                if (task.State == PluginBackgroundTaskState.Stopping) return new(false, "Background task is still stopping.");
                if (task.Work is { IsCompleted: false }) return new(true, "Background task is already running.");
                intent = task.Intent with { DesiredRunning = true };
            }
            if (!await SaveAsync(task, intent, cancellationToken)) return PersistenceFailed();
            lock (_sync)
            {
                // Plugin disable can revoke starts while the intent write was in progress.
                if (!_active) return Unavailable();
                task.Cancellation?.Dispose();
                task.Cancellation = new();
                task.Execution = new(task.Cancellation.Token, reason);
                task.Stop = null;
                task.State = PluginBackgroundTaskState.Running;
                task.ErrorCode = null;
                task.StartedAt = DateTimeOffset.UtcNow;
                task.FinishedAt = null;
                task.Work = Task.Run(() => RunObservedAsync(task));
            }
            return new(true, "Background task started.");
        }
        finally { _operations.Release(); }
    }

    private async Task RunObservedAsync(Registration task)
    {
        string? errorCode = null;
        try { await task.Definition.RunAsync(task.Execution!); }
        catch (OperationCanceledException) when (task.Execution!.CancellationToken.IsCancellationRequested) { }
        catch (Exception)
        {
            errorCode = "BackgroundTaskFailed";
            diagnostic(new(PluginDiagnosticCode.BackgroundTaskFailed, errorCode));
        }
        lock (_sync)
        {
            task.ErrorCode = errorCode;
            task.FinishedAt = DateTimeOffset.UtcNow;
            if (task.State != PluginBackgroundTaskState.Stopping)
                task.State = errorCode is null ? PluginBackgroundTaskState.Completed : PluginBackgroundTaskState.Faulted;
        }
    }

    public async Task<PluginTaskOperationResult> StopAsync(string id, CancellationToken cancellationToken = default)
    {
        Task stop;
        bool savedIntent;
        await _operations.WaitAsync(cancellationToken);
        try
        {
            Registration? task;
            lock (_sync)
            {
                if (!_active || !_tasks.TryGetValue(id, out task)) return Unavailable();
            }
            savedIntent = await SaveAsync(task, task.Intent with { DesiredRunning = false }, cancellationToken);
            lock (_sync) stop = BeginStop(task, PluginStopReason.UserRequest);
        }
        finally { _operations.Release(); }
        try { await stop.WaitAsync(stopTimeout, cancellationToken); }
        catch (TimeoutException) { return new(false, "Background task is still stopping."); }
        return savedIntent ? new(true, "Background task stopped.") : PersistenceFailed();
    }

    public async Task<PluginTaskOperationResult> SetResumeOnStartupAsync(string id, bool enabled, CancellationToken cancellationToken = default)
    {
        await _operations.WaitAsync(cancellationToken);
        try
        {
            Registration? task;
            lock (_sync)
            {
                if (!_active || !_tasks.TryGetValue(id, out task)) return Unavailable();
                if (enabled && task.Definition.RequiresPluginStart) return PluginStartRequired();
            }
            return await SaveAsync(task, task.Intent with { ResumeOnStartup = enabled }, cancellationToken)
                ? new(true, "Background task resume preference saved.") : PersistenceFailed();
        }
        finally { _operations.Release(); }
    }

    internal async Task RestoreAsync(CancellationToken cancellationToken)
    {
        string[] eligible;
        lock (_sync) eligible = _tasks.Values.Where(t => t.Intent is { ResumeOnStartup: true, DesiredRunning: true })
            .Select(t => t.Definition.Id).ToArray();
        foreach (var id in eligible) await StartCoreAsync(id, PluginTaskStartReason.Restore, cancellationToken);
    }

    internal async Task<bool> StopAllAsync()
    {
        var success = true;
        List<Task> stops = [];
        await _operations.WaitAsync();
        try
        {
            foreach (var task in _tasks.Values)
            {
                if (_closeReason != PluginStopReason.HostShutdown)
                    success &= await SaveAsync(task, task.Intent with { DesiredRunning = false }, CancellationToken.None);
                lock (_sync) stops.Add(BeginStop(task, _closeReason));
            }
        }
        finally { _operations.Release(); }
        await Task.WhenAll(stops);
        return success;
    }

    // Called with _sync held. Cancellation callbacks run asynchronously and remain part of ownership.
    private Task BeginStop(Registration task, PluginStopReason reason)
    {
        if (task.Stop is not null) return task.Stop;
        if (task.Work is null || task.Work.IsCompleted) return Task.CompletedTask;
        if (task.Execution!.StopReason == PluginStopReason.None) task.Execution.SetStopReason(reason);
        task.State = PluginBackgroundTaskState.Stopping;
        var cancel = task.Cancellation!.CancelAsync();
        task.Stop = FinishStopAsync(task, task.Work, cancel);
        return task.Stop;
    }

    private async Task FinishStopAsync(Registration task, Task work, Task cancel)
    {
        try { await Task.WhenAll(work, cancel); }
        catch (Exception)
        {
            lock (_sync) task.ErrorCode = "BackgroundTaskCancellationFailed";
            diagnostic(new(PluginDiagnosticCode.BackgroundTaskFailed, "BackgroundTaskCancellationFailed"));
        }
        lock (_sync)
        {
            task.State = task.ErrorCode is null ? PluginBackgroundTaskState.Stopped : PluginBackgroundTaskState.Faulted;
            task.FinishedAt = DateTimeOffset.UtcNow;
        }
    }

    private async Task<bool> SaveAsync(Registration task, SavedTaskIntent intent, CancellationToken cancellationToken)
    {
        try { await persist(task.Definition.Id, intent, cancellationToken); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            diagnostic(new(PluginDiagnosticCode.RuntimeStatePersistenceFailed, "Background task intent could not be saved."));
            return false;
        }
        lock (_sync) task.Intent = intent;
        return true;
    }

    public void Dispose()
    {
        lock (_sync)
        {
            _active = false;
            _open = false;
            foreach (var task in _tasks.Values) task.Cancellation?.Dispose();
        }
    }

    private static PluginTaskOperationResult Unavailable() => new(false, "Background task is unavailable until its plugin is enabled.");
    private static PluginTaskOperationResult PersistenceFailed() => new(false, "Background task intent could not be saved.");
    private static PluginTaskOperationResult PluginStartRequired() => new(false, "PluginStartRequired: use the plugin's confirmation flow; automatic resume is unavailable.");

    private sealed class Registration(PluginBackgroundTask definition, SavedTaskIntent intent)
    {
        public PluginBackgroundTask Definition { get; } = definition;
        public SavedTaskIntent Intent { get; set; } = intent;
        public PluginBackgroundTaskState State { get; set; } = PluginBackgroundTaskState.Stopped;
        public DateTimeOffset? StartedAt { get; set; }
        public DateTimeOffset? FinishedAt { get; set; }
        public string? ErrorCode { get; set; }
        public CancellationTokenSource? Cancellation { get; set; }
        public Execution? Execution { get; set; }
        public Task? Work { get; set; }
        public Task? Stop { get; set; }
    }

    private sealed class Execution(CancellationToken token, PluginTaskStartReason startReason) : IPluginBackgroundExecution
    {
        private int _stopReason;
        public CancellationToken CancellationToken { get; } = token;
        public PluginTaskStartReason StartReason { get; } = startReason;
        public PluginStopReason StopReason => (PluginStopReason)Volatile.Read(ref _stopReason);
        internal void SetStopReason(PluginStopReason reason) => Volatile.Write(ref _stopReason, (int)reason);
    }
}
