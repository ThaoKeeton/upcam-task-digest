# Historical event-window collection and evidence corrections

## Approved scope

Repair the diagnostic-confirmed collection and classification defects without
changing report windows, user status mappings, report formatting or delivery
authorization. Private diagnostics remain outside this repository; regressions
use independent fictional objects only.

## Design

- Enumerate every currently readable task in the selected project/archive scope
  with the approved project task API, omitting its optional `q` parameter.
- Exhaust task activity pages and filter `createTime` locally using the original
  half-open window. A task's latest `updated` is a consistency field, not a
  historical event selector. Re-enumerate tasks after collection and stop when
  visible task versions or membership changed. This is a consistency check,
  not an atomic or historical snapshot guarantee.
- Preserve task, request, page, record, byte and time budgets. Incomplete reads
  must stop before summary generation or delivery; do not label them zero updates.
- Validate window status/owner/location chains against later known transitions
  before comparing current task fields. Keep later context local and outside
  window counts, classifications and OpenAI input.
- A resolution requires entry from outside the user configured resolved set,
  with the window ending inside that set. Internal moves do not resolve again.
- Recognize only the evidenced custom-field attachment shapes, retain both raw
  events, union consistent file identities locally, and block conflicting identity
  metadata. File references and automation are not human communication progress.

## Work and verification

1. Add failing synthetic history collection regressions and verify the failure.
2. Replace the source contract and collector; migrate setup and error explanations.
3. Integrate the independently implemented status and attachment adapters.
4. Cover pages, mutable source, millisecond skew, historical state changes, empty
   and incomplete reads, archive choice and all resource limits.
5. Run the full suite without credential environment variables, obtain a separate
   review, and run the existing Windows ordinary-startup, migration, native UI,
   extracted-package and synthetic service checks.
6. Merge only the verified source tree to main and provide its build artifact.

## Source limits

The official API specifies no task-activity snapshot or global project activity
time query. Audit logs are a different stream. Current accessible project scope
cannot prove coverage of deleted/inaccessible/moved-away tasks or past membership.
The user's archive option remains explicit and unchanged. No real enterprise,
OpenAI or DingTalk service is contacted during development or verification.
