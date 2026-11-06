# Batched Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute the independent module tasks with synthetic tests before integration.

**Goal:** Replace the desktop's 700-project-task ceiling with bounded batches and safely recoverable encrypted progress.

**Architecture:** Preserve the low-level default Client and add an opt-in coordinator with shared session budgets and source reconciliation. Store only completed task reads in an owned DPAPI SQLite checkpoint. All desktop source entry points use the same factory and collection lease.

**Tech Stack:** Existing Python stdlib, SQLite, Windows DPAPI, C# WinUI 3 and GitHub Windows CI.

**Spec:** `docs/superpowers/specs/2026-11-06-batched-collection-design.md`

## Global constraints

- Per-batch size defaults to 300, range 1–700, using the existing settings key.
- Session limits: 10,000 tasks; 50,000 attempts; 1,000,000 returned rows; 512 MiB response bytes; 128 MiB retained JSON; two hours; three closing checks.
- No production credentials, requests, messages or target-machine changes in development.
- Never equate completed pagination or cached candidate equality with an atomic/historical snapshot.

## Review focus

- Initial and closing project pagination must cover more than 5,000 tasks independently of the batch activity-page ceiling.
- Budget reservations, including details, duplicates and retries, survive crashes and resumes.
- Checkpoint ciphertext must bind to context, session and task identity; corruption and oversized inputs stop before reuse.
- Initial setup, connection tests, diagnostics and actual workflow must all use batch collection; clearing progress never clears delivery state.
- Candidate maps, completed records and unfinished activity reads all count against retained JSON; this is not a process-RSS guarantee.

## Task 1: Encrypted checkpoint

- [ ] Add failing codec, binding, transaction, lease, corruption, expiry and quota tests.
- [ ] Implement `DpapiCollectionCheckpoint.start/load_task/save_task/save_counters/prune/clear/close` and exact context/session-bound envelopes.
- [ ] Verify injected synthetic codecs locally and actual DPAPI on Windows CI.

## Task 2: Batch coordinator

- [ ] Add failing projects with 701, 1,201, 5,001 and 10,000 task candidates.
- [ ] Add shared low-level attempt/byte/row/time hooks, retaining default Client behavior.
- [ ] Implement full-task checkpoints, bounded retry of unfinished tasks, source reconciliation and strict closing completeness.
- [ ] Prove no partial result on interruption, changing source, pagination, record/byte/time/retention/session limits.

## Task 3: Desktop integration

- [ ] Add a common collection-client builder used by project setup, connection tests, diagnostics and workflow.
- [ ] Add clear-progress command protected by the worker job lock and checkpoint lease, without touching settings, credentials, history or ledgers.
- [ ] Relabel existing batch setting, translate fixed strings and test the actual native clear control.
- [ ] Increase diagnostic input limits to match the bounded collector while preserving explicit export truncation and output budgets.

## Task 4: Acceptance and delivery

- [ ] Run the complete synthetic suite without credential environment variables and obtain independent review.
- [ ] Run Windows DPAPI, ordinary startup/migration, two native UI rounds and complete extracted-package verification.
- [ ] Verify source tree and artifact, merge the tested tree to main, synchronize the worktree and rerun final checks.
