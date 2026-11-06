namespace Workbench.Sdk;

/// <summary>Optional runtime services supplied by host 0.2.0 or later. API 1 is unchanged.</summary>
public static class WorkbenchRuntimeServices
{
    public const int Version = 1;
}

public interface IPluginRuntimeContext : IPluginContext
{
    IPluginBackgroundTasks BackgroundTasks { get; }
    IPluginSecretStore Secrets { get; }
    PluginStopReason StopReason { get; }
}

public enum PluginTaskStartReason { UserRequest, Restore }
public enum PluginStopReason { None, UserRequest, PluginDisabled, HostShutdown, InitializationFailed }
public enum PluginBackgroundTaskState { Stopped, Running, Stopping, Completed, Faulted }

/// <summary>A supervised delegate runs on the thread pool and must not access UI objects.</summary>
public sealed record PluginBackgroundTask(string Id, string Title, Func<IPluginBackgroundExecution, Task> RunAsync)
{
    /// <summary>Host Start/resume must defer to the plugin's own confirmation flow. A hint, not a security boundary.</summary>
    public bool RequiresPluginStart { get; init; }
}

public interface IPluginBackgroundExecution
{
    CancellationToken CancellationToken { get; }
    PluginTaskStartReason StartReason { get; }
    /// <summary>Updated before cancellation is requested; read again when handling cancellation.</summary>
    PluginStopReason StopReason { get; }
}

public sealed record PluginBackgroundTaskSnapshot(
    string Id, string Title, PluginBackgroundTaskState State,
    bool ResumeOnStartup, bool DesiredRunning,
    DateTimeOffset? StartedAt, DateTimeOffset? FinishedAt, string? ErrorCode)
{
    public bool RequiresPluginStart { get; init; }
}

public sealed record PluginTaskOperationResult(bool Succeeded, string Message);

public interface IPluginBackgroundTasks
{
    void Register(PluginBackgroundTask task);
    IReadOnlyList<PluginBackgroundTaskSnapshot> GetSnapshots();
    Task<PluginTaskOperationResult> StartAsync(string id, CancellationToken cancellationToken = default);
    Task<PluginTaskOperationResult> StopAsync(string id, CancellationToken cancellationToken = default);
    Task<PluginTaskOperationResult> SetResumeOnStartupAsync(string id, bool enabled, CancellationToken cancellationToken = default);
}

/// <summary>Metadata only. Values must never be put in settings, metadata, return messages or logs.</summary>
public sealed record PluginSecretDefinition(string Id, string Title, string? Description = null);

public interface IPluginSecretStore
{
    bool IsSupported { get; }
    void Register(PluginSecretDefinition definition);
    IReadOnlyList<PluginSecretDefinition> GetDefinitions();
    Task<string?> ReadAsync(string id, CancellationToken cancellationToken = default);
    Task WriteAsync(string id, string value, CancellationToken cancellationToken = default);
    Task DeleteAsync(string id, CancellationToken cancellationToken = default);
}

/// <summary>A safe error code with no provider exception or secret-bearing inner exception.</summary>
public sealed class PluginSecretStoreException(string errorCode) : Exception(errorCode)
{
    public string ErrorCode { get; } = errorCode;
}
