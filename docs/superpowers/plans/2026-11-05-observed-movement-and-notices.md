# Observed movement and association notices

**Goal:** Support the newly evidenced task-position event and strictly bounded Teambition association display notices without claiming code provenance or inferring task completion.

**Evidence boundary:** The user-provided detailed snapshot remains private and is never copied into the repository, logs or fixtures. Tests use new fictional tasks, people and titles. No production API or credential access, sending, deployment, or member-name lookup is authorized for development.

**Implementation:** Keep event identity, task binding, half-open time windows and source completeness checks. Parse only the evidenced movement schema, verify ordered position changes against the current task, and report position updates as other changes. Distinguish exact commit/MR display notices from legacy title candidates; count activity IDs, leave provider/object/merge verification false, and keep notice bodies out of model inputs. Preserve unknown and malformed-event blocking. Investigate the independently reproduced status mapping/completion mismatch before changing that check.

- [ ] Add failing synthetic movement/schema/sequence/current-position tests; implement the bounded adapter and pipeline diagnostics.
- [ ] Add failing synthetic exact display-notice and negative tests; integrate deterministic notice counts with explicit unverified-code wording.
- [ ] Reproduce completion/category mismatch with a synthetic closed state; preserve positive resolved-state evidence and separate technical completion from the configured resolved category.
- [ ] Replay the private snapshot in memory, returning only aggregate verification results; never save or upload project records.
- [ ] Run full local tests and independent review; update rule documentation and application/rules versions.
- [ ] Build and interact with the Windows application, verify packaged artifacts and tested tree, then merge into main and synchronize the clean local checkout.
