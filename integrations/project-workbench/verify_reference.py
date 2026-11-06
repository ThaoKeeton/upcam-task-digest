"""Verify the exact upstream build/test input; this is not publisher signing."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys


class ReferenceError(ValueError):
    pass


PINNED_COMMIT = "6565180b17130f6d494978291df4138de3fdb0b4"
PINNED_TREE = "3402ab69645bef0ef0f81061222360ebd8d1c838"


def _reject_link(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise ReferenceError("Reference source links are not allowed.")
    return info


def verify_reference(reference: Path) -> dict:
    reference = Path(reference)
    try:
        manifest_info = _reject_link(reference / "source-manifest.json")
        if not stat.S_ISREG(manifest_info.st_mode) or manifest_info.st_size > 2 * 1024 * 1024:
            raise ReferenceError("Reference provenance is invalid.")
        manifest = json.loads((reference / "source-manifest.json").read_text(encoding="utf-8"))
        if (manifest["repository"] != "ThaoKeeton/project-workbench"
                or manifest["commit"] != PINNED_COMMIT or manifest.get("tree") != PINNED_TREE
                or manifest.get("branch") != "feat/native-plugin-workbench"
                or not isinstance(manifest["files"], list) or not manifest["files"]):
            raise ReferenceError("Reference provenance is invalid.")
        seen = set()
        for item in manifest["files"]:
            name = item["path"]
            path = PurePosixPath(name)
            if (not isinstance(name, str) or path.is_absolute() or "\\" in name
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or ":" in name or name.casefold() in seen
                    or not re.fullmatch(r"[0-9a-f]{40}", item["gitBlob"])):
                raise ReferenceError("Reference inventory contains an unsafe or repeated path.")
            seen.add(name.casefold())
            file = reference.joinpath(*path.parts)
            for part in (file, *file.parents):
                _reject_link(part)
            if not stat.S_ISREG(file.lstat().st_mode):
                raise ReferenceError("Reference sources must be regular files.")
            data = file.read_bytes()
            actual = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
            if actual != item["gitBlob"]:
                raise ReferenceError("Pinned reference source changed: " + name)
        # Default Compile globs and Directory.Build imports must not accept an
        # extra source/build input while the package is described as upstream.
        build_directories = {str(PurePosixPath(item["path"]).parent / output)
                             for item in manifest["files"] if item["path"].endswith(".csproj")
                             for output in ("bin", "obj")}
        expected_files = {item["path"] for item in manifest["files"]} | {"source-manifest.json"}
        for directory, directories, files in os.walk(reference, followlinks=False):
            current = Path(directory)
            for name in list(directories):
                child = current / name
                _reject_link(child)
                if child.relative_to(reference).as_posix() in build_directories:
                    directories.remove(name)
            for name in files:
                file = current / name
                _reject_link(file)
                if file.relative_to(reference).as_posix() not in expected_files:
                    raise ReferenceError("The reference contains an unlisted source or build input.")
        return {"repository": manifest["repository"], "commit": manifest["commit"],
                "verified_files": len(seen)}
    except ReferenceError:
        raise
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ReferenceError("Reference sources or provenance cannot be verified.") from exc


if __name__ == "__main__":
    try:
        root = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(__file__).with_name("reference")
        print(json.dumps(verify_reference(root), separators=(",", ":")))
    except ReferenceError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
