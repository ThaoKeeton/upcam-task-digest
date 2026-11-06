# General Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox syntax for tracking.

**Goal:** Deliver native general settings, real language/theme choices, reusable startup/tray controls, and a verified private-repository update channel.

**Architecture:** Keep the Python worker as the credential and update trust boundary. Native WinUI owns localized presentation and process handoff. General preferences save independently from project verification, so appearance changes never cause enterprise requests.

**Tech Stack:** Existing Python standard library, C#/.NET 8, WinUI 3, Windows App SDK, DPAPI, GitHub Releases and Windows CI.

**Spec:** docs/superpowers/specs/2026-11-06-general-settings-design.md

## Global Constraints

- Fixed repository ThaoKeeton/upcam-task-digest, stable channel only, private access optional local token.
- Preserve report format/rules, existing data paths, ledger identities and delivery guards.
- No administrator requirement, real credentials, production API requests, real messages or user-machine deployment in development.
- Only Windows default, zh-CN and en-US are advertised languages; themes system/light/dark.

## Review Focus

- Old settings and a blank project must allow general settings saves without probing or clearing verification.
- Authenticated asset redirects must not forward the token; missing digests and unsafe archives must block installation.
- A disabled/unavailable tray must never hide the last usable application window.
- Theme/language changes must preserve unsaved project and secret drafts and the original report/business text.
- A running send, failed new startup or unwritable target must preserve the old app and prevent blind restarts.

## Task 1: Verified release and archive boundary

- [ ] Add failing synthetic tests in tests/test_app_updates.py for fixed release URLs, stable versions, access/no-release states, asset digest, redirects and Windows archive names/limits.
- [ ] Implement upcam_app/updates.py with injectable transport, check/download/stage/prepare operations and safe error codes. Expose no token or signed URL.
- [ ] Verify safe staging, old app retention, tampered downloads/build metadata and unwritable targets.

## Task 2: Preferences and worker integration

- [ ] Add migration/validation and worker tests for ui_language, ui_theme, tray_icon_enabled, close_to_tray, start_in_tray, auto_check_updates and existing start_with_windows.
- [ ] Add a narrow save_preferences operation preserving business data/drafts and requiring no enterprise request; add the DPAPI GitHub token field and fixed update commands.
- [ ] Verify failures roll back preference/credential/startup writes and old settings remain compatible.

## Task 3: Native resources and general page

- [ ] Add separate English interface resources and a deterministic language resolver, with resource/behavior checks.
- [ ] Add the General page, local preference save, effective theme/caption/dialog handling and live tray configuration. Remove duplicate startup controls from the report scheduler page.
- [ ] Verify actual English/Chinese controls, restored preferences, system theme, no tray lockout, and untouched report text.

## Task 4: Update handoff and distribution

- [ ] Add explicit verified install/restart handling, single-instance parent handoff and startup-path repair after successful initialization.
- [ ] Include version/rules/source/hashes in packaged build metadata and a manually triggered stable release workflow gated on successful build/tests.
- [ ] Exercise a fully synthetic verified update without a real release or registry change.

## Task 5: Delivery

- [ ] Run complete Python tests, independent security/compatibility review and Windows builds/native interaction checks.
- [ ] Check actual screenshots and source-tree equality, merge into main, verify remote/local state and deliver the whole application ZIP.
- [ ] Explain private-token/release setup, update verification guarantees and actual unverified deployment/client limitations.
