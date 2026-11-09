# Bounded Concurrent Collection Implementation Plan


**Goal:** Overlap independent task-read latency without multiplying rate or safety budgets.
**Architecture:** A shared request gate and a bounded task pool marshal stateful accounting/checkpoint work to the owner thread.
**Tech Stack:** Python standard library threading, queue, concurrent.futures and unittest.
**Spec:** docs/superpowers/specs/2026-11-09-bounded-concurrent-collection-design.md

## Global Constraints
- At most four in-flight task readers; default global interval remains 0.25 seconds.
- Task cursor pages remain serial and complete; output order remains deterministic.
- SQLite and shared session counters remain owner-thread-only.
- No real external-service requests or sending.

## Review Focus
- Cancellation while a task is awaiting a cursor page must never produce partial success.
- Global 429 cooldown must affect all new admissions, including retries.
- Multiple in-flight response reservations must never reset or exceed shared budgets.
- Reconciliation/resume must preserve candidate and activity identity rules.
- Out-of-order completions must not change final task/event order or leak source content into progress.

### Task 1: Add a global request gate and task pool
- [ ] Write failing tests for bounded overlap, rate/cooldown, cancellation, owner-only checkpoint, pagination and deterministic output.
- [ ] Run tests and verify the missing behavior fails.
- [ ] Implement collection_concurrency.py, add optional Client hooks and branch from _Coordinator._collect_tasks.
- [ ] Opt desktop Client construction into four workers; leave injected client signatures unchanged.
- [ ] Run focused and full offline suites, then synthetic latency benchmark.
- [ ] Review integration with separately restored candidate filtering and report exact verification limits.
