# Native title bar and targeted diagnostics implementation plan

> **For agentic workers:** Execute this focused repair in the current session with the executing-plans workflow; request an independent final review.

**Goal:** Match the requested PowerToys native top structure, use Teambition branding consistently, and improve generic protocol diagnostics without guessing.

**Architecture:** Keep WinUI 3 / Windows App SDK and the existing Python worker. Use the SDK TitleBar control with OS caption buttons and inline NavigationView. Use one official 128 px transparent PNG and its lossless multi-size ICO for the window, EXE and tray. Diagnostic v3 only adds strict quote JSON shapes and safe action tokens; existing extraction gates remain.

**Tech Stack:** C# / XAML, Windows App SDK 2.6.5, Win32 shell, Python 3.12.

**Spec:** Requested PowerToys structure, consistent Teambition branding, and privacy-preserving protocol diagnostics.

## Global constraints

- Preserve schema-1 local data, DPAPI, tray, startup, scheduling and send deduplication.
- No real credentials, production API calls, actual messages or target-PC settings changes.
- No fake search control. System minimize, maximize, close, dragging and keyboard navigation must remain usable.
- Do not upscale beyond the official 128 px source. Supply 16, 20, 24, 32, 40, 48, 64, 96 and 128 px ICO images.
- Keep private input files, identifiers, findings and statistics outside the repository.

## Review focus

- Collapsed and expanded navigation at 640 px must reserve layout space and retain action controls.
- Native title bar must retain drag regions, dark caption buttons and window controls.
- DPI changes must load a correctly sized tray icon, dispose owned handles and survive Explorer restart.
- JSON-like quotes may be invalid, duplicate-key, nonfinite or double encoded; never normalize from a substring test.
- Exported JSON field names and protocol tokens must be bounded without exposing names, body text or secrets.

### Task 1: Targeted safe diagnostic evidence

- [x] Add failing strict-quote, privacy and protocol-token tests.
- [x] Add diagnostic v3: bounded strict nested quote shape parsing, decoded text categories and safe protocol action token. Preserve all source-validation blocks.
- [x] Run the complete Python suite and update export docs.

### Task 2: Native title bar and common branding

- [x] Obtain official public favicon from https://open.teambition.com/favicon.ico (PNG, 128x128 RGBA).
- [x] Add failing authentic-asset / transparency / multi-size tests.
- [x] Add native UI regression assertions for title bar, inline pane, same asset icons and OS caption behavior; confirm old UI fails in Windows CI.
- [x] Use the native TitleBar control and reserve a 48 DIP top row. Use inline Left pane; move toggle to title bar.
- [x] Embed the EXE icon and set native window/tray frames from Assets/Teambition.ico, replacing/discarding owned handles correctly.
- [ ] Publish and exercise the real Windows UI, archive round-trip, native window/tray screenshots, DPI evidence and existing controls with synthetic services.
- [ ] Review, re-run tests, merge into main under the existing authorization, verify remote tree and publish the artifact link with exact remaining limits.
