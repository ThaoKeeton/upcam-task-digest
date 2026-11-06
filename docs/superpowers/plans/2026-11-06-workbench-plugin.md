# Native Workbench Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install UPCAM digest as native WinUI pages without losing standalone 0.4.3 rollback.

**Architecture:** A .NET 10 / host-version WinUI class library registers fresh native pages and owns one bounded stdio Python process. It shares the unchanged digest core and explicitly bound DPAPI profile/ledger; a singleton excludes simultaneous standalone scheduling.

**Tech Stack:** C#, WinUI 3, Windows App SDK 1.8.260921001, Python 3.12, PyInstaller, actual API 1 SDK/Core snapshot.

**Spec:** `docs/superpowers/specs/2026-11-06-workbench-plugin.md`

## Global Constraints

- Keep standalone APP_VERSION 0.4.3, existing worker entry and project behavior.
- Do not modify platform or DingTalk repositories; the initial API 1 reference is retained in history. Task 5 uses immutable, source-hash-verified runtime commit `d2005d469aef6a385c134b6914fb6dfa9cff20b2`.
- No real service calls, keys or sends; synthetic profiles only in development/CI.
- Manifest `com.upcam.task-digest`, plugin 0.1.0, protocol 1, API 1, .NET 10, Windows App SDK 1.8.260921001.
- No automatic authorization expansion, copied delivery ledger, nested window, copied host framework or secret in host settings/logs.

## Review Focus

- Enable persistence failure: no scheduler/service work before successful activation.
- Timeout during sending: terminate owned process; never replay; recover unknown delivery safely.
- Standalone/plugin race: only one singleton holder and one canonical history/ledger.
- Navigation/profile switch during edits: preserve drafts; serialize operations; stop old backend before switching.
- Tampered or relocated package: contained regular paths, handshake, no SDK/framework duplicate loading.

---

### Task 1: Headless plugin adapter and profile rules

**Files:** create `plugin_worker_entry.py`, `upcam_app/plugin_worker.py`, `tests/test_plugin_worker.py`.

**Interfaces:** the exact CLI/JSON handshake in the spec; business operations delegate to `WorkerService` with an explicit profile and no startup mutation.

- [x] Write and run failing tests for missing/relative/link profiles, required handshake, version mismatch, secret-safe responses, forbidden standalone operations and preference-preserving saves.
- [x] Implement bounded adapter and explicit synthetic service injection; retain unchanged existing entry.
- [x] Verify parity with standalone-core sample, no-dynamic OpenAI zero calls, schedule/history continuity, interrupted send recovery and no permission expansion.
- [x] Run focused tests and submit scoped review report.

### Task 2: Supervised process transport

**Files:** create `plugins/UPCAM.Workbench/PluginWorkerClient.cs`, focused Windows process tests under `integrations/project-workbench/tests`.

**Interfaces:** exact `StartAsync`, `SendAsync`, `DisposeAsync` in the spec; package relative worker `worker/UPCAMPluginWorker.exe`.

- [x] Write failing tests for bad handshake, UTF-8/size/correlation, occupied standalone singleton and path containment.
- [x] Implement serialized stdio, fixed safe errors, deadlines, cancel-to-kill, owned Job Object and singleton.
- [x] Verify timeout/cancel/no replay, descendant cleanup on host death, space paths, release after shutdown and send-result uncertainty recovery.
- [x] Submit scoped review report; do not change standalone transport. Runtime-operation deadline/error extensions require the new Windows rerun.

### Task 3: Native plugin and business pages

**Files:** create plugin project, manifest, `DigestPlugin.cs`, native page/state files. Source-link plain `DesktopState.cs` only when compatibility is proven.

**Interfaces:** real `IWorkbenchPlugin`/`WinUiView`, Task 2 client, Task 1 operations. Root owns reference/package/CI scripts.

- [ ] Pin tests for fresh factory controls, draft retention, profile binding and first-view activation.
- [ ] Implement native digest and settings views with operation serialization/cancellation and visible integration limits.
- [ ] Preserve all business features and safe secrets input; leave host-wide preferences to host, old prefs intact.
- [ ] Verify resize/local lists/keyboard, no nested windows, no hidden auto-send authorization; submit report.

### Task 4: Package, Windows acceptance and delivery

**Files:** immutable SDK/Core reference, `integrations/project-workbench` harness/scripts/docs, `.github/workflows/workbench-plugin.yml`, synthetic source/packaging regression tests.

- [ ] Verify source Git blobs against pinned manifest and no platform repository edits.
- [ ] Build worker/plugin; package only private plugin dependencies, preserve standalone build.
- [ ] Run actual SDK/Core install/trust/enable/view/command/disable/restart and native UI acceptance on Windows with synthetic profiles; collect report/screenshots.
- [ ] Run full Python suite; review all changes independently, fix material findings, rerun affected validation.
- [ ] Recheck platform branch/API drift and remote UPCAM main; publish only tested source via authorized PR and provide plugin artifact, rollback instructions and exact remaining platform gaps.

### Task 5: Implemented host 0.2 runtime alignment

**Spec:** `docs/superpowers/specs/2026-11-06-workbench-runtime.md` supersedes the earlier first-page scheduler design.

- [x] Verify/materialize actual SDK/Core at d2005d469aef6a385c134b6914fb6dfa9cff20b2 with exact source Git blobs; preserve the tested API 1 checkpoint in history.
- [x] Add local-only `plugin_auto_authorization` proof and atomic `plugin_run_due_authorized` snapshot operations, synthetic rejection/mutation/fingerprint/no-network tests and scoped review.
- [x] Register the real managed background task, implement explicit native start/stop/resume controls, generated SDK-secret HMAC binding and restore checks; remove independent scheduling from page activation. Scoped source re-review closed all R1–R4; Windows evidence remains the next item.
- [x] Preserve the canonical service-key DPAPI store and all standalone 0.4.3 behavior; no silent service-key migration/dual write.
- [ ] Extend real native install/lifecycle tests for managed background registration/restoration/revocation with no page required; rerun Windows package, actual complete-host smoke and review.
