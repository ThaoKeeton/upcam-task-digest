# Diagnostic responsiveness implementation plan

Goal: make the long read-only diagnostic observable, cancellable and bounded without changing collection completeness or touching existing profile data.
Architecture: request-scoped, opt-in numeric progress frames keep existing plugin protocol behavior; the native transport owns deadlines and kills an interrupted worker without replay; only read-only diagnosis exposes cancellation and safely reopens the same profile afterward.
Baseline: main e410979, 959 Python cases pass (12 platform skips). Screenshot alone does not prove a user-machine deadlock. Confirmed code defects are unlimited native stdout wait and no visible progress/cancel for full-project collection.

Constraints: no real enterprise calls, OpenAI calls or DingTalk/Webhook sends in verification; no changes to mappings, settings, history or send ledger schemas; fixed host SDK reference remains unchanged. Version backend/standalone 0.4.4, plugin 0.1.1. No merge, stable release or deployment.

Review focus: coalesced/fragmented protocol lines; malformed/unbounded progress; cancelling before and during a read; a worker that never replies; restore same profile without replay or restarting an authorized scheduler unexpectedly.

1. Write failing synthetic collection/protocol tests. Add request-scoped numeric progress and coordinator/validation checkpoints, preserving non-opt-in one-response framing and secrecy. Run focused and full Python suites.
2. Write a source-linked real process C# probe for bounded line parsing, progress, silence/overall deadlines, cancellation and no replay. Add cancellable native transport and diagnostic-only progress/elapsed/cancel UI. Reinitialization uses the same profile; failure disables worker actions. Run resource audit and Windows probe/UI smoke in Actions.
3. Update exact version contracts, operation guide and scoped test-branch workflow triggers. Push only the test branch; verify exact-commit standalone and plugin Windows jobs and download links. Record never-run user-machine/real-source verification accurately.

Implementation ledger:
- 2026-11-09: request-scoped safe progress passed focused tests; service progress remains opt-in for diagnosis and saving's read-only scope probe. Existing non-opt-in callers retain one final response.
- Independent review found three issues: removed reader call in WinUI smoke, legitimate slow task versus short silence watchdog, and updates incorrectly using a 30s deadline. Fixed the stale call; added per-request numeric refresh and an 11-minute silence bound covering the backend's maximum600-second single batch; updater gets6minutes for its5-minute backend budget. The synthetic140+second paging regression and delayed-update real-child test were observed red before their fixes, then green.
- Ruling: diagnostic no longer implicitly saves dirty settings/credentials, matching plugin behavior. User must save first; scope saving now displays safe counts. This avoids making a read-only operation silently write settings or start a second invisible full scan. No new cancellation is added around standalone configuration writes.
- Actual local .NET8 process probe passed after installing the official SDK into a temporary directory. Sandbox mutex restrictions required an approved isolated execution. No production services or credentials were used.
- Same-profile recovery uses the unchanged default WorkerClient profile path and waits for old worker exit. Synthetic UI smoke's fresh temporary profiles verify cancellation/reuse but do not prove real existing-profile preservation. Existing profile/checkpoint regression remains separate; Windows/user-machine evidence is still pending.
