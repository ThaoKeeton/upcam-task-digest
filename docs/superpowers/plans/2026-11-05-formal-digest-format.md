# Formal digest formatting and task references

**Goal:** Make the actual automatic report follow the confirmed time-range header, numbered task titles and concise editorial content, preserving title punctuation and source validation.

**Boundaries:** User messages and diagnostics stay private. A detailed diagnostic marked data_altered/sendable=false is analysis evidence only, never a sendable replay. Public tests contain newly synthesized data. No production requests, credentials, DingTalk sends or deployment are used in development.

**Design:** Keep the source diagnostic pipeline and its event evidence. Rebuild formal summaries and categories from structured audits after validated model summaries, rather than copying diagnostic prose. Use the actual canonical window for the header. Resolve task numbers from documented uniqueId and a confirmed project prefix; never infer them from commits, task IDs or edited titles. Support a locally saved all-view route and a documented standalone task-link fallback. Bind formatting to report reconstruction and template provenance.

- [ ] Add failing synthetic formal-output tests for headers, category priority, concise/empty summaries, identity, links and model-input privacy.
- [ ] Implement pure formal-content and task-reference helpers, then wire build and independent report validation to the same reconstruction.
- [ ] Add native settings for optional confirmed prefix/view routing, preserve configuration compatibility and sample-window consistency.
- [ ] Update current template/golden specification and documentation; retain diagnostics, pagination, event, duplicate, mapping and delivery guards.
- [ ] Review and run the full suite; verify Windows packaging, native interaction, screenshots and tested tree before merging main and delivering.
