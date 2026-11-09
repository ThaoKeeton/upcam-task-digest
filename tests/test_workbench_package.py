"""Offline checks for the pinned host input and plugin distribution boundary."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "project-workbench"


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, INTEGRATION / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkbenchReferenceTests(unittest.TestCase):
    def test_exact_platform_source_blobs_and_sdk_versions(self):
        verifier = load_script("verify_reference")
        result = verifier.verify_reference(INTEGRATION / "reference")
        self.assertEqual(result["commit"], "d2005d469aef6a385c134b6914fb6dfa9cff20b2")
        self.assertEqual(result["verified_files"], 42)

    def test_source_tampering_is_rejected(self):
        verifier = load_script("verify_reference")
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference"
            shutil.copytree(INTEGRATION / "reference", reference,
                            ignore=shutil.ignore_patterns("bin", "obj"))
            file = reference / "src/Workbench.Sdk/PluginContracts.cs"
            file.write_bytes(file.read_bytes() + b"\n// changed\n")
            with self.assertRaises(verifier.ReferenceError):
                verifier.verify_reference(reference)

    def test_unlisted_compile_input_is_rejected_but_build_outputs_are_ignored(self):
        verifier = load_script("verify_reference")
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference"
            shutil.copytree(INTEGRATION / "reference", reference,
                            ignore=shutil.ignore_patterns("bin", "obj"))
            generated = reference / "src/Workbench.Sdk/obj/Release/Generated.cs"
            generated.parent.mkdir(parents=True)
            generated.write_text("// normal generated build output")
            self.assertEqual(verifier.verify_reference(reference)["verified_files"], 42)
            added = reference / "src/Workbench.Sdk/Unlisted.cs"
            added.write_text("// not part of the pinned upstream tree")
            with self.assertRaises(verifier.ReferenceError):
                verifier.verify_reference(reference)

    def test_reference_manifest_cannot_escape_or_repeat_a_path(self):
        verifier = load_script("verify_reference")
        original = json.loads((INTEGRATION / "reference/source-manifest.json").read_text())
        for bad_path in ("../outside.cs", "/outside.cs", "src\\outside.cs", "src/../outside.cs"):
            with self.subTest(path=bad_path), tempfile.TemporaryDirectory() as directory:
                reference = Path(directory)
                manifest = deepcopy(original)
                manifest["files"][0]["path"] = bad_path
                (reference / "source-manifest.json").write_text(json.dumps(manifest))
                with self.assertRaises(verifier.ReferenceError):
                    verifier.verify_reference(reference)

    def test_reference_provenance_must_match_the_pinned_commit(self):
        verifier = load_script("verify_reference")
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "reference"
            shutil.copytree(INTEGRATION / "reference", reference,
                            ignore=shutil.ignore_patterns("bin", "obj"))
            file = reference / "source-manifest.json"
            manifest = json.loads(file.read_text())
            manifest["commit"] = "0" * 40
            file.write_text(json.dumps(manifest))
            with self.assertRaises(verifier.ReferenceError):
                verifier.verify_reference(reference)

    def test_standalone_version_and_default_entry_remain_separate(self):
        self.assertIn("<Version>0.4.4</Version>",
                      (ROOT / "winui/UPCAMTaskDigest/UPCAMTaskDigest.csproj").read_text())
        source = (ROOT / "worker_entry.py").read_text()
        self.assertNotIn("plugin_handshake", source)
        self.assertNotIn("--data-directory", source)

    def test_ci_watches_each_source_linked_standalone_input(self):
        workflow = (ROOT / ".github/workflows/workbench-plugin.yml").read_text()
        for name in ("winui/UPCAMTaskDigest/DesktopState.cs", "winui/UPCAMTaskDigest/UiStrings.cs", "winui/UPCAMTaskDigest/WorkerProtocol.cs",
                     "winui/UPCAMTaskDigest/Strings/en-US.json", "THIRD_PARTY_NOTICES.md", "LICENSES/**"):
            self.assertIn("'" + name + "'", workflow)

    def test_worker_data_paths_are_anchored_outside_the_spec_directory(self):
        script = (INTEGRATION / "build-plugin.ps1").read_text()
        self.assertIn("(Join-Path $repo 'templates')", script)
        self.assertIn("(Join-Path $repo 'examples')", script)
        self.assertNotIn("'--add-data', 'templates;templates'", script)

    def test_complete_host_driver_is_ci_only_sta_and_preserves_chinese_selectors(self):
        driver = (INTEGRATION / "test-real-host.ps1").read_bytes()
        self.assertTrue(driver.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 needs the UTF-8 BOM")
        source = driver.decode("utf-8-sig")
        self.assertIn("$env:RUNNER_ENVIRONMENT -ceq 'github-hosted'", source)
        self.assertIn("$PSVersionTable.PSEdition -ne 'Desktop'", source)
        self.assertIn("freshNormalUserRoot", source)
        self.assertNotIn("Remove-Item", source)
        workflow = (ROOT / ".github/workflows/workbench-plugin.yml").read_text()
        self.assertIn("reference/src/Workbench.App/Workbench.App.csproj", workflow)
        self.assertIn("'-NonInteractive', '-STA'", workflow)

    def test_acceptance_artifact_is_uploaded_separately_only_after_success(self):
        workflow = (ROOT / ".github/workflows/workbench-plugin.yml").read_text()
        marker = "name: UPCAMTaskDigest-workbench-acceptance-0.1.1-windows"
        self.assertIn(marker, workflow)
        position = workflow.index(marker)
        start = workflow.rfind("- uses: actions/upload-artifact@v4", 0, position)
        finish = workflow.index("retention-days:", position)
        upload = workflow[start:finish]
        self.assertIn("if: success()", upload)
        for path in ("UPCAMTaskDigest-workbench-acceptance-0.1.1-win-x64.zip",
                     "UPCAMTaskDigest-workbench-acceptance-0.1.1-win-x64.zip.sha256",
                     "acceptance-handoff.json", "ACCEPTANCE.zh-CN.md"):
            self.assertIn("dist/workbench-plugin/" + path, upload)
        self.assertNotIn("workbench-production-", upload)

    def test_acceptance_handoff_checks_identity_hash_and_records_source(self):
        workflow = (ROOT / ".github/workflows/workbench-plugin.yml").read_text()
        self.assertIn("name: Record verified synthetic package handoff", workflow)
        self.assertIn("Get-FileHash -LiteralPath $archive -Algorithm SHA256", workflow)
        self.assertIn("$declaredHash -cne $hash", workflow)
        self.assertIn("$metadata.pluginId -cne 'com.upcam.task-digest.acceptance'", workflow)
        self.assertIn("$metadata.syntheticAcceptance -ne $true", workflow)
        for field in ("buildCommit", "sourceHeadCommit", "hostReferenceCommit", "minimumHostVersion",
                      "windowsAppSdk", "sha256", "publisherSignatureVerified"):
            self.assertIn(field + " =", workflow)

    def test_acceptance_package_has_an_explicit_isolated_handoff_guide(self):
        guide = (INTEGRATION / "ACCEPTANCE.zh-CN.md").read_text()
        self.assertIn("com.upcam.task-digest.acceptance", guide)
        self.assertIn(".plugin-synthetic.json", guide)
        self.assertIn("不填写真实凭据", guide)
        self.assertIn("不访问真实 Webhook", guide)
        script = (INTEGRATION / "build-plugin.ps1").read_text()
        self.assertIn("(Join-Path $PSScriptRoot 'ACCEPTANCE.zh-CN.md')", script)
        self.assertIn("https://github.com/ThaoKeeton/upcam-task-digest/blob/main/integrations/project-workbench/ACCEPTANCE.zh-CN.md",
                      (INTEGRATION / "README.zh-CN.md").read_text())


class WorkbenchDistributionTests(unittest.TestCase):
    def make_package(self, root):
        files = {
            "UPCAM.Workbench.dll": b"synthetic assembly placeholder, not executable",
            "UPCAM.Workbench.deps.json": b"{}",
            "worker/UPCAMPluginWorker.exe": b"synthetic worker placeholder, not executable",
            "plugin.json": json.dumps({
                "id": "com.upcam.task-digest", "version": "0.1.1", "apiVersion": 1,
                "minimumHostVersion": "0.2.0",
                "entryAssembly": "UPCAM.Workbench.dll", "entryType": "Upcam.Workbench.DigestPlugin",
            }).encode(),
        }
        for name, data in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        metadata = {"schemaVersion": 1, "pluginId": "com.upcam.task-digest",
                    "pluginVersion": "0.1.1", "backendVersion": "0.4.4",
                    "protocolVersion": 1, "syntheticAcceptance": False,
                    "hostReferenceCommit": "d2005d469aef6a385c134b6914fb6dfa9cff20b2",
                    "runtimeServicesVersion": 1,
                    "windowsAppSdk": "1.8.260921001",
                    "files": [{"path": name, "bytes": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in files.items()]}
        (root / "plugin-package.json").write_text(json.dumps(metadata))
        return metadata

    def test_private_package_inventory_matches_and_excludes_host_runtime(self):
        auditor = load_script("audit_package")
        with tempfile.TemporaryDirectory(prefix="plugin package with spaces ") as directory:
            root = Path(directory)
            self.make_package(root)
            result = auditor.audit_package(root)
            self.assertEqual(result["verified_files"], 4)
            self.assertFalse(result["synthetic_acceptance"])

    def test_unlisted_file_changed_file_and_host_sdk_copy_are_rejected(self):
        auditor = load_script("audit_package")
        for name in ("credentials.dpapi", "Workbench.Sdk.dll", "Microsoft.WinUI.dll",
                     "System.Private.CoreLib.dll", "worker/credentials.dpapi", "worker/Workbench.Sdk.dll"):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.make_package(root)
                (root / name).write_bytes(b"fixture")
                with self.assertRaises(auditor.PackageError):
                    auditor.audit_package(root)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_package(root)
            (root / "UPCAM.Workbench.dll").write_bytes(b"changed")
            with self.assertRaises(auditor.PackageError):
                auditor.audit_package(root)

    def test_windows_device_names_and_duplicate_paths_are_not_portable(self):
        auditor = load_script("audit_package")
        for path in ("CON", "worker/NUL.txt", "worker/LPT¹.dll", "worker/COM1.exe", "worker/../outside"):
            self.assertFalse(auditor._safe_path(path), path)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = self.make_package(root)
            metadata["files"].append(deepcopy(metadata["files"][0]))
            (root / "plugin-package.json").write_text(json.dumps(metadata))
            with self.assertRaises(auditor.PackageError):
                auditor.audit_package(root)

    def test_correctly_inventoried_nested_host_or_dotnet_runtime_is_rejected(self):
        auditor = load_script("audit_package")
        for name in ("worker/Workbench.Sdk.dll", "worker/Microsoft.WinUI.dll", "worker/WinRT.Runtime.dll",
                     "worker/System.Private.CoreLib.dll", "worker/System.Runtime.dll", "worker/netstandard.dll",
                     "worker/Microsoft.Windows.AppNotifications.Projection.dll"):
            with self.subTest(path=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                metadata = self.make_package(root)
                data = b"correctly inventoried but belongs to host or dotnet"
                (root / name).write_bytes(data)
                metadata["files"].append({"path": name, "bytes": len(data),
                                          "sha256": hashlib.sha256(data).hexdigest()})
                (root / "plugin-package.json").write_text(json.dumps(metadata))
                with self.assertRaises(auditor.PackageError):
                    auditor.audit_package(root)

    def test_test_variant_must_have_separate_identity(self):
        auditor = load_script("audit_package")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = self.make_package(root)
            metadata["syntheticAcceptance"] = True
            (root / "plugin-package.json").write_text(json.dumps(metadata))
            with self.assertRaises(auditor.PackageError):
                auditor.audit_package(root)


if __name__ == "__main__":
    unittest.main()
