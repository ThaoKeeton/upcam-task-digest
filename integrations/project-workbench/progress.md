# Workbench plugin progress

Plan: `docs/superpowers/plans/2026-11-06-workbench-plugin.md`
Baseline: UPCAM main `a61a20a0df6d98eff7abfec56496eb0a9052747c`, standalone 0.4.3.
Reference: platform feature head `6565180b17130f6d494978291df4138de3fdb0b4`.

- SDK inspection complete; 35 unmodified source files verified against Git blob hashes.
- Ruling: independent Python, process-transport and native-page tasks may proceed in parallel with disjoint ownership, matching the user's parallel preparation request. Root owns reference/packaging/CI and integration review.
- Ruling: no new service/scheduler activity during SDK initialization. Without a post-activation hook, first view/command activates the backend; this is an explicit platform gap, not a fabricated lifecycle signal.
- Ruling: existing standalone profile adoption binds the same directory and ledger, never copies an independently schedulable profile. Existing OS startup/tray preferences remain unchanged for rollback.

Task 1: pending. Task 2: pending. Task 3: pending. Task 4: reference complete, remaining pending.

- Task 1 first implementation: 34 focused tests passed (2 Windows skips); full suite 917 passed (11 skips). Root full-suite run overlapped a deliberate new failing test; implementer reran after the fix and confirmed green. Native runs remain pending.
- Scoped review round 1 found credential URL-component filtering, owned-file hardlink rejection and precommit settings serialization budget gaps. Backend implementer addressing these with regression tests.
- Root fixed the CI path filter for all source-linked standalone inputs after observing the new regression test fail. Packaging tests also cover device names and nested duplicate SDK assemblies.
- Integration ruling: preserve actionable errors with an exact fixed-literal catalog at the C# boundary; arbitrary worker error text and stderr remain suppressed. Rich diagnosis remains a successful structured result, not a generic transport failure.

## First integrated candidate

- Local commit `f28aa5a3d82df21a2437b14b69123e4fc0085e7a`; identical remote source tree `9dd41af274a97af9cea5bf1914b495e9650f585b`, remote commit `83f191c2aee755a93ba8eedd8bdc8b776c2ec1a8`, draft PR 24.
- Root verification: 931 Python tests passed, 11 platform skips, 15.543 seconds. Task 1 scoped review completed two fix rounds; all six original findings closed. Reference exact commit/tree/branch and all 35 source blobs verified; package tests 12 passed.
- First Windows plugin run `42250214359` and standalone regression `42250214421` stopped at two path assertions before native compilation: Python expands `RUNNER~1` to the same directory's long name. Windows ran 931 tests, two failures, seven skips. Legacy Tk packaging/smoke succeeded. No WinUI/plugin runtime success is claimed from this run.
- Backend tests now expect resolved canonical paths and add a Windows short-name alias regression. Transport and native binding are being aligned to the same safe Windows canonical identity; raw string comparison is insufficient.
- Whole-native review found detailed-export nested `saved:false` was ignored and idle 15-second scheduler checks rebuilt the entire editor tree. Native implementation and meaningful export/poll regression checks are being fixed. Profile adoption/default/ledger continuity and binding persistence failure checks are being added.
- Platform head advanced to `de5a316507c9cb74f72968d3580845d867807597`, adding three design documents only; all pinned runtime/SDK/Core/App source blobs are unchanged. Its proposed host 0.2 `IPluginRuntimeContext` services are explicitly not implemented yet. The API 1 first-page activation limitation remains real.

Task 1: implementation/review complete; Windows alias regression rerun pending. Task 2: first native execution pending. Task 3: scoped review fixes in progress. Task 4: draft CI running/fixing, no deploy or stable publication.

## Second candidate and follow-up

- Local commit `432fd049514332dc83b8d27e6e0e756979df3e78`; remote commit `a089037acb990561c1ab8ac905c6af12c0bbd6d3`, exact tree `9b0f35885ce6377fad253af610d13cd808a050f0`. Frozen native path/export/editor fixes were sent while additional harness checks were completed independently.
- Local 932 tests passed, 12 platform skips. Windows run `42252380375` passed all 932 Python tests, seven skips, then found a build-script defect: relative `--add-data` paths were interpreted relative to the `build` spec directory. Native compilation had not run. Script now anchors data/entry paths to the repository and compiles native variants before Python packaging. Packaging regression failed before the fix, then all 13 packaging tests passed.
- Native harness now has 46 required checks plus four optional real PNG captures, retaining all 39 original check IDs. Added export outcomes, actual idle poll/editor/feedback retention, second synthetic profile preparation, binding persistence rollback, adoption/default return and draft/history/ledger continuity. Only the real process client is source-linked for fixture preparation; plugin entry/session/pages remain dynamically loaded via the real manager.
- Whole-native round 1 accepted export and editor retention fixes, found the remaining background feedback overwrite; a three-line guard now limits foreground result revisions to foreground operations. Scoped round 2 and Windows execution are pending.
- Root full local verification after these changes: 933 tests passed, 12 platform skips, 15.614 seconds. A separate CI-only real complete-host UI smoke driver is being prepared without changing platform source or any system/security setting.

