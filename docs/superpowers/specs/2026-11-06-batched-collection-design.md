# Bounded, resumable project collection

The user has authorized repair of the large-project collection ceiling. Preserve
the v0.4.2 event-time window, source validation and delivery behavior. Development
and Windows acceptance use synthetic transports and credentials only.

## Approaches considered

1. Raise one global task/request/time ceiling. This still loses all progress on
   interruption and does not control the size of each working batch.
2. Divide the project into bounded batches, retain complete task reads in an
   encrypted local checkpoint, and revalidate source membership/versions before
   reusing progress. This is the implementation choice for this repair.
3. Incremental server events or a historical snapshot. The approved API does not
   provide an equivalent historical task-activity stream or snapshot token; do
   not invent one or replace task activity with enterprise audit logs.

## Contract

- The saved `max_tasks_per_run` (default 300, 1–700) becomes a batch size, not a
  project-size ceiling. Existing settings and profiles remain readable.
- The batch-enabled desktop path supports at most 10,000 currently readable
  project tasks. Each batch keeps existing request/time/record/byte limits. Shared
  session bounds are 50,000 attempted requests, 1,000,000 returned rows, 512 MiB
  response bytes, 128 MiB retained JSON and two hours including checkpoint age.
  These are safety bounds, not a promise of unbounded history.
- Retained JSON is a serialization budget, not a process memory limit. The
  checkpoint limits the encrypted database and rollback journal individually
  to 160 MiB; a transaction may temporarily need approximately 320 MiB on disk.
- Read every project and activity page. Never stop at 700, truncate a task,
  assume activity ordering, or use task.updated to select the report window.
- Completed task records only may be checkpointed. An interrupted activity stream
  is read again from its first page; opaque page tokens are not resumed.
- Bind progress to tenant, project, archive choice, exact frozen report window,
  collection policy and the SHA-256 identity of the Teambition credential pair.
  Store no API keys, JWTs, HTTP headers, webhook or signing credentials.
- Checkpoint rows and metadata use current-user Windows DPAPI encryption in an
  owned SQLite database. No plaintext disk fallback. Tests inject a synthetic
  codec or memory store. Clear completed/stale/incompatible progress safely;
  clearing this cache must not change profiles, credentials, history or ledgers.
- On restart and after each pass, fully reread the project task list. Reuse a
  complete record only when its candidate object still matches. Refresh changed
  or added tasks, remove obsolete task cache entries, and require the closing
  task list to match the completed records. At most three reconciliation passes;
  continuing change blocks delivery instead of looping indefinitely.
- The API does not guarantee that every activity mutates task.updated or that
  cursors form a snapshot. These are observable consistency checks only, bounded
  within the same collection session, and cannot recover deleted/inaccessible or
  moved-away historical tasks. `snapshot_guaranteed` and absolute
  `historical_event_coverage` remain false.
- Incomplete or corrupt progress, resource exhaustion, ambiguous versions or
  incomplete pages cannot become zero updates or reach OpenAI/DingTalk. No send
  state or automatic retry is stored in the collection checkpoint.

## Interface and evidence

Use the existing schedule page for the batch-size setting and an explicit
operation to clear unfinished collection progress. Explain project/session bounds
and that shortening the report window does not reduce the base scan. Keep controls
visible at supported small window sizes and translate new fixed interface text.

Quantify 701+, 1,201 and multi-page synthetic projects, interrupted/resumed reads,
scope/credential changes, corruption, source reconciliation, resource exhaustion,
and zero model/sender calls on incomplete input. Run the full suite and the Windows
DPAPI, ordinary-startup/migration, native UI and extracted-package workflow before
merging the verified source tree to main.
