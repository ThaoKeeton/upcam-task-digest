# Communication Summary Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for this focused correction, then independent code review before integration.

**Goal:** Keep formal task summaries focused on substantive communication, without automatic attachment-processing or assignee-change appendices.

**Architecture:** Keep the existing validated collection, structured audit, summary API and renderer. Formal selection returns the validated communication summary; metadata remains local. Update the repository editorial rules and native synthetic acceptance case together.

**Tech Stack:** Python stdlib, unittest, C# WinUI 3, Windows GitHub Actions.

**Spec:** User feedback on 2026-11-05 identifies attachment counts and unread-content notices as unnecessary; preserve the confirmed report layout and meaningful communication facts.

## Global Constraints

- No real credentials, production API calls, enterprise content in fixtures or real DingTalk sends.
- Preserve title, actual time range, update overview, category priority, numbering, spacing and current status/owner row.
- Preserve attachment validation, original evidence, unknown-source blocking and duplicate-delivery guards.
- Attachment-only events remain activities, use an explicit empty summary and make zero model calls.

## Review Focus

- Mixed substantive comment and attachments: preserve the conclusion and risk exactly, without processing metadata.
- Attachment-only or assignee-only activity: retain the task without claiming no activity or inventing progress.
- Explicit handoff/deliverable facts in communication: preserve them; do not remove text using broad keywords.
- Conflicting or malformed file evidence: still block before the model and sender.
- Packaged application: same rules and behavior in foreground and extracted background startup.

## Task 1: Formal content and packaged regression

- [x] Change formal selector and end-to-end expectations; observe six failures caused by existing metadata appendices.
- [x] Stop appending file-processing and system assignee details; keep evidence validation.
- [x] Align repository editorial rules and model instruction with communication-focused summaries.
- [x] Update synthetic native workflow to include attachments/assignee automation and assert clean formal output.
- [ ] Run complete Python suite, obtain independent review and check the patch for private content.
- [ ] Build and interact with the Windows package, inspect screenshots/reports and verify source-tree identity.
- [ ] Integrate into main and verify the remote/local snapshot without overwriting working files.

## Task 2: Deliberate manual resend

User clarification: remove the confirmed-delivery restriction for new manual clicks without adding a confirmation dialog. Scheduled execution retains duplicate protection; in-flight or unknown outcomes must not be blindly retried.

- Backend: `DeliveryGuard.attempt` and `DigestWorkflow.run` accept `allow_confirmed_repeat=False` and `manual_request_id=None`; the worker manual route enables them with a UUID v4 per deliberate command. Scheduled calls omit these arguments and reject incompatible combinations.
- Additive ledger records retain prior delivery evidence, deduplicate request IDs and serialize the scope across independent connections. An unresolved earlier attempt blocks new sends.
- WinUI and legacy manual entry points create one request UUID per click. Native acceptance clicks the same send button twice on the same range with no modal confirmation and requires two confirmed history rows.
- Synthetic tests cover first/manual-repeat success, scheduled duplicate rejection, request replay, unknown outcomes, concurrent claims, old ledgers and invalid options. No real service is contacted.
