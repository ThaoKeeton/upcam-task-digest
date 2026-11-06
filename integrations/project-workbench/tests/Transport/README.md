# Windows process transport acceptance

Run from the repository root in an isolated Windows user session with the .NET 10 SDK:

```powershell
dotnet run --project integrations/project-workbench/tests/Transport/UPCAM.Workbench.TransportTests.csproj --configuration Release -- --report transport-results.json
```

The project has no external test packages or host SDK dependencies. It links the
production `PluginWorkerClient.cs` and copies its own apphost into a disposable
plugin directory as `worker/UPCAMPluginWorker.exe`. Run the apphost, rather than
`dotnet UPCAM.Workbench.TransportTests.dll`, so child fixtures use the same executable.
All fixtures use temporary explicit profiles, fake service data, and local files;
they do not load credentials or call services. Run sequentially with other UPCAM
acceptance suites because the production singleton intentionally spans profiles.

The executable tests real pipes, UTF-8 framing, byte caps, correlation, handshake
binding, process deadlines, cancellation, serialized callers, singleton exclusion,
reparse rejection, shutdown, descendant cleanup on host death, and recovery of a
locally recorded uncertain send without replay. A known fixed setup message is
preserved while arbitrary and partially matching child errors remain private.
The Windows alias regression uses real `GetShortPathNameW`/`GetLongPathNameW`
results for an existing profile, a missing descendant profile and package paths;
it also rejects a handshake for a different directory. The acceptance volume
must expose an actual 8.3 alias (as the Windows runner's temporary path does).
The ordinary deadline test takes about 30 seconds and the collection test runs
both `diagnose` and `save` for 31 seconds each; the whole suite normally takes
a little under two minutes.

The worker fixture exercises the transport boundary. It does not replace the
separate bundled Python worker tests or actual SDK/Core and WinUI acceptance.
The collection test proves that `diagnose` and collection-capable `save` survive the ordinary 30 second limit;
the 125 minute ceiling is deliberately not waited out during CI.
