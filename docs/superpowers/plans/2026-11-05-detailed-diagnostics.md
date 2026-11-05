# Optional detailed diagnostics

**Goal:** Let a user explicitly export project evidence for private debugging while excluding application credentials and preserving all report validation gates.

**Design:** Keep the default structural export. Add a separate native action that explains project content disclosure, chooses a local folder, then performs one read-only collection and creates a uniquely named, credential-cleaned snapshot from that same collection. Do not cache project plaintext or return it through the desktop protocol. Clarify window-membership counters without changing their meaning. Anchor the write to the checked directory and never overwrite an existing target.

**Constraints:** No production requests or real credentials in development. Real attachments remain private; synthetic fixtures only in the repository. No sending, model calls, operating-system startup changes or member-name lookup. Preserve configuration, history, DPAPI, source integrity and duplicate-delivery guards.

- [x] Add strict credential filtering, bounded detailed snapshots and synthetic tests.
- [x] Add explicit counter aliases, legacy compatibility and inconsistency rejection tests.
- [x] Add worker opt-in/path validation, same-snapshot file output, protected-state paths, safe failure handling and zero-network/model/delivery tests.
- [x] Add native disclosure/cancel/save flow, clear diagnostic counts, version metadata and Windows interaction checks.
- [ ] Review, run the complete suite and Windows packaging checks, inspect artifacts, then merge the tested tree into main and verify the remote result.