## Native execution and runtime SDK handoff

- Run `42254025721`: production/acceptance plugin and native harness compiled and packaged; Windows 933 tests passed, seven skips; all 25 real process tests passed, including cancellation, 8.3 aliases, real deadlines, mutex and host-death descendant cleanup. Native install/trust/activation/rollback/fresh factories/first backend activation passed. Native checks then caught a control-realization race (`window-start`, `settings-tabs`), not a complete runtime pass.
- Harness now waits for the current logical tab host and selected content to be realized with matching XamlRoot, measured bounds and stable visual membership. It captures range values before detach and emits only bounded control ID/type/layout inventories on failure. All 46 required cases remain.
- CI-only complete-host UIA driver added, using Windows PowerShell 5.1 STA and a BOM; source review found and closed a rejected report-path write, now guarded by successful validation. No existing normal host root is read/modified, no service actions run, no global settings change. Actual complete-host UI execution still pending.
- Local verification: 934 tests passed, 12 platform skips, 17.595 seconds. This API 1 checkpoint is retained before runtime migration.
- Parent supplied implemented/tested host 0.2 runtime SDK at `d2005d469aef6a385c134b6914fb6dfa9cff20b2`, tree `46e072e9a97afcfaef89f1683b841e521801fb94`, CI `42255867411` Linux and Windows success. API 1/assembly 1.0.0.0 remain stable. Root is aligning native scheduler ownership/restoration with actual IPluginRuntimeContext; the old first-page scheduler is not the final design now that services exist. Platform/DingTalk repositories remain unmodified.

## Host 0.2 runtime candidate

- API 1 checkpoint: local `6135776888d8f9a5af4aa96430f40c277d885e05`, remote `b6ca581afc4296812e6c0665cfde4e513ecfdeb3`, exact tree `e81cc08ea5fc4513e9d81cdabbe5ac1648052f48`. Run `42259031136` compiled both packages and complete host, passed Windows 934 Python tests and all 25 process cases; 45 of 46 required native cases passed, plus four actual PNG captures. One visible-window assertion and complete-host trust UI selection still need diagnosis. The screenshots had transparent backing; the harness now renders an opaque backing before capture rather than changing image pixels.
- Standalone regression run `42259031149` passed Tk and actual WinUI packaging/startup checks. The standalone remains 0.4.3, unchanged by plugin-only runtime work.
- Replaced immutable reference with all 42 actual platform files from `d2005d469aef6a385c134b6914fb6dfa9cff20b2`; exact commit/tree/blob verification passed. Plugin min-host is 0.2.0, with a real SDK-managed `digest-scheduler`, default-off startup restoration, generated host-secret HMAC binding and no first-page timer. Business API credentials stay in the existing profile DPAPI store.
- Independent runtime review identified one P1 authorization snapshot gap and three P2 concurrency/lifecycle flaws. Added plugin-only atomic `plugin_run_due_authorized`, immutable settings/credential snapshots and deterministic stale-proof/mid-run mutation tests; ordinary standalone `run_due` is unchanged. Focused backend 66 tests passed, three Windows skips; whole local suite 956 passed, 12 platform skips, 21.013 seconds. Scoped re-review and new Windows execution are pending.
- Native mutation admission, bounded start handling and normal stop cancellation are being completed with actual-manager runtime coverage. The harness now retains prior checks and adds host-managed startup/restore/secret/profile-revocation tests. Complete-host driver emits bounded selector metadata and a genuine failure capture when available; it never logs page text or treats a failed trust action as passed.
- Runtime source fixes frozen: remove caller-owned readiness waiters; bound local start preflight; fence configuration mutations before stop and through a final stop/binding clear; validate each callback generation; treat published lifecycle cancellation cooperatively. Independent scoped re-review closed R1–R4 with no new concrete P1/P2. The backend atomic-operation review also passed. Neither review substitutes for the pending host 0.2 Windows build/runtime checks.
- First host 0.2 candidate: local `05eaf5f9bdaafd9e8ebc78b97c05cba2f7a64afa`, remote `588b3c2c7b86660d52767fe2d9e1cef026776138`, identical tree `a86bc8ef8312173d61100ff22ad60df4c392220c`. Run `42265753421` passed Linux regression/reference checks; Windows compilation found `StackPanel.IsEnabled` at `DigestPage.Settings.cs:320` (CS1061). No native runtime pass is claimed. A minimal supported-control enablement correction is being applied before rerun.
