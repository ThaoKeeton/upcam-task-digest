namespace Workbench.Sdk;

/// <summary>Version of the cross-platform plugin contract, independent of assembly versions.</summary>
public static class WorkbenchApi
{
    public const int Version = 1;
}

/// <summary>Trusted in-process code. Stopping a plugin does not unload its assembly.</summary>
public interface IWorkbenchPlugin
{
    Task InitializeAsync(IPluginContext context, CancellationToken cancellationToken);
    Task StopAsync(CancellationToken cancellationToken);
}

public interface IPluginContext
{
    string PluginId { get; }
    /// <summary>Existing per-plugin directory under the selected user root. A storage convention, not a sandbox.</summary>
    string DataDirectory { get; }
    CancellationToken Lifetime { get; }
    IPluginSettings Settings { get; }
    void RegisterView(IPluginView view);
    void RegisterCommand(PluginCommand command);
    void RegisterSetting(PluginSetting setting);
}

/// <summary>A UI SDK can extend this with a native view factory.</summary>
public interface IPluginView
{
    string Id { get; }
    string Title { get; }
}

public sealed record PluginCommand(
    string Id,
    string Title,
    Func<CancellationToken, Task<PluginCommandResult>> ExecuteAsync,
    string? Description = null);

public sealed record PluginCommandResult(bool Succeeded, string Message);

/// <summary>Metadata for a string setting displayed by the host.</summary>
public sealed record PluginSetting(string Id, string Title, string DefaultValue, string? Description = null);

/// <summary>Per-plugin JSON storage; neither a secret store nor a security sandbox.</summary>
public interface IPluginSettings
{
    Task<T> GetAsync<T>(string key, T defaultValue, CancellationToken cancellationToken = default);
    Task SetAsync<T>(string key, T value, CancellationToken cancellationToken = default);
}
