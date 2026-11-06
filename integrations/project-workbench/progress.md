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
