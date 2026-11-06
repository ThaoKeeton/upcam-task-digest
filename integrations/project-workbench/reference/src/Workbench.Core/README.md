# Local trusted plugin runtime

SDK and Core target .NET 10 and have no WinUI dependencies. Plugins run as trusted
in-process code. An AssemblyLoadContext controls dependency identity; it is not a
security sandbox. Disabling removes contributions and cancels cooperative work,
but does not unload assemblies. Update/remove packages only after process exit,
then restart the application. Plugin settings are plain JSON, not a secret store.

Create a manager with a writable `PluginsRoot`, a persistent `UserRoot`, the host
version, and optional `AdditionalDiscoveryRoots` for bundled packages. Startup is
`DiscoverAsync` followed by `RestoreEnabledAsync`. Discovery never activates a
plugin. A new package defaults to disabled and untrusted; the first enable must
pass `confirmTrust: true` in response to explicit user approval. Saved approval
binds to a SHA-256 fingerprint of every package filename and file content. A
changed package needs fresh approval; loaded packages require a process restart.

Use `DisableAsync` for an explicit user disable. Use `ShutdownAsync` or
`DisposeAsync` when closing the app: both stop instances while preserving saved
enabled intent for the next startup. Per-package errors are in `Plugins`;
state-file errors are in `Diagnostics`. Execute commands through
`ExecuteCommandAsync`; contribution snapshots expose command descriptions,
never delegates. Refresh the host's navigation after lifecycle operations and
drop references to views belonging to disabled plugins.

Registrations are accepted only during `InitializeAsync`, staged until it
succeeds, and discarded on failure. Local IDs are scoped to one plugin; duplicate
view, command or setting IDs within that plugin fail activation. `PluginSetting`
describes a string setting for host UI; `IPluginSettings` can persist any
JSON-serializable value. Values live in
`UserRoot/settings/<plugin-id>/settings.json`. Enabled/trusted preferences live in
`UserRoot/plugin-state.json`. Writes replace an adjacent temporary file atomically.

If rediscovery invalidates a live package, its runtime remains owned for cleanup.
Scanning does not stop plugin code; re-enable is refused until explicit disable
or shutdown has stopped that instance.

Each immediate subdirectory of a discovery root is a package containing a
`plugin.json` like this (all fields required, unknown properties rejected):

```json
{
  "id": "com.example.notes",
  "name": "Notes",
  "version": "1.0.0",
  "apiVersion": 1,
  "minimumHostVersion": "0.1.0",
  "entryAssembly": "Workbench.Notes.dll",
  "entryType": "Workbench.Notes.NotesPlugin",
  "description": "Local demonstration notes",
  "capabilities": ["Native view", "Local settings", "Commands"]
}
```

IDs are lowercase namespaced identifiers, versions are three numeric parts, and
entry paths use forward slashes and end in `.dll`. Include the independently
built DLL, its `.deps.json`, and its private dependencies. Building a plugin with
`EnableDynamicLoading` enabled preserves dependencies. Packages may contain only
regular files and directories; links/reparse points, traversal, Windows device
names and case-colliding paths are rejected. Local installation stages and
revalidates a copy before exposing a new ID, and never replaces an existing ID.

`Workbench.Sdk` and .NET framework assemblies share the host identity. Framework
names come from the SDK's target-framework reference catalog embedded during the
Core build, so colocated host-private DLLs in a self-contained deployment do not
become shared framework assemblies. The WinUI
host must designate its additional SDK and WinUI/WinRT projection assemblies
through `SharedAssemblies` (actual host Assembly instances) or
`SharedAssemblyNames` (simple names resolved in the default load context). Private
managed dependencies resolve only inside the package through
AssemblyDependencyResolver; missing private dependencies do not fall back to an
unrelated host assembly.

The runtime assumes packages stay unchanged during activation/execution and a
single host owns the user store. It does not watch files continuously, lock out
other processes, or protect against trusted code modifying files concurrently.
Uncooperative plugin code can block or crash the host. Native UI creation,
thread dispatch and view disposal belong to the host/UI SDK.

## Plugin data files

`IPluginContext.DataDirectory` is created during activation under
`<UserRoot>/data/<plugin-id>` with the same containment/reparse-point checks as
other Core storage. Use it for local drafts and exports rather than deriving a
real user path; isolated hosts and native smoke supply their own UserRoot. This
is a storage convention, not a filesystem sandbox. The native host holds an
exclusive user-root file lease for the manager lifetime because the JSON stores
assume a single owning host process.
