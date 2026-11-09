# UPCAM 0.1.1 / Workbench 0.4.0 shipping-pair QA

This is a proposed Windows CI addition, not a passed Windows result.

Copy `ShippingInstall/` and `test-shipping-host.ps1` into `integrations/project-workbench/shipping-pairing/`, and append `pairing-job.yml` under the existing plugin workflow's jobs. The new job consumes the production plugin already built by `windows-native`; it does not rebuild either the host or plugin.

The pinned host is the actual artifact from Workbench commit 655d7b6496067daad7c5e4cefb1f1da1b8379536, run 42518602297, artifact 13633291911. The downloaded archive, EXE, App DLL, and loaded Core DLL are independently hash-checked. The tiny installer is compiled against the shipping Core DLL, not copied host sources. It performs `PluginPackageStore.InstallAsync` on the audited production folder, verifies every installed file's bytes and disabled/untrusted/unloaded state, then exits. The unchanged shipping EXE opens normally; UI Automation handles native trust, enable, page and backend, no-project diagnostic rejection, disable/re-enable, and graceful close with owned worker cleanup.

A separate fresh runner is required because the existing complete-reference-host UI test already creates `%LOCALAPPDATA%/ProjectWorkbench`. This test refuses an existing profile. Do not remove that profile or weaken the gate to fit the same job. Windows PowerShell 5.1/STA is used for native UIA, PowerShell 7 for orchestration.

No real service credentials, project IDs, data, provider calls, OpenAI calls, Webhook or DingTalk sends are involved. No existing profile is read or removed. No task cancellation during real collection is claimed: the unconfigured diagnostic rejects before collection. Cancel-disabled after rejection and lifecycle cleanup are checked; synthetic transport cancellation remains separate evidence. Native folder-picker installation is not exercised; the actual shipping PackageStore is.

The upstream UIA process ownership, clean host environment, fresh root, symlink rejection, fixed/sanitized selector diagnostics, and bounded job-object cleanup are retained. Evidence includes input identities, PackageStore result, UIA report and an owned unconfigured window screenshot. Success must include all checks and `cleanup: graceful`.

The public cross-repository Actions artifact download uses the job's temporary GitHub token. If GitHub rejects that token's artifact access, stop and report that access error. Do not add a secret, weaken authorization, rebuild and call it the same binary, or silently switch host versions. A separately authorized artifact-transfer route would then be needed.
