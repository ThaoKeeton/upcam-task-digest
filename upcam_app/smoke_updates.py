"""Synthetic Releases transport; never opens a socket or executes an update."""

import hashlib
import io
import json
import zipfile

from . import RULESET_REVISION
from .updates import ASSET_NAME, GitHubUpdates, LATEST_RELEASE_API, REPOSITORY_API


class _Response:
    def __init__(self, payload):
        self.status = 200
        self.headers = {}
        self.payload = payload

    def iter_bytes(self, chunk_size):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset:offset + chunk_size]

    def close(self):
        pass


class SyntheticUpdateTransport:
    def __init__(self):
        version = "0.4.1"
        commit = "c" * 40
        files = {
            "UPCAMTaskDigest.exe": b"CI synthetic app; cannot execute",
            "UPCAMTaskDigest.dll": b"CI synthetic managed assembly",
            "UPCAMTaskDigest.pri": b"CI synthetic resources",
            "coreclr.dll": b"CI synthetic runtime",
            "hostfxr.dll": b"CI synthetic host",
            "Assets/Teambition.ico": b"CI synthetic icon",
            "Assets/Teambition.png": b"CI synthetic image",
            "worker/UPCAMWorker.exe": b"CI synthetic worker; cannot execute",
            "worker/_internal/templates/daily_digest.json": b'{"synthetic":true}',
            "worker/_internal/examples/digest.json": b'{"synthetic":true}',
        }
        files["build-info.json"] = json.dumps({
            "applicationVersion": version, "rulesetRevision": RULESET_REVISION, "sourceCommit": commit,
            "applicationSha256": hashlib.sha256(files["UPCAMTaskDigest.exe"]).hexdigest(),
            "workerSha256": hashlib.sha256(files["worker/UPCAMWorker.exe"]).hexdigest(),
        }).encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in files.items():
                archive.writestr(name, value)
        self.archive = stream.getvalue()
        asset_url = REPOSITORY_API + "/releases/assets/456"
        self.responses = {
            LATEST_RELEASE_API: json.dumps({
                "id": 123, "tag_name": "v" + version, "draft": False, "prerelease": False,
                "target_commitish": commit, "url": REPOSITORY_API + "/releases/123",
                "html_url": "https://github.com/ThaoKeeton/upcam-task-digest/releases/tag/v" + version,
                "assets": [{"id": 456, "name": ASSET_NAME, "state": "uploaded", "size": len(self.archive),
                            "url": asset_url, "content_type": "application/zip",
                            "browser_download_url": "https://github.com/ThaoKeeton/upcam-task-digest/releases/download/v" + version + "/" + ASSET_NAME,
                            "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest()}],
            }).encode(),
            asset_url: self.archive,
        }

    def request(self, url, *, headers, timeout):
        if url not in self.responses:
            raise AssertionError("Synthetic update attempted an unapproved request")
        return _Response(self.responses[url])


def synthetic_update_manager(directory, current_version):
    return GitHubUpdates(directory, current_version=current_version, transport=SyntheticUpdateTransport())
