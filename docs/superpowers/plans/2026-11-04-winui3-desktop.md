# WinUI 3 Desktop Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Tk desktop window with a genuine WinUI 3 app while preserving all local data and Python automation behavior.

**Architecture:** A C# WinUI 3 process owns the desktop window, navigation, tray integration, current-user startup, and presentation. A packaged Python worker process exposes a narrow JSON-lines protocol and remains the sole owner of settings, DPAPI secrets, Teambition collection, OpenAI, validation, history, and DingTalk delivery.

**Tech Stack:** C# / .NET 8, WinUI 3, Windows App SDK 2.6.5, Python 3.12, PyInstaller, GitHub Actions Windows runner.

**Spec:** `docs/superpowers/designs/2026-11-04-winui3-desktop.md`

## Global Constraints

- Preserve the `%LOCALAPPDATA%\\UPCAM\\TaskDigest` data directory and existing storage schemas.
- Never expose credential values in worker responses, logs, stdout diagnostics, or history UI.
- Preserve fail-closed collection validation and durable no-retry delivery semantics.
- Use synthetic data only in automated UI verification; never contact live enterprise or delivery APIs from tests.
- Do not alter comment/commit protocol recognition without a redacted user-provided shape export.
- The UI must be actual WinUI 3 built with Windows App SDK; Tk styling is not evidence of migration.

## Review Focus

- Legacy profile compatibility: verify the worker reads the existing settings, DPAPI, cache, SQLite and ledger paths without a migration rewrite.
- Credential secrecy: verify initialize/history/model responses contain presence metadata only, not secret values.
- Scope verification: verify only a fresh matching probe or existing unchanged evidence can keep a project scope verified.
- Uncertain send and shutdown: retain the existing history/ledger behavior and never retry ambiguous deliveries.
- Small screens/high DPI and tray startup: verify the Window uses adaptive navigation and an accessible show/exit tray menu.
- Real Teambition comment/commit shapes: keep parser gates intact and report the pending redacted export as a separate limitation.

---

### Task 1: Add a headless worker service boundary

**Files:**
- Create: `upcam_app/worker_api.py`
- Create: `worker_entry.py`
- Test: `tests/test_worker_api.py`

**Interfaces:**
- `WorkerService.dispatch(request: dict) -> dict` handles a strict allowlist of commands.
- `worker_entry.py` reads and writes one bounded JSON object per line; each response carries the request ID and contains safe errors only.

- [ ] Write failing tests for initialize secrecy, no implicit model refresh, save and batch import, redacted command errors, and synthetic preview.
- [ ] Run the tests to verify the missing worker interface fails for the expected reasons.
- [ ] Implement only the tested commands using existing stores and workflow modules.
- [ ] Run the focused tests and the full Python test suite.

### Task 2: Create the real WinUI 3 desktop shell

**Files:**
- Create: `winui/UPCAMTaskDigest/UPCAMTaskDigest.csproj`
- Create: `winui/UPCAMTaskDigest/App.xaml`
- Create: `winui/UPCAMTaskDigest/App.xaml.cs`
- Create: `winui/UPCAMTaskDigest/MainWindow.xaml`
- Create: `winui/UPCAMTaskDigest/MainWindow.xaml.cs`
- Create: `winui/UPCAMTaskDigest/WorkerClient.cs`

**Interfaces:**
- `WorkerClient.SendAsync<T>(string operation, object? payload)` performs serialized JSON-lines IPC and rejects malformed responses.
- `MainWindow` hosts the Fluent side navigation and page frame; no business request is made during UI smoke mode.

- [ ] Add project files and a CI compile path pinned to the current stable Windows App SDK.
- [ ] Implement single-instance activation, close-to-tray, explicit Exit, and current-user Run entry while preserving existing names.
- [ ] Add a smoke-only command line mode with synthetic data and no real worker/API calls.
- [ ] Compile and launch on Windows CI before building out the form pages.

### Task 3: Add configuration, mappings, scheduling, diagnostics, and history pages

**Files:**
- Create: `winui/UPCAMTaskDigest/DesktopState.cs`
- Create: `winui/UPCAMTaskDigest/MainWindow.Pages.cs`
- Create: `winui/UPCAMTaskDigest/Ui.cs`
- Modify: `upcam_app/worker_api.py`
- Test: `tests/test_worker_api.py`

**Interfaces:**
- UI settings are round-tripped as the existing JSON settings document; the worker recomputes and owns verification evidence.
- Secret input is write-only; the UI receives only saved/missing flags.
- Project probing, source diagnosis, preview, manual run, scheduled run, and model refresh are separate explicit worker operations.

- [ ] Add Fluent, adaptive pages with controls covering existing settings and workflows.
- [ ] Add controls for member/status maps and safe JSON/TSV batch member imports.
- [ ] Implement real asynchronous worker calls and user-visible safe diagnostics.
- [ ] Add synthetic UI smoke assertions for every navigation route and important controls.
- [ ] Run Python tests and Windows build/UI smoke.

### Task 4: Build and package the WinUI app and Python worker in Windows CI

**Files:**
- Modify: `.github/workflows/windows-build.yml`
- Modify: `README.md`
- Modify: `WINDOWS_APP.md`
- Add: Windows packaging scripts and ZIP artifact manifest.

**Interfaces:**
- CI artifact contains a self-contained app folder ZIP with `UPCAMTaskDigest.exe`, Windows App SDK dependencies, and `worker/UPCAMWorker.exe` plus its runtime files.

- [ ] Build Python worker and .NET WinUI app on `windows-latest`.
- [ ] Launch smoke mode, traverse pages, collect a GUI screenshot, and check artifact contents.
- [ ] Run all Python tests and upload the ZIP artifact.
- [ ] Document upgrade/data preservation, Windows App SDK deployment, and unverified target-PC installation.

### Task 5: Review and integrate

- [ ] Review the full diff for duplicated validation or secret exposure.
- [ ] Confirm main has not advanced; rerun remote checks if it has.
- [ ] Open PR against `main`; merge only after the Windows build and UI smoke are green.
- [ ] Report remaining real-data and user-machine verification steps explicitly.
