# Native general settings

## Intent and scope

Implement the user's requested PowerToys-style general settings in the existing WinUI 3 application: stable updates, supported interface languages, system/light/dark themes, existing sign-in startup, and tray behavior. Preserve every business setting, credential, model cache, report rule, history entry, and delivery guard. The supplied PowerToys screenshots are references, not a request to duplicate unrelated tools, administrator elevation, global hotkeys, or backup products.

## Decisions

- Add a General navigation page with local sections for Updates, Appearance, and Startup and tray. Keep the existing scheduler page focused on report automation. Reuse the current-user startup integration and native tray.
- Language choices are Windows default, Simplified Chinese, and English. Localize actual interface controls, navigation, help, and dialogs using separate resources. Project text, names, reports, and source diagnostics remain unchanged. Theme choices are Windows default, light, and dark; caption buttons, cards, dialogs, and secondary text follow the effective theme.
- Keep existing startup disabled by default and sign-in startup in the tray. Tray icon enabled and closing-to-tray remain defaults; optional manual-launch-to-tray is off. Disable incompatible hiding behavior when the tray is disabled and always keep a recoverable visible window if registration fails.
- The repository is private and has no Releases. Add an optional local GitHub token protected by the existing Windows-user DPAPI store. Read only this fixed repository's Releases and assets; do not use OpenAI or enterprise credentials. No token or inaccessible release means an explicit unavailable/access state, never an assertion that the current app is latest.
- Stable updates require an uploaded correctly named ZIP, a SHA-256 digest from authenticated GitHub metadata, a canonical release version and commit, matching packaged build metadata, and bounded Windows-safe archive validation. Never install Actions artifacts or arbitrary URLs. Strip authorization on permitted asset redirects.
- Stage a verified whole application under the current user's application data. Installation is side by side, does not require elevation or overwrite the running installation, and preserves the old directory. Revalidate before launch, coordinate parent exit/single-instance acquisition, then refresh the existing sign-in startup path only after the new app initializes successfully. Do not silently discard unsaved work or interrupt a send. Report failures without credentials or signed URLs.
- Provide a manually triggered release workflow for future stable versions after tests/builds succeed. This development run does not create a release or alter repository visibility. Clearly disclose that first live release publication remains necessary before real update distribution can be exercised.
- Automatic update checks are opt in; downloading/installing requires an explicit application action. Keep report execution independent of update availability.

## Alternatives

Retaining only expiring Actions artifacts cannot provide a stable update channel. Executing an unchecked ZIP or elevating a generic installer violates the requested source/integrity and permission requirements. Use fixed-repository verified Releases and a per-user side-by-side deployment.

## Verification

Use synthetic transports and temporary data only. Test inaccessible/missing releases, versions and digests, credential stripping, corrupt/malicious ZIPs, staging permissions and tampering, preference migration and atomic save failures, language changes without business-text mutation, theme/dialog/caption consistency, tray-disabled recovery, old startup defaults, and unsaved/in-flight update refusal. Build and run real Windows controls at the existing viewport sizes, exercise both supported languages/themes and a verified synthetic update handoff, and retain evidence. No real API keys, production requests, messages, or user-machine configuration changes.
