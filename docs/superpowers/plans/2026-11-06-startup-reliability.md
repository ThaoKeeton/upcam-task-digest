# Ordinary Windows Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reproduce and fix the reported ordinary v0.4.0 startup crash.

**Architecture:** Exercise the actual no-argument EXE against a test-owned Windows
KnownFolder profile. Use UI Automation to require a ready main window, then apply
only fixes supported by the failing test and record safe startup error metadata.

**Tech Stack:** C# / WinUI 3, Windows App SDK 2.6.5, PowerShell, Python unittest.

**Spec:** `docs/superpowers/specs/2026-11-06-startup-reliability.md`

## Global Constraints

- No real credentials, provider requests, messages or user-system changes.
- Preserve schema 1, config, mappings, cache, history, DPAPI, tray and startup.
- Main is the baseline; use a PR and merge only verified changes.

## Review Focus

- Missing/system bootstrap must not pass an invalid language tag to MRT Core.
- Explicit Chinese and English must use the same initialization as ordinary runs.
- Malformed bootstrap must fall back safely without exposing its contents.
- The Windows test must never inspect or remove a pre-existing profile.
- Crash metadata must exclude exception messages, raw stacks and credentials.

### Task 1: Reproduce ordinary startup

Files: `tests/windows/ordinary_startup.ps1`, `.github/workflows/windows-build.yml`.

- [x] Add normal EXE launches without the synthetic flag, require an enabled
  `save-settings` UI Automation control, and retain only safe test evidence.
- [x] Guard the ephemeral CI KnownFolder, disable automatic work, and verify old
  settings are unchanged. Never use a real user profile or just override LOCALAPPDATA.
- [x] Run on unchanged v0.4.0 production source and inspect the actual failure.

### Task 2: Fix the demonstrated startup path

Files: `App.xaml.cs`, startup helpers/tests as needed, application version/docs.

- [ ] Only after RED, correct the demonstrated source of failure and add bounded
  error metadata for startup failures, without logging raw exception contents.
- [ ] Cover default/system, old config, both supported languages and invalid
  bootstrap. Repeat against the extracted complete ZIP.
- [ ] Run the whole unittest suite and native interaction/background tests.

### Task 3: Review and delivery

- [ ] Obtain fresh independent review; resolve actionable findings.
- [ ] Verify source tree, Windows artifact, images and all test results.
- [ ] Merge the PR, verify main and synchronize local files without overwriting
  unrelated work; deliver exact results and remaining target-PC limits.

## Execution ledger

Ruling: Proceed without another design approval because the user explicitly
authorized diagnosis, correction, tests, review and merge of this shipped defect.
No implementation change is approved as a root-cause fix until Windows reproduces it.

RED evidence: Windows CI50 (42168024484), unmodified v0.4.0 production code.
Fresh, legacy, system and invalid-bootstrap each exit before ready with
code -1073741189; explicit zh-CN and en-US both reach an enabled main-window
save control and preserve settings bytes. No exception contents were exported.

Ruling: Keep this fix to the demonstrated invalid API argument and its regression
coverage. The bounded CI report records startup error metadata; a general crash
reporter is outside this one-parameter correction. Any target-PC-only failure
will request Windows fault code/module rather than configuration or credentials.
