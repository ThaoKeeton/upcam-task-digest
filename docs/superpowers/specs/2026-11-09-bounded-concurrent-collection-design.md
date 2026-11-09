# Bounded concurrent task collection

The user requested restoration of fast candidate selection and asked whether task
activities can be requested concurrently. This change overlaps independent task
reads; candidate selection is a separate change. Existing pipeline evidence,
archive choice, report windows, budgets and delivery authorization stay intact.

## Approach

Use at most four task workers, with one shared request-admission gate. A task's
detail and opaque activity-page cursor chain stay sequential. The owner thread
continues to own the SQLite checkpoint, all shared session accounting, completion
and progress. Workers synchronously marshal accounting/partial-size updates back
to that owner instead of accessing SQLite across threads. Output follows candidate
order, regardless of completion order.

Raising request limits alone does not overlap latency. Independent worker rate
limits could multiply the API load, so the chosen design preserves one aggregate
minimum request interval of 0.25 seconds. HTTP 429 Retry-After/exponential backoff
pauses new admissions globally. At most four already admitted requests can remain
in flight. Four is a conservative configurable local ceiling, not a claimed API
quota. Cancellation stops queued admissions/waits; in-flight urllib reads retain
their existing bounded timeout (30 seconds). Native process cancellation remains
available. No real Teambition, OpenAI or DingTalk requests are used in tests.

## Safety and compatibility

Low-level Client defaults to one worker for existing explicit/offline clients;
production desktop construction opts into four. Requests, response reservations,
retries, partial retained JSON and completed tasks share existing session bounds.
An interrupted response keeps its conservative reservation. A task that changes
version is reconciled by the existing closing scan. No successful/partial source
is returned after any worker failure or cancellation. Checkpoints are written only
for complete tasks and are not turned into cross-run historical snapshots.

## Reproducible verification

All transports below are fictional; results are not production Teambition timings.
An eight-task source with one activity page per task needs 18 GETs, including two
project-list scans. At the unchanged aggregate 0.25-second admission interval:

- Simulated 50 ms transport latency: one worker 4.270 s; four workers 4.268 s.
- Simulated 1,000 ms latency: one worker 16.275 s; four workers 5.048 s (3.22x).
- Peak in-flight calls: 1 and 4 respectively in the high-latency case; shortest
  observed aggregate request-start gap was 0.2502 s.

The low-latency case is rate-bound, so concurrency is deliberately not faster.
The request count is unchanged; candidate reduction is an independent optimization.
The concurrent-only Linux suite passes 983 tests with 13 platform-dependent skips. Windows
DPAPI, native UI controls and extracted worker packaging still require Windows CI.
Persisted max_concurrent_tasks accepts integers 1–4 and defaults old profiles to 4.
Both native UIs expose this setting. One-worker fallback keeps all existing bounds.


## Review fixes

An independent review reproduced three boundary defects before publication.
Regression tests first reproduced each failure, then verified owner-atomic
write-ahead reservations with capacity waiting, cancellation after the closing
scan, and rate-state carryover between repeated Client runs. Actual SQLite with a
fictional reversible codec also verifies owner-only connection/encryption calls;
that is not a substitute for real Windows DPAPI. No review finding is deferred.
