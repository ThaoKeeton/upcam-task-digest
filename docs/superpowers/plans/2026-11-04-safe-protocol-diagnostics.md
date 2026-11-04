# Safe protocol diagnostics

**Goal:** Make one local diagnostic useful for identifying unsupported event protocols and markup without exporting business text or changing automatic-send guards.

**Design:** Keep the existing plaintext action allowlist. Add domain-separated fingerprints for bounded protocol identifiers and an offline candidate matcher. Add bounded literal-markup metadata and fixed source-field shapes; none is rendering or provenance proof. Group local unknown-action errors by type rather than repeating tasks, and show explicit truncation. Provide a native copy action for the local diagnostic text.

**Constraints:** Start from main; synthetic tests and Windows CI only. No real keys, production calls, actual delivery, system startup changes, member-name lookup, fabricated action semantics or commit provenance. Do not commit user diagnostics, identifiers or member lists.

## Implementation

- [ ] Red/green tests for diagnostic fingerprints and offline matching, including legacy missing evidence, malformed tokens, bounds and privacy.
- [ ] Red/green tests and implementation for bounded markup hints. Preserve unknown classifications and never strip or approve content.
- [ ] Red/green tests and implementation for grouped unknown-action reports, mixed blocker visibility and explicit bounded truncation.
- [ ] Integrate diagnostic v5, fixed commit-source-field shapes and native diagnostic-copy control. Keep cache/window identity checks and version compatibility.
- [ ] Update version/instructions, run full tests and independent review, then verify real Windows UI, extracted ZIP, tray/background startup and remote main integration.

## Evidence limits

A fingerprint match establishes an exact candidate identifier, never the event's meaning. Literal marker hints do not establish rendered semantics. Source-field presence does not prove a provider, repository or commit. Unsupported events continue to block model requests and delivery.
