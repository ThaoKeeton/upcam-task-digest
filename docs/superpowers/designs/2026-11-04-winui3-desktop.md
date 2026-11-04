# WinUI 3 Desktop Migration

## Goal

Replace the Tk window with a real WinUI 3 / Windows App SDK desktop application while preserving the existing Python automation core and the user's `%LOCALAPPDATA%\\UPCAM\\TaskDigest` data.

## Architecture

- Build the user interface as a C# .NET 8 WinUI 3 unpackaged Windows desktop app using the stable Windows App SDK 2.6.5.
- Package the existing Python business logic as a sibling worker executable. A private JSON-lines standard-input/output protocol connects the UI to the worker; credentials are never returned to the UI and are saved only by the existing DPAPI secret store.
- Keep configuration, encrypted secrets, model cache, SQLite history, delivery ledger, collection, validation, OpenAI calls, and DingTalk delivery in the existing Python modules. Do not reimplement safety rules in C#.
- Use WinUI `NavigationView` with independent, compact pages for dashboard, accounts/models, project scope, member mapping, status mapping, schedule, diagnostics, and history. Allow local page scrolling for dense tables and previews; do not wrap the complete app in one long scroll view.
- Keep the existing per-user Run entry name and singleton mutex for upgrade compatibility. Hide the main window to a native shell tray icon on close; explicit Exit stops the worker and removes the tray icon.
- Build and smoke-test on `windows-latest`; use only synthetic UI fixtures in the smoke path. Release a self-contained app directory ZIP, not a falsely advertised single-file executable.

## Constraints

- Do not read or use real credentials, make production Teambition/OpenAI requests, send DingTalk messages, modify the developer machine's registry, or deploy to the user's PC during development.
- Do not guess or relax the Teambition comment/commit parser. Wait for a user-provided redacted response-shape export before changing that protocol handling.
- Preserve settings, DPAPI credentials, model cache, SQLite history, schedule keys, and delivery ledger formats.
- A run must continue to rely on the existing Python validation and durable delivery guard.
- WinUI runtime dependencies must be included or documented according to Microsoft's deployment guidance.

## Verification

- Python tests exercise worker commands with temporary directories, memory secrets, and synthetic fixtures.
- Windows CI compiles the WinUI project, builds the Python worker, launches the actual WinUI window in a synthetic UI smoke mode, visits every navigation destination, verifies key controls and DPI-aware startup, and captures a screenshot artifact.
- The PR report must distinguish compiler and smoke evidence from the unavailable real Teambition structure test and from a user-computer installation test.
