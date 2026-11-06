"""Synthetic, network-free tests for the private stable update boundary."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile


REPOSITORY_URL = "https://api.github.com/repos/ThaoKeeton/upcam-task-digest"
LATEST_URL = REPOSITORY_URL + "/releases/latest"
ASSET_URL = REPOSITORY_URL + "/releases/assets/456"
RELEASE_URL = "https://github.com/ThaoKeeton/upcam-task-digest/releases/tag/v0.4.0"
ASSET_NAME = "UPCAMTaskDigest-WinUI3-win-x64.zip"
COMMIT = "1a" * 20
TOKEN = "synthetic-read-only-token-never-return"
SIGNED_URL = ("https://release-assets.githubusercontent.com/"
              "github-production-release-asset/123/abc-def?sig=synthetic-signed-secret")


class Response:
    def __init__(self, status=200, body=b"", headers=None, *, chunks=None):
        self.status = status
        self.headers = dict(headers or {})
        self.body = body
        self.chunks = chunks
        self.closed = False

    def iter_bytes(self, chunk_size):
        if self.chunks is not None:
            yield from self.chunks
        else:
            for offset in range(0, len(self.body), chunk_size):
                yield self.body[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, url, *, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):
            response = response.pop(0)
        return response


def archive_bytes(*, version="0.4.0", commit=COMMIT, rules="2026-11-06.1",
                  extra=(), omit=(), manifest_changes=None, compression=zipfile.ZIP_DEFLATED):
    files = {
        "UPCAMTaskDigest.exe": b"synthetic-app-executable",
        "UPCAMTaskDigest.dll": b"synthetic-native-managed-code",
        "UPCAMTaskDigest.pri": b"synthetic-xaml-resources",
        "coreclr.dll": b"synthetic-runtime",
        "hostfxr.dll": b"synthetic-host",
        "Assets/Teambition.ico": b"synthetic-icon",
        "Assets/Teambition.png": b"synthetic-image",
        "worker/UPCAMWorker.exe": b"synthetic-worker-executable",
        "worker/_internal/templates/daily_digest.json": b'{"synthetic":true}',
        "worker/_internal/examples/digest.json": b'{"synthetic":true}',
    }
    manifest = {
        "applicationVersion": version, "rulesetRevision": rules,
        "sourceCommit": commit,
        "applicationSha256": hashlib.sha256(files["UPCAMTaskDigest.exe"]).hexdigest(),
        "workerSha256": hashlib.sha256(files["worker/UPCAMWorker.exe"]).hexdigest(),
    }
    manifest.update(manifest_changes or {})
    files["build-info.json"] = json.dumps(manifest).encode()
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=compression) as archive:
        for name, value in files.items():
            if name not in omit:
                archive.writestr(name, value)
        for name, value in extra:
            archive.writestr(name, value)
    return payload.getvalue()


def release_metadata(payload, *, version="0.4.0"):
    return {
        "id": 123, "tag_name": "v" + version,
        "draft": False, "prerelease": False, "target_commitish": COMMIT,
        "url": REPOSITORY_URL + "/releases/123",
        "html_url": "https://github.com/ThaoKeeton/upcam-task-digest/releases/tag/v" + version,
        "assets": [{
            "id": 456, "name": ASSET_NAME, "state": "uploaded", "size": len(payload),
            "url": ASSET_URL,
            "browser_download_url": "https://github.com/ThaoKeeton/upcam-task-digest/releases/download/v"
                                    + version + "/" + ASSET_NAME,
            "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "content_type": "application/zip",
        }],
    }


def make_transport(payload=None, metadata=None, *, asset_response=None):
    payload = archive_bytes() if payload is None else payload
    metadata = release_metadata(payload) if metadata is None else metadata
    return Transport({LATEST_URL: Response(body=json.dumps(metadata).encode()),
                      ASSET_URL: asset_response or Response(body=payload),
                      REPOSITORY_URL: Response(body=json.dumps({
                          "full_name": "ThaoKeeton/upcam-task-digest", "private": True}).encode())})


class AppUpdateTests(unittest.TestCase):
    def setUp(self):
        try:
            self.updates = importlib.import_module("upcam_app.updates")
        except ModuleNotFoundError:
            self.fail("The verified stable update module has not been implemented")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "user-data"

    def updater(self, transport=None, *, current="0.3.10"):
        return self.updates.GitHubUpdates(self.data, current_version=current,
                                          transport=transport or make_transport())

    def assert_safe(self, value):
        serialized = repr(value)
        for secret in (TOKEN, "synthetic-signed-secret", "synthetic-response-secret"):
            self.assertNotIn(secret, serialized)

    def assert_rejected(self, operation, *, code=None):
        with self.assertRaises(self.updates.UpdateError) as raised:
            operation()
        error = raised.exception
        self.assertTrue(error.code)
        self.assertTrue(error.message)
        self.assertEqual(str(error), error.message)
        if code:
            self.assertEqual(error.code, code)
        self.assert_safe(error)
        return error

    def current_executable(self):
        current = self.root / "old-installation" / "UPCAMTaskDigest.exe"
        current.parent.mkdir(exist_ok=True)
        current.write_bytes(b"old application stays untouched")
        return current

    def test_missing_token_reports_private_access_without_network_or_writes(self):
        transport = Transport({})
        updater = self.updater(transport)
        for operation in (updater.check, updater.download):
            result = operation()
            self.assertEqual(result["status"], "access_required")
            self.assert_safe(result)
        self.assertFalse(transport.calls)
        self.assertFalse(self.data.exists())

    def test_authenticated_stable_check_returns_only_canonical_safe_metadata(self):
        transport = make_transport()
        result = self.updater(transport).check(TOKEN)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["version"], "0.4.0")
        self.assertEqual(result["source_commit"], COMMIT)
        self.assertEqual(result["release_url"], RELEASE_URL)
        self.assertEqual(transport.calls[0][0], LATEST_URL)
        self.assertEqual(transport.calls[0][1]["Authorization"], "Bearer " + TOKEN)
        self.assert_safe(result)
        self.assertFalse(self.data.exists())

    def test_versions_are_compared_semantically(self):
        cases = [("0.3.9", "0.3.10", "up_to_date"),
                 ("0.3.10", "0.3.10", "up_to_date"),
                 ("0.10.0", "0.9.9", "available")]
        for released, current, status in cases:
            with self.subTest(released=released, current=current):
                payload = archive_bytes(version=released)
                result = self.updater(make_transport(payload, release_metadata(payload, version=released)),
                                      current=current).check(TOKEN)
                self.assertEqual(result["status"], status)

    def test_no_release_is_distinguished_from_inaccessible_private_repository(self):
        for repository_status, expected in ((200, "no_release"), (404, "access_required"),
                                             (403, "access_required"), (503, "unavailable")):
            with self.subTest(repository_status=repository_status):
                transport = make_transport()
                transport.responses[LATEST_URL] = Response(404, b"synthetic-response-secret")
                if repository_status != 200:
                    transport.responses[REPOSITORY_URL] = Response(repository_status)
                result = self.updater(transport).check(TOKEN)
                self.assertEqual(result["status"], expected)
                self.assert_safe(result)

    def test_access_and_network_failures_never_claim_latest_or_echo_details(self):
        for response in (Response(401, b"synthetic-response-secret"),
                         Response(403, b"synthetic-response-secret"),
                         Response(500, b"synthetic-response-secret"),
                         OSError(TOKEN + " " + SIGNED_URL)):
            with self.subTest(response=type(response).__name__):
                transport = Transport({LATEST_URL: response})
                result = self.updater(transport).check(TOKEN)
                self.assertIn(result["status"], ("access_required", "unavailable"))
                self.assert_safe(result)

    def test_invalid_token_header_is_rejected_before_transport(self):
        transport = Transport({})
        result = self.updater(transport).check(TOKEN + "\r\nX-Leak: yes")
        self.assertEqual(result["status"], "unavailable")
        self.assertFalse(transport.calls)
        self.assert_safe(result)

    def test_noncanonical_and_nonstable_release_metadata_cannot_offer_update(self):
        modifications = [{"tag_name": "v0.4.0-rc.1"}, {"tag_name": "0.4.0"},
                         {"tag_name": "v00.4.0"}, {"draft": True}, {"prerelease": True},
                         {"target_commitish": "main"}, {"target_commitish": "a" * 39},
                         {"html_url": "https://github.com/attacker/project/releases/tag/v0.4.0"},
                         {"url": "https://api.github.com/repos/attacker/project/releases/123"}]
        for modification in modifications:
            with self.subTest(modification=modification):
                payload = archive_bytes()
                metadata = release_metadata(payload)
                metadata.update(modification)
                result = self.updater(make_transport(payload, metadata)).check(TOKEN)
                self.assertEqual(result["status"], "unavailable")
                self.assert_safe(result)

    def test_asset_requires_fixed_name_authenticated_digest_and_canonical_urls(self):
        modifications = [{"digest": None}, {"digest": "sha256:" + "x" * 64},
                         {"digest": "sha512:" + "a" * 64}, {"name": "artifact.zip"},
                         {"state": "new"}, {"size": -1}, {"id": True},
                         {"url": "https://api.github.com/repos/attacker/project/releases/assets/456"},
                         {"url": "http://api.github.com/repos/ThaoKeeton/upcam-task-digest/releases/assets/456"},
                         {"url": ASSET_URL + "?token=" + TOKEN},
                         {"browser_download_url": "https://github.com/attacker/project/actions/artifacts/456"}]
        for modification in modifications:
            with self.subTest(modification=list(modification)):
                payload = archive_bytes()
                metadata = release_metadata(payload)
                metadata["assets"][0].update(modification)
                result = self.updater(make_transport(payload, metadata)).check(TOKEN)
                self.assertEqual(result["status"], "unavailable")
                self.assert_safe(result)

    def test_duplicate_matching_assets_are_ambiguous_and_blocked(self):
        payload = archive_bytes()
        metadata = release_metadata(payload)
        metadata["assets"].append(deepcopy(metadata["assets"][0]))
        self.assertEqual(self.updater(make_transport(payload, metadata)).check(TOKEN)["status"],
                         "unavailable")

    def test_metadata_size_is_bounded_and_response_is_closed(self):
        response = Response(chunks=[b"{" * 80, b"x" * 80])
        with patch.object(self.updates, "MAX_METADATA_BYTES", 100):
            result = self.updater(Transport({LATEST_URL: response})).check(TOKEN)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(response.closed)

    def test_slow_metadata_and_download_streams_obey_total_time_budget(self):
        for kind in ("metadata", "download"):
            with self.subTest(kind=kind):
                clock = [0.0]
                payload = archive_bytes()
                metadata = release_metadata(payload)
                body = json.dumps(metadata).encode() if kind == "metadata" else payload

                def slow_chunks(_chunk_size):
                    yield body[:10]
                    clock[0] = 301.0
                    yield body[10:]

                response = Response()
                response.iter_bytes = slow_chunks
                transport = make_transport(payload, metadata)
                transport.responses[LATEST_URL if kind == "metadata" else ASSET_URL] = response
                with patch.object(self.updates, "monotonic", side_effect=lambda: clock[0], create=True):
                    if kind == "metadata":
                        result = self.updater(transport).check(TOKEN)
                        self.assertEqual(result["status"], "unavailable")
                        self.assertEqual(result["code"], "operation_timed_out")
                    else:
                        self.assert_rejected(lambda: self.updater(transport).download(TOKEN),
                                             code="operation_timed_out")
                self.assertTrue(response.closed)

    def test_metadata_redirect_is_not_followed_and_never_forwards_authentication(self):
        response = Response(302, headers={"Location": SIGNED_URL})
        transport = Transport({LATEST_URL: response})
        result = self.updater(transport).check(TOKEN)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(response.closed)
        self.assert_safe(result)

    def test_verified_download_is_staged_side_by_side_under_user_data(self):
        current = self.current_executable()
        result = self.updater().download(TOKEN)
        self.assertEqual(result["status"], "downloaded")
        stage = Path(result["directory"])
        self.assertTrue(stage.is_relative_to(self.data))
        self.assertEqual((stage / "UPCAMTaskDigest.exe").read_bytes(), b"synthetic-app-executable")
        self.assertEqual(result["ruleset_revision"], "2026-11-06.1")
        self.assertTrue(Path(result["archive"]).is_file())
        self.assertEqual(current.read_bytes(), b"old application stays untouched")
        self.assert_safe(result)

    def test_download_does_not_fetch_assets_for_equal_or_older_versions(self):
        transport = make_transport()
        result = self.updater(transport, current="0.4.0").download(TOKEN)
        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual([item[0] for item in transport.calls], [LATEST_URL])
        self.assertFalse(self.data.exists())

    def test_asset_redirect_strips_token_and_accept_headers(self):
        payload = archive_bytes()
        transport = make_transport(payload, asset_response=Response(302, headers={"Location": SIGNED_URL}))
        transport.responses[SIGNED_URL] = Response(body=payload)
        result = self.updater(transport).download(TOKEN)
        self.assertEqual(result["status"], "downloaded")
        asset_call, redirected_call = transport.calls[-2:]
        self.assertEqual(asset_call[1]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(asset_call[1]["Accept"], "application/octet-stream")
        self.assertNotIn("Authorization", redirected_call[1])
        self.assert_safe(result)

    def test_untrusted_redirect_schemes_hosts_paths_and_credentials_are_blocked(self):
        urls = ["http://release-assets.githubusercontent.com/github-production-release-asset/123/a",
                "https://release-assets.githubusercontent.com.evil.test/github-production-release-asset/123/a",
                "https://user:password@release-assets.githubusercontent.com/github-production-release-asset/123/a",
                "https://release-assets.githubusercontent.com:444/github-production-release-asset/123/a",
                "https://release-assets.githubusercontent.com/other/123/a",
                "https://release-assets.githubusercontent.com/github-production-release-asset/123/../a",
                "https://release-assets.githubusercontent.com/github-production-release-asset/123/%2e%2e/a",
                "https://github.com/attacker/project/actions/artifacts/42",
                "https://api.github.com/repos/attacker/project/releases/assets/456",
                SIGNED_URL + "#fragment", "//release-assets.githubusercontent.com/github-production-release-asset/123/a"]
        for url in urls:
            with self.subTest(url=url.split("?")[0]):
                transport = make_transport(asset_response=Response(302, headers={"Location": url}))
                self.assert_rejected(lambda: self.updater(transport).download(TOKEN))
                self.assertEqual(len(transport.calls), 2)

    def test_redirect_loops_and_error_bodies_are_bounded_and_redacted(self):
        for response in (Response(302, headers={"Location": SIGNED_URL}),
                         Response(500, b"synthetic-response-secret")):
            with self.subTest(status=response.status):
                transport = make_transport(asset_response=Response(302, headers={"Location": SIGNED_URL}))
                transport.responses[SIGNED_URL] = response
                self.assert_rejected(lambda: self.updater(transport).download(TOKEN))
                self.assertLessEqual(len(transport.calls), 6)

    def test_archive_size_and_actual_stream_size_are_bounded(self):
        payload = archive_bytes()
        with patch.object(self.updates, "MAX_DOWNLOAD_BYTES", len(payload) - 1):
            self.assertEqual(self.updater(make_transport(payload)).check(TOKEN)["status"], "unavailable")
        for response in (Response(body=payload + b"trailer"), Response(body=payload[:-1]),
                         Response(body=payload, headers={"Content-Length": str(len(payload) + 1)})):
            with self.subTest(size=len(response.body)):
                self.assert_rejected(lambda: self.updater(make_transport(payload, asset_response=response)).download(TOKEN))
        self.assertFalse(list(self.data.rglob("*.zip")))

    def test_archive_digest_mismatch_and_corruption_cannot_create_application(self):
        payload = archive_bytes()
        self.assert_rejected(lambda: self.updater(make_transport(
            payload, asset_response=Response(body=b"X" + payload[1:]))).download(TOKEN), code="digest_mismatch")
        corrupt = b"not a zip archive"
        self.assert_rejected(lambda: self.updater(make_transport(corrupt)).download(TOKEN))
        self.assertFalse(list(self.data.rglob("UPCAMTaskDigest.exe")))

    def test_windows_unsafe_names_are_rejected_before_extraction(self):
        names = ["../escape.exe", "/absolute.exe", "C:/escape.exe", "folder\\escape.exe",
                 "worker/../../escape.exe", "file:stream", "worker/CON.txt", "NUL", "AUX.dll",
                 "COM1.ini", "lpt9", "COM¹.txt", "CONIN$", "CONOUT$", "trailing. ",
                 "worker /file", "file.", "folder//file", "folder/./file", "control\x01.exe",
                 "wild*.exe", "quest?.exe", "quote\".exe", "pipe|.exe", "less<.exe"]
        for name in names:
            with self.subTest(name=repr(name)):
                payload = archive_bytes(extra=[(name, b"malicious")])
                self.assert_rejected(lambda: self.updater(make_transport(payload)).download(TOKEN),
                                     code="unsafe_archive")
        self.assertFalse((self.root / "escape.exe").exists())

    def test_case_aliases_file_directory_collisions_and_symlinks_are_rejected(self):
        symlink = zipfile.ZipInfo("worker/link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        examples = [[("upcamtaskdigest.EXE", b"overwrite")],
                    [("UPCAMTaskDıgest.exe", b"Windows Unicode case alias")],
                    [("worker", b"file conflicts with directory")],
                    [("Worker/extra.dll", b"directory case alias")],
                    [(symlink, b"../../outside")]]
        for extra in examples:
            with self.subTest(extra=repr(extra)):
                payload = archive_bytes(extra=extra)
                self.assert_rejected(lambda: self.updater(make_transport(payload)).download(TOKEN),
                                     code="unsafe_archive")

    def test_reserved_names_with_spaces_before_extensions_are_rejected(self):
        for name in ("CON .txt", "aux .dll", "worker/LPT1 .log"):
            with self.subTest(name=name):
                payload = archive_bytes(extra=[(name, b"reserved alias")])
                self.assert_rejected(lambda: self.updater(make_transport(payload)).download(TOKEN),
                                     code="unsafe_archive")

    def test_zip_central_directory_is_bounded_before_allocating_entries(self):
        payload = archive_bytes()
        end = payload.rfind(b"PK\x05\x06")
        too_many_entries = bytearray(payload)
        struct.pack_into("<HH", too_many_entries, end + 8, 65534, 65534)
        oversized_directory = bytearray(payload)
        struct.pack_into("<I", oversized_directory, end + 12, 64 * 1024 * 1024)
        for malicious in (bytes(too_many_entries), bytes(oversized_directory)):
            with self.subTest(size=len(malicious)):
                self.assert_rejected(lambda: self.updater(make_transport(malicious)).download(TOKEN),
                                     code="archive_too_large")

    def test_false_small_entry_count_cannot_bypass_preallocation_limit(self):
        payload = bytearray(archive_bytes())
        end = payload.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", payload, end + 8, 1, 1)
        malicious = bytes(payload)
        with patch.object(self.updates, "MAX_ARCHIVE_FILES", 3), patch.object(
                zipfile, "ZipFile", side_effect=AssertionError("unsafe allocation must not happen")):
            self.assert_rejected(lambda: self.updater(make_transport(malicious)).download(TOKEN),
                                 code="archive_too_large")

    def test_extraction_checks_total_budget_between_archive_reads(self):
        clock = [0.0]
        original_read = zipfile.ZipExtFile.read
        reads = []

        def slow_read(source, size=-1):
            value = original_read(source, size)
            reads.append(len(value))
            clock[0] = 301.0
            return value

        transport = make_transport()
        with patch.object(self.updates, "monotonic", side_effect=lambda: clock[0]), patch.object(
                zipfile.ZipExtFile, "read", slow_read):
            self.assert_rejected(lambda: self.updater(transport).download(TOKEN),
                                 code="operation_timed_out")
        self.assertEqual(len(reads), 1)
        self.assertFalse(list(self.data.rglob("UPCAMTaskDigest.exe")))

    def test_zip_entry_count_total_size_member_size_and_ratio_are_bounded(self):
        examples = [("MAX_ARCHIVE_FILES", 3, archive_bytes()),
                    ("MAX_UNCOMPRESSED_BYTES", 10, archive_bytes()),
                    ("MAX_MEMBER_BYTES", 10, archive_bytes()),
                    ("MAX_COMPRESSION_RATIO", 2, archive_bytes(extra=[("bomb.txt", b"A" * 100000)]))]
        for limit, value, payload in examples:
            with self.subTest(limit=limit), patch.object(self.updates, limit, value):
                self.assert_rejected(lambda: self.updater(make_transport(payload)).download(TOKEN))

    def test_missing_required_application_files_are_rejected(self):
        for name in ("UPCAMTaskDigest.exe", "UPCAMTaskDigest.dll", "UPCAMTaskDigest.pri",
                     "coreclr.dll", "hostfxr.dll", "worker/UPCAMWorker.exe",
                     "Assets/Teambition.ico", "worker/_internal/templates/daily_digest.json", "build-info.json"):
            with self.subTest(name=name):
                payload = archive_bytes(omit=[name])
                self.assert_rejected(lambda: self.updater(make_transport(payload)).download(TOKEN),
                                     code="invalid_package")

    def test_build_metadata_must_match_release_commit_version_rules_and_executable_hashes(self):
        changes = [{"applicationVersion": "0.4.1"}, {"sourceCommit": "b" * 40},
                   {"rulesetRevision": ""}, {"rulesetRevision": "../../evil"},
                   {"applicationSha256": "b" * 64}, {"workerSha256": "b" * 64},
                   {"applicationSha256": None}]
        for modification in changes:
            with self.subTest(modification=modification):
                payload = archive_bytes(manifest_changes=modification)
                self.assert_rejected(lambda: self.updater(make_transport(payload)).download(TOKEN),
                                     code="build_mismatch")

    def test_prepare_requires_explicit_download_and_preserves_current_application(self):
        current = self.current_executable()
        self.assert_rejected(lambda: self.updater().prepare_install(TOKEN, current_executable=current),
                             code="not_downloaded")
        self.assertEqual(current.read_bytes(), b"old application stays untouched")

    def test_prepare_revalidates_all_staged_files_and_retains_old_installation(self):
        current = self.current_executable()
        updater = self.updater()
        downloaded = updater.download(TOKEN)
        result = updater.prepare_install(TOKEN, current_executable=current)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["executable"], str(Path(downloaded["directory"]) / "UPCAMTaskDigest.exe"))
        self.assertEqual(result["previous_executable"], str(current))
        self.assertEqual(result["previous_directory"], str(current.parent))
        self.assertTrue(result["previous_preserved"])
        self.assertEqual(current.read_bytes(), b"old application stays untouched")
        self.assert_safe(result)

    def test_prepare_detects_archive_tampering(self):
        updater = self.updater()
        result = updater.download(TOKEN)
        Path(result["archive"]).write_bytes(b"corrupt after download")
        self.assert_rejected(lambda: updater.prepare_install(TOKEN, current_executable=self.current_executable()),
                             code="digest_mismatch")

    def test_prepare_detects_staged_dll_manifest_missing_files_and_extra_files(self):
        for change in ("dll", "manifest", "missing", "extra"):
            with self.subTest(change=change), tempfile.TemporaryDirectory(dir=self.root) as directory:
                updater = self.updates.GitHubUpdates(Path(directory), current_version="0.3.10", transport=make_transport())
                result = updater.download(TOKEN)
                stage = Path(result["directory"])
                if change == "dll":
                    (stage / "hostfxr.dll").write_bytes(b"tampered shared library")
                elif change == "manifest":
                    (stage / "build-info.json").write_text("{}")
                elif change == "missing":
                    (stage / "UPCAMTaskDigest.pri").unlink()
                else:
                    (stage / "malicious.dll").write_bytes(b"added shared library")
                self.assert_rejected(lambda: updater.prepare_install(TOKEN, current_executable=self.current_executable()),
                                     code="staged_tampered")

    def test_prepare_reauthenticates_release_and_does_not_trust_local_receipts(self):
        transport = make_transport()
        updater = self.updater(transport)
        updater.download(TOKEN)
        transport.responses[LATEST_URL] = Response(401, b"synthetic-response-secret")
        result = updater.prepare_install(TOKEN, current_executable=self.current_executable())
        self.assertEqual(result["status"], "access_required")
        self.assert_safe(result)

    def test_prepare_rejects_invalid_or_same_current_executable_paths(self):
        updater = self.updater()
        result = updater.download(TOKEN)
        targets = [self.root / "missing.exe", Path("relative/UPCAMTaskDigest.exe"),
                   Path(result["directory"]) / "UPCAMTaskDigest.exe"]
        wrong = self.root / "wrong-name.exe"
        wrong.write_bytes(b"not current app")
        targets.append(wrong)
        for target in targets:
            with self.subTest(target=str(target)):
                self.assert_rejected(lambda: updater.prepare_install(TOKEN, current_executable=target),
                                     code="invalid_current_executable")

    def test_unwritable_staging_directory_blocks_prepare(self):
        updater = self.updater()
        result = updater.download(TOKEN)
        stage = Path(result["directory"])
        old_mode = stage.stat().st_mode
        try:
            stage.chmod(0o555)
            self.assert_rejected(lambda: updater.prepare_install(TOKEN, current_executable=self.current_executable()),
                                 code="target_unwritable")
        finally:
            stage.chmod(old_mode)

    def test_verify_installed_checks_local_integrity_without_authentication_or_network(self):
        result = self.updater().download(TOKEN)
        transport = Transport({})
        installed = self.updater(transport, current="0.4.0").verify_installed(Path(result["executable"]))
        self.assertEqual(installed["status"], "installed")
        self.assertEqual(installed["version"], "0.4.0")
        self.assertEqual(installed["source_commit"], COMMIT)
        self.assertEqual(installed["verification"], "local_integrity")
        self.assertFalse(transport.calls)
        self.assert_safe(installed)

    def test_verify_installed_rejects_outside_path_wrong_version_or_tampered_tree(self):
        result = self.updater().download(TOKEN)
        executable = Path(result["executable"])
        self.assert_rejected(lambda: self.updater(current="0.4.0").verify_installed(self.current_executable()),
                             code="invalid_current_executable")
        self.assert_rejected(lambda: self.updater().verify_installed(executable),
                             code="invalid_current_executable")
        (executable.parent / "UPCAMTaskDigest.dll").write_bytes(b"changed after application starts")
        self.assert_rejected(lambda: self.updater(current="0.4.0").verify_installed(executable),
                             code="staged_tampered")

    def test_verify_installed_detects_modified_archive(self):
        result = self.updater().download(TOKEN)
        Path(result["archive"]).write_bytes(b"changed saved ZIP")
        self.assert_rejected(lambda: self.updater(current="0.4.0").verify_installed(Path(result["executable"])),
                             code="digest_mismatch")

    @unittest.skipUnless(hasattr(os, "symlink"), "filesystem has no symlinks")
    def test_symlinked_data_stage_and_current_paths_are_blocked(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            self.data.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable to this account")
        self.assert_rejected(lambda: self.updater().download(TOKEN), code="unsafe_path")
        self.data.unlink()
        updater = self.updater()
        result = updater.download(TOKEN)
        file_path = Path(result["directory"]) / "hostfxr.dll"
        file_path.unlink()
        file_path.symlink_to(self.current_executable())
        self.assert_rejected(lambda: updater.prepare_install(TOKEN, current_executable=self.current_executable()))
        current_link = self.root / "UPCAMTaskDigest.exe"
        current_link.symlink_to(self.current_executable())
        self.assert_rejected(lambda: updater.prepare_install(TOKEN, current_executable=current_link),
                             code="invalid_current_executable")


if __name__ == "__main__":
    unittest.main()
