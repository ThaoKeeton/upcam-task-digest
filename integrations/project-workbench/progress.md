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
