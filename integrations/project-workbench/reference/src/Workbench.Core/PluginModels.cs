using System.Reflection;
using Workbench.Sdk;

namespace Workbench.Core;

public sealed class PluginManagerOptions
{
    public required string PluginsRoot { get; init; }
    public required string UserRoot { get; init; }
    public IReadOnlyCollection<string> AdditionalDiscoveryRoots { get; init; } = [];
    public Version HostVersion { get; init; } = new(0, 2, 0);
    public TimeSpan StopTimeout { get; init; } = TimeSpan.FromSeconds(5);
    public IReadOnlyCollection<Assembly> SharedAssemblies { get; init; } = [];
    public IReadOnlyCollection<string> SharedAssemblyNames { get; init; } = [];
}

public sealed record PluginManifest
{
    public required string Id { get; init; }
    public required string Name { get; init; }
    public required string Version { get; init; }
    public required int ApiVersion { get; init; }
    public required string MinimumHostVersion { get; init; }
    public required string EntryAssembly { get; init; }
    public required string EntryType { get; init; }
    public required string Description { get; init; }
    public required IReadOnlyList<string> Capabilities { get; init; }
}

public enum PluginState { Invalid, Disabled, Enabled, Failed, Stopping }

public enum PluginDiagnosticCode
{
    InvalidManifest, InvalidPackage, IncompatibleApi, IncompatibleHost, MissingAssembly,
    DuplicatePluginId, Untrusted, PackageChanged, LoadFailed, InitializationFailed,
    StopFailed, CommandFailed, SettingsCorrupt, StateCorrupt, StatePersistenceFailed, InstallationFailed,
    StopTimedOut, BackgroundTaskFailed, RuntimeStateCorrupt, RuntimeStatePersistenceFailed, SecretStorageFailed
}

public sealed record PluginDiagnostic(PluginDiagnosticCode Code, string Message);

public sealed record PluginSnapshot(
    string PackagePath,
    PluginManifest? Manifest,
    PluginState State,
    bool IsTrusted,
    bool DesiredEnabled,
    bool IsLoaded,
    IReadOnlyList<PluginDiagnostic> Diagnostics,
    bool HasRuntime = false,
    bool HasPendingStop = false);

public sealed record PluginViewRegistration(string PluginId, IPluginView View);
public sealed record PluginCommandRegistration(string PluginId, string Id, string Title, string? Description);
public sealed record PluginSettingRegistration(string PluginId, PluginSetting Setting);
public sealed record PluginContributions(
    IReadOnlyList<PluginViewRegistration> Views,
    IReadOnlyList<PluginCommandRegistration> Commands,
    IReadOnlyList<PluginSettingRegistration> Settings);

public sealed record PluginOperationResult(bool Succeeded, string Message);

/// <summary>Known plugin work is still alive. The host must retain its process and user-root lease.</summary>
public sealed class PluginShutdownException() : Exception("PluginStopPending: known plugin work is still stopping; retry shutdown without releasing ownership.");
