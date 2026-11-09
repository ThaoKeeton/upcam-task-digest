"""Check a built package's inventory and host/private dependency boundary.

The co-packaged hashes detect packaging mistakes, not an independent publisher
signature. Host package trust and source review remain separate requirements.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class PackageError(ValueError):
    pass


def _safe_part(part):
    stem = part.split(".")[0].rstrip(" ").upper()
    device = (stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
              or len(stem) == 4 and stem[:3] in {"COM", "LPT"} and stem[3] in "123456789¹²³")
    return (part not in ("", ".", "..") and not part.endswith((" ", ".")) and not device
            and not any(ord(c) < 32 or c in ':<>"|?*' for c in part))


def _safe_path(name):
    return (isinstance(name, str) and len(name) <= 1024 and "\\" not in name
            and not PurePosixPath(name).is_absolute()
            and all(_safe_part(part) for part in name.split("/")))


def _regular(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise PackageError("Package links or reparse points are not allowed.")
    return info


def audit_package(root: Path) -> dict:
    root = Path(root).absolute()
    try:
        for parent in (root, *root.parents):
            if parent.exists():
                _regular(parent)
        manifest_file = root / "plugin-package.json"
        if manifest_file.stat().st_size > 2 * 1024 * 1024:
            raise PackageError("Package inventory exceeds the local metadata budget.")
        metadata = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        plugin = json.loads((root / "plugin.json").read_text(encoding="utf-8-sig"))
        synthetic = metadata.get("syntheticAcceptance")
        expected_id = "com.upcam.task-digest.acceptance" if synthetic is True else "com.upcam.task-digest"
        if (type(synthetic) is not bool or metadata.get("schemaVersion") != 1
                or metadata.get("pluginId") != expected_id or plugin.get("id") != expected_id
                or plugin.get("version") != metadata.get("pluginVersion")
                or plugin.get("apiVersion") != 1
                or plugin.get("minimumHostVersion") != "0.2.0"
                or metadata.get("runtimeServicesVersion") != 1
                or metadata.get("hostReferenceCommit") != "d2005d469aef6a385c134b6914fb6dfa9cff20b2"
                or plugin.get("entryAssembly") != "UPCAM.Workbench.dll"
                or plugin.get("entryType") != "Upcam.Workbench.DigestPlugin"
                or metadata.get("backendVersion") != "0.4.5"
                or metadata.get("protocolVersion") != 1
                or metadata.get("windowsAppSdk") != "1.8.260921001"):
            raise PackageError("Plugin identity, host target or backend contract is inconsistent.")
        entries = metadata.get("files")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 5000:
            raise PackageError("Package file inventory is invalid.")
        expected = {}
        for item in entries:
            name = item["path"]
            if (not _safe_path(name) or name.casefold() in expected
                    or name == "plugin-package.json"
                    or not isinstance(item["bytes"], int) or not 0 <= item["bytes"] <= 256 * 1024 * 1024
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])):
                raise PackageError("Package inventory contains unsafe or duplicate entries.")
            expected[name.casefold()] = item
        actual = {}
        total = 0
        for path in root.rglob("*"):
            info = _regular(path)
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise PackageError("Only regular package files are allowed.")
            name = path.relative_to(root).as_posix()
            key = name.casefold()
            if not _safe_path(name) or key in actual:
                raise PackageError("Package contains unsafe or case-colliding entries.")
            total += info.st_size
            if total > 512 * 1024 * 1024 or len(actual) >= 5001:
                raise PackageError("Package exceeds the distribution budget.")
            host_assemblies = {"workbench.sdk.dll", "workbench.winui.sdk.dll", "microsoft.winui.dll",
                               "winrt.runtime.dll", "microsoft.windows.sdk.net.dll", "netstandard.dll", "mscorlib.dll"}
            dll_name = path.name.casefold()
            duplicated_runtime = (path.suffix.casefold() == ".dll" and
                                  (dll_name.startswith("system.") or dll_name == "system.dll"
                                   or dll_name.startswith("microsoft.windows.") and dll_name.endswith(".projection.dll")
                                   or dll_name.startswith("microsoft.windowsappruntime.")))
            if (key.endswith((".dpapi", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"))
                    or path.name.casefold() in (".env", "settings.json", "openai_models.json")
                    or path.name.casefold() in host_assemblies
                    or duplicated_runtime
                    or (path.suffix.casefold() == ".dll" and path.parent == root
                        and path.name != "UPCAM.Workbench.dll")):
                raise PackageError("Package contains profile data or a duplicate host/runtime assembly.")
            if name == "plugin-package.json":
                continue
            item = expected.get(key)
            if item is None or item["path"] != name or item["bytes"] != info.st_size:
                raise PackageError("Package file inventory does not match the built files.")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != item["sha256"]:
                raise PackageError("Package file integrity does not match the build inventory.")
            actual[key] = name
        required = {"plugin.json", "upcam.workbench.dll", "upcam.workbench.deps.json",
                    "worker/upcampluginworker.exe"}
        if set(actual) != set(expected) or not required <= set(actual):
            raise PackageError("Package is incomplete.")
        return {"plugin_id": expected_id, "plugin_version": plugin["version"],
                "verified_files": len(actual), "total_bytes": total,
                "synthetic_acceptance": synthetic, "publisher_signature_verified": False}
    except PackageError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise PackageError("Package metadata or files cannot be validated.") from exc


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise PackageError("Usage: audit_package.py PACKAGE_DIRECTORY")
        print(json.dumps(audit_package(Path(sys.argv[1])), separators=(",", ":")))
    except PackageError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
