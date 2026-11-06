# Native digest acceptance host

This executable uses the **unmodified platform API 1 SDK and Core** from
`../../reference/src`, pinned at `6565180b17130f6d494978291df4138de3fdb0b4`.
It does not reference the plugin project, implement a replacement plugin API, or
load the plugin assembly directly. `PluginManager.InstallAsync`, `DiscoverAsync`,
`EnableAsync`, its contribution catalog, and `ExecuteCommandAsync` own that path.
The actual `PluginWorkerClient.cs` is source linked only to prepare an independent
owned synthetic profile before plugin activation and compare canonical fixture
paths. No plugin entry point, session, page or replacement SDK interface is linked.

Build/publish on Windows x64 with .NET 10 and the Windows SDK/WinUI build tools:

```powershell
dotnet publish integrations/project-workbench/tests/Acceptance/Workbench.Digest.Acceptance.csproj `
  -c Release -r win-x64 --self-contained true -o C:\ci\native-acceptance-host
```

Run in a Windows desktop session, passing independently built unpacked packages
and an absolute JSON report path:

```powershell
& 'C:\ci\native-acceptance-host\Workbench.Digest.Acceptance.exe' `
  --package-directory 'C:\ci\packages\UPCAM acceptance package' `
  --production-package-directory 'C:\ci\packages\UPCAM production package' `
  --report 'C:\ci\evidence\native-acceptance.json'
if ($LASTEXITCODE -ne 0) { throw "Native acceptance failed; inspect the JSON report." }
```

The first package must have manifest ID `com.upcam.task-digest.acceptance` and be
compiled with `AcceptanceBuild=true`. Its worker is invoked in explicit synthetic
mode. The production package must use ID `com.upcam.task-digest` and the normal
build. Both must contain their private `worker/UPCAMPluginWorker.exe`. The
production option is syntactically optional so a partial invocation produces a
useful report; omitting it skips required production checks and returns failure.
Malformed options return failure and write a fixed diagnostic to a new temporary
JSON file, without echoing arguments or credential values.

Every run creates its own GUID temporary user roots whose names contain spaces.
It passes those exact roots to the real manager and holds its own exclusive
`host.lock` file. No installed plugin catalog, ordinary UPCAM profile, real
credentials, existing diagnostic file, registry startup setting, or user host
settings are read. Known service credential environment variable names are
removed from this process before any worker starts, without reading their
values. Input packages remain unchanged; mutation tests alter only the installed
copy beneath the owned temporary root. The harness neither kills an existing
standalone process nor changes firewall/security settings. An occupied UPCAM
singleton fails the precondition.

The required checks cover:

- Fresh install/discovery leaves the plugin unloaded, disabled and untrusted;
  an untrusted enable and disabled command cannot load code or start a worker.
- Explicit approval, persisted enabled intent and committed native contributions.
  A Windows sharing lock on the run's own state file forces enabled-state commit
  failure after initialization; staged contributions and backend activity must
  roll back. Releasing the lock allows normal activation.
- Shared `WinUiView` identities, fresh `FrameworkElement` factories on the actual
  WinUI thread, a single visible native top-level window, real attachment, native
  tabs, range focus and resize, and unsaved settings/range/tab draft retention
  across navigation and new factories.
- First-view activation of the packaged worker; registered local status and sample
  commands; actual native project probe, explicit business save, model refresh,
  diagnosis, manual synthetic action and history refresh. The acceptance fixture
  has transport-free client/model/DingTalk factories; no enterprise request or
  actual delivery is intended. Read-only SQLite aggregate queries validate
  confirmed history/manual ledger counts without reading report bodies.
- Detailed export keeps explicit native content consent. An existing owned JSON
  sentinel forces the real worker's nested `saved:false` result after diagnosis;
  the operation must show failure with permissions/space guidance, retain the
  diagnosis and leave the sentinel unchanged. A new filename must report saved
  success and create a nonempty file while retaining the diagnosis.
- An explicitly saved synthetic schedule with both run times two hours ahead
  produces a real successful no-op poll. After at least 16 seconds and an observed
  completed schedule revision, the same range editor must retain focus, selection,
  draft and enabled state, and the foreground operation result/revision must stay
  unchanged, with no new history/delivery rows. Normal native Save
  turns the fixture's automatic authorization off in `finally`.
- Native adoption of a second existing synthetic profile keeps each profile's
  unsaved draft, history and ledger identity separate. A sharing lock on the
  owned host binding file forces a real persistence failure: the target worker
  stops, saved binding stays unchanged, and reconnect restores the old draft.
  Retry, default return and re-adoption use actual controls and preserve both
  original profiles. No history/ledger copying or real data adoption occurs.
- Disable revokes all contribution kinds and commands, stops package-owned workers
  and releases the singleton. Re-enable uses the saved exact-package approval.
  Shutdown preserves enabled intent. A newly constructed real manager discovers
  and restores the same package, starts the worker only on its next view, and
  retains canonical history/ledger counts. Mutating the installed copy revokes
  trust and prevents restoration, including after another new manager.
- The separately installed production build opens only fresh local setup/status.
  Native controls must show no tenant/project, no saved external credentials and
  no automatic sending. History, deliveries and manual-attempt tables stay empty;
  local settings retain unconfigured defaults and no model cache is fetched.
  Host JSON settings may contain only the non-secret profile directory binding.

The JSON file is updated after each check and contains `passed`, `failed` or
`skipped`, a required flag, safe fixed details, elapsed times and evidence paths.
Native readiness requires the current logical tab host and its selected content
to be loaded, laid out and present in the visual tree; a ready status label alone
does not complete the wait. Selection reacquires a replaced host and waits for
the selected content, including nested account tabs. Missing-control/time-out
details include bounded AutomationId/type/count/layout-presence inventories,
without control text or business data. Draft values are captured before detach.
Unexpected exception messages/stacks, worker protocol lines, stderr, secrets and
report bodies are never included. Exit code `0` means all required checks passed;
`1` means a failure or an unverified required check; `124` is the finite process
deadline. There is a 12-minute cooperative budget and a 14-minute final watchdog.
The owned worker's Job Object provides crash cleanup, covered separately by the
transport tests. The harness never replays a failed send operation.

Optional screenshot checks write real WinUI `RenderTargetBitmap` PNGs beside the
report in `<report-name> screenshots`. A rendering/capture failure is recorded and
does not hide the other checks. The images require human visual review. Isolated
profiles remain in their explicitly reported temporary roots for local diagnosis;
they are synthetic or unconfigured, and the report does not attach their DPAPI
files or SQLite report bodies.

This is a dedicated native acceptance host, **not a full production Workbench UI
test**. Manager restart checks reconstruct the actual manager inside this native
process; they do not claim a second full host process launch or plugin assembly
unloading. Production smoke observes empty local controls, persisted authorization
and database counts; it does not instrument or prove the absence of transient
network requests. Backend no-network tests/injected factory evidence and transport
lifecycle/singleton/host-death tests are separate supporting evidence. No Windows
execution, successful compilation or screenshot capture may be claimed from this
source tree alone.
