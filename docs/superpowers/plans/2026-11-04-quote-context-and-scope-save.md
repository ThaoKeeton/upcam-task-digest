# Structured quoted context and automatic scope save

**Goal:** Accept the exact bounded single-layer `_id` / `content` quoted-context envelope, preserve provenance, and remove the project-range confirmation checkbox.

**Architecture:** Python remains the sole validator and sender. Quoted text is tagged context separate from author text; reference IDs remain local. Native settings save represents the scope choice and triggers read-only proof when scope or Teambition authentication changes. Failed proof preserves edits, disables automatic execution and cannot call a model or sender.

**Constraints:** No real credentials or enterprise/OpenAI/DingTalk calls during development. No private input files, IDs, names or analysis counts in this repository. Preserve all pagination, source, mapping, status, provenance and deduplication checks.

## Tasks

- [ ] Add and observe failing synthetic quote envelope, role, privacy, unsupported-shape and source-gate regressions.
- [ ] Implement exact bounded quote parsing; preserve raw evidence and local reference identity; send only necessary role-labelled text context to the model.
- [ ] Validate model context fields, indexes and role; keep raw IDs out of requests and diagnostics.
- [ ] Add and observe failing save/proof/migration regressions; remove native confirmation control and automatically verify changed saved scope.
- [ ] Exercise automatic scope save and structured quotes using isolated synthetic native Windows UI; run the full suite and archive startup checks.
- [ ] Review changes, merge under the existing main-branch authorization, verify remote/local contents and provide the tested artifact and remaining protocol requirements.
