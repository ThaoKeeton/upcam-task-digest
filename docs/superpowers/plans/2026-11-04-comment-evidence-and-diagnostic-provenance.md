# Comment evidence and diagnostic provenance

**Goal:** Accept evidence-backed file-only comments and unavailable link-preview metadata, and make each exported diagnostic identifiable and bound to its actual saved configuration and report window.

**Architecture:** Keep raw evidence local. Only substantive author text goes to the existing summary request. The worker owns diagnostic provenance and rejects exports after its source context changes; WinUI disables stale exports and uses a unique suggested filename.

**Constraints:** Synthetic fixtures only; preserve collection completeness, mapping, unknown-action, commit-source and delivery guards. No authentication values, business text, or real member mappings in the repository. Continue from main and validate the packaged native Windows application before merging.

## Tasks

- [ ] Add failing adapter/pipeline tests for file-only comments, exact unavailable preview metadata, conflicts and malformed variants; implement only the supported structures.
- [ ] Add failing diagnostic tests for version, UTC creation time, unique ID, explicit half-open window and unmapped current-owner identifier without names or text.
- [ ] Add failing worker tests for stale settings, credentials, window, failed refresh and missing diagnostic export. Implement a context fingerprint and enforce it at both export operations.
- [ ] Update native state, export filename/payload and visible version. Exercise stale-window invalidation, real file export and version compatibility in Windows UI acceptance.
- [ ] Update the repository editorial rule and operation instructions for file-only comments and diagnostic provenance.
- [ ] Run all Python tests, obtain an independent review, run Windows build and extracted ZIP/background interaction checks, inspect real UI evidence, then merge the tested tree into main and verify the remote result.

## Review focus

- Attachment IDs or preview metadata must never become model input.
- Attachment-only activity must not become an empty collection or inferred progress.
- Failed diagnosis must not leave the previous successful export available.
- Saved scope, mappings, Teambition identity or time-window changes must not export an unrelated cached result.
- The diagnostic version/window must be explicit; absent scope must remain unknown, without inventing a date.
