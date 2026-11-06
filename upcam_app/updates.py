"""Verified stable Releases for the fixed private UPCAM repository.

The transport never follows redirects itself. Its ``request(url, *, headers,
timeout)`` response supplies ``status``, ``headers``, ``iter_bytes(chunk_size)``
and ``close()``. Tests inject this boundary; no business credentials are read.
Downloaded applications are staged under data_dir and are never executed here.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
from time import monotonic
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


REPOSITORY = "ThaoKeeton/upcam-task-digest"
REPOSITORY_API = "https://api.github.com/repos/" + REPOSITORY
LATEST_RELEASE_API = REPOSITORY_API + "/releases/latest"
ASSET_NAME = "UPCAMTaskDigest-WinUI3-win-x64.zip"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1536 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
MAX_COMPRESSION_RATIO = 200
MAX_MANIFEST_BYTES = 64 * 1024
MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_OPERATION_SECONDS = 300
_CHUNK_BYTES = 64 * 1024
_TIMEOUT_SECONDS = 30
_MAX_REDIRECTS = 3
_APP_EXE = "UPCAMTaskDigest.exe"
_WORKER_EXE = "worker/UPCAMWorker.exe"
_REQUIRED_FILES = frozenset({
    _APP_EXE, "UPCAMTaskDigest.dll", "UPCAMTaskDigest.pri", "coreclr.dll", "hostfxr.dll",
    "Assets/Teambition.ico", "Assets/Teambition.png", _WORKER_EXE,
    "worker/_internal/templates/daily_digest.json", "worker/_internal/examples/digest.json",
    "build-info.json",
})
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_RULESET = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}(?:\.[0-9]+)?\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]|CONIN\$|CONOUT\$|CLOCK\$)\Z", re.I)
_MESSAGES = {
    "access_required": "私有更新源暂不可访问；请配置具有本仓库只读权限的 GitHub 令牌。",
    "no_release": "仓库尚无可用的稳定发布；当前无法通过此渠道更新。",
    "unavailable": "暂时无法核验稳定更新；请稍后重试。",
    "available": "发现已核验来源的稳定版本，可手动下载。",
    "up_to_date": "已核验可访问的稳定发布，当前版本无需更新。",
    "downloaded": "完整更新包已校验并保存，可在退出当前应用后切换。",
    "ready": "更新已重新校验；旧应用目录已保留，等待当前应用安全退出。",
    "invalid_current_version": "当前应用版本信息无效，无法安全比较更新。",
    "invalid_token": "GitHub 令牌格式无效，请检查本地更新设置。",
    "network_error": "无法连接更新源；未显示响应内容或凭据，请稍后重试。",
    "invalid_response": "更新源响应格式或大小无效，已停止更新。",
    "invalid_release": "稳定发布的版本、提交或来源信息不完整，已停止更新。",
    "invalid_asset": "稳定发布缺少符合要求的完整应用 ZIP 或 SHA-256 摘要。",
    "unsafe_redirect": "更新下载发生不受信任的重定向，已停止更新。",
    "download_failed": "更新包未能完整下载，请稍后重试。",
    "download_too_large": "更新包超过允许的下载大小，已停止更新。",
    "digest_mismatch": "更新包 SHA-256 与 GitHub 发布摘要不符，已停止更新。",
    "unsafe_archive": "更新 ZIP 包含不安全的 Windows 路径或特殊文件，已拒绝解压。",
    "archive_too_large": "更新 ZIP 的文件数量、展开大小或压缩比例超出安全限制。",
    "invalid_package": "更新包损坏或缺少完整应用所需文件，已停止更新。",
    "build_mismatch": "应用构建信息与稳定发布或可执行文件不匹配，已停止更新。",
    "not_downloaded": "请先完整下载并校验此稳定版本，再准备安装。",
    "staged_tampered": "已保存的更新目录发生变化，已停止切换应用。",
    "unsafe_path": "本地更新目录包含链接或不安全路径，已停止更新。",
    "target_unwritable": "当前用户无法写入更新目录；旧应用已保留。",
    "invalid_current_executable": "当前应用路径无效或与更新目标冲突，已停止切换。",
    "update_failed": "更新未完成；旧应用已保留，请检查本地更新设置后重试。",
    "operation_timed_out": "更新核验或下载超过允许时限，已停止操作；请稍后重试。",
    "installed": "当前更新目录的本地完整性已核验。",
}


class UpdateError(ValueError):
    """Only fixed codes and safe messages may cross the worker boundary."""

    def __init__(self, code: str):
        self.code = code if code in _MESSAGES else "update_failed"
        self.message = _MESSAGES[self.code]
        super().__init__(self.message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class _HttpResponse:
    def __init__(self, response):
        self._response = response
        self.status = response.code
        self.headers = response.headers

    def iter_bytes(self, chunk_size):
        # read1 returns after one underlying read, so a trickle of bytes cannot
        # keep a buffered read(chunk_size) alive beyond our operation budget.
        while chunk := self._response.read1(chunk_size):
            yield chunk

    def close(self):
        self._response.close()


class _UrllibTransport:
    def __init__(self):
        self._opener = build_opener(_NoRedirect())

    def request(self, url, *, headers, timeout):
        request = Request(url, headers=headers, method="GET")
        try:
            response = self._opener.open(request, timeout=timeout)
        except HTTPError as response:
            return _HttpResponse(response)
        return _HttpResponse(response)


@dataclass(frozen=True)
class _Release:
    version: str
    source_commit: str
    release_url: str
    asset_url: str
    size: int
    sha256: str

    def public(self, status):
        return {"status": status, "message": _MESSAGES[status], "version": self.version,
                "release_url": self.release_url, "source_commit": self.source_commit,
                "sha256": self.sha256}


@dataclass
class _Package:
    version: str
    source_commit: str
    ruleset_revision: str
    files: dict[str, tuple[int, str]]
    directories: set[str]


class _Deadline:
    def __init__(self):
        self.expires = monotonic() + MAX_OPERATION_SECONDS

    def remaining(self):
        remaining = self.expires - monotonic()
        if remaining <= 0:
            raise UpdateError("operation_timed_out")
        return remaining

    def check(self):
        self.remaining()


def _state(status, *, code=None):
    result = {"status": status, "message": _MESSAGES[code or status]}
    if code:
        result["code"] = code
    return result


def _version(value):
    if not isinstance(value, str) or len(value) > 50 or not _VERSION.fullmatch(value):
        raise UpdateError("invalid_current_version")
    return tuple(int(item) for item in value.split("."))


def _positive_int(value):
    return type(value) is int and value > 0


def _decode_json(payload, code):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise UpdateError(code) from None


def _header(response, name):
    for key, value in response.headers.items():
        if str(key).lower() == name.lower():
            return str(value)
    return None


def _canonical_url(value, expected):
    # Equality to our constructed URL rules out credentials, ports, queries,
    # fragments, alternate encodings, other repositories and Actions artifacts.
    return isinstance(value, str) and value == expected


def _asset_redirect(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return False
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or parts.netloc not in {
                "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
                or parts.fragment or parts.username or parts.password or parts.port):
            return False
        if not re.fullmatch(r"/github-production-release-asset(?:-[0-9a-f]+)?/[0-9]+/[A-Za-z0-9._~-]+", parts.path):
            return False
        return parts.path.rsplit("/", 1)[-1] not in {".", ".."}
    except ValueError:
        return False


def _parse_release(metadata):
    try:
        tag = metadata["tag_name"]
        if not isinstance(tag, str) or not tag.startswith("v"):
            raise ValueError
        version = tag[1:]
        _version(version)
        release_id = metadata["id"]
        commit = metadata["target_commitish"]
        release_url = "https://github.com/" + REPOSITORY + "/releases/tag/" + tag
        if (not _positive_int(release_id) or metadata.get("draft") is not False
                or metadata.get("prerelease") is not False
                or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                or not _canonical_url(metadata.get("url"), REPOSITORY_API + "/releases/" + str(release_id))
                or not _canonical_url(metadata.get("html_url"), release_url)):
            raise ValueError
    except (KeyError, ValueError, TypeError, UpdateError):
        raise UpdateError("invalid_release") from None
    assets = metadata.get("assets")
    if not isinstance(assets, list) or len(assets) > 100:
        raise UpdateError("invalid_asset")
    matching = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == ASSET_NAME]
    if len(matching) != 1:
        raise UpdateError("invalid_asset")
    asset = matching[0]
    asset_id, size, digest = asset.get("id"), asset.get("size"), asset.get("digest")
    if (not _positive_int(asset_id) or not _positive_int(size) or size > MAX_DOWNLOAD_BYTES
            or asset.get("state") != "uploaded" or not isinstance(digest, str)
            or not digest.startswith("sha256:") or not _SHA256.fullmatch(digest[7:])
            or not _canonical_url(asset.get("url"), REPOSITORY_API + "/releases/assets/" + str(asset_id))
            or not _canonical_url(asset.get("browser_download_url"), "https://github.com/" + REPOSITORY
                                  + "/releases/download/" + tag + "/" + ASSET_NAME)):
        raise UpdateError("invalid_asset")
    return _Release(version, commit, release_url, asset["url"], size, digest[7:].lower())


def _assert_safe_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise UpdateError("unsafe_path")
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UpdateError("unsafe_path")


def _regular_file(path, code):
    try:
        _assert_safe_path(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise UpdateError(code)
        return info
    except (OSError, UpdateError):
        raise UpdateError(code) from None


def _hash_file(path, *, maximum, code, deadline):
    deadline.check()
    _regular_file(path, code)
    digest, size = hashlib.sha256(), 0
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise UpdateError(code)
            while chunk := stream.read(_CHUNK_BYTES):
                deadline.check()
                size += len(chunk)
                if size > maximum:
                    raise UpdateError(code)
                digest.update(chunk)
    except OSError:
        raise UpdateError(code) from None
    deadline.check()
    return size, digest.hexdigest()


def _writable_directory(path, *, create=False):
    _assert_safe_path(path)
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        _assert_safe_path(path)
        info = path.stat()
        if (not stat.S_ISDIR(info.st_mode) or not info.st_mode & 0o222
                or not os.access(path, os.W_OK)):
            raise UpdateError("target_unwritable")
        descriptor, probe = tempfile.mkstemp(prefix=".write-check-", dir=path)
        os.close(descriptor)
        os.unlink(probe)
    except OSError:
        raise UpdateError("target_unwritable") from None


def _member_name(info):
    name = info.filename
    if (name != info.orig_filename or not name or len(name) > 240 or "\\" in name
            or name.startswith("/") or unicodedata.normalize("NFC", name) != name):
        raise UpdateError("unsafe_archive")
    is_directory = info.is_dir()
    parts = (name[:-1] if is_directory else name).split("/")
    for part in parts:
        if (not part or part in {".", ".."} or len(part) > 200 or part[-1] in " ."
                or any(ord(c) < 32 or ord(c) == 127 or c in '<>:"|?*' for c in part)
                or _RESERVED.fullmatch(part.split(".", 1)[0].rstrip(" ."))):
            raise UpdateError("unsafe_archive")
    mode = info.external_attr >> 16
    if (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
            or info.external_attr & 0x400 or (stat.S_ISDIR(mode) and not is_directory)
            or (stat.S_ISREG(mode) and is_directory)):
        raise UpdateError("unsafe_archive")
    return "/".join(parts), is_directory


def _bounded_zip_directory(path, deadline):
    """Reject huge/ZIP64 central directories before ZipFile allocates entries."""
    deadline.check()
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - (65535 + 22)))
            tail = stream.read(65535 + 22)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise UpdateError("invalid_package")
        _, disk, central_disk, disk_entries, entries, central_size, central_offset, comment_size = struct.unpack(
            "<4s4H2IH", tail[offset:offset + 22])
        if (entries > MAX_ARCHIVE_FILES or central_size > MAX_CENTRAL_DIRECTORY_BYTES
                or entries == 65535 or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF):
            raise UpdateError("archive_too_large")
        end_offset = size - len(tail) + offset
        if (disk != 0 or central_disk != 0 or disk_entries != entries
                or len(tail) - offset != 22 + comment_size or entries == 0
                or central_offset + central_size != end_offset):
            raise UpdateError("invalid_package")
        with path.open("rb") as stream:
            stream.seek(central_offset)
            central = stream.read(central_size)
        if len(central) != central_size:
            raise UpdateError("invalid_package")
        position, actual_entries = 0, 0
        while position < len(central):
            deadline.check()
            if position + 46 > len(central) or central[position:position + 4] != b"PK\x01\x02":
                raise UpdateError("invalid_package")
            name_size, extra_size, file_comment_size, start_disk = struct.unpack_from("<4H", central, position + 28)
            actual_entries += 1
            if actual_entries > MAX_ARCHIVE_FILES:
                raise UpdateError("archive_too_large")
            if start_disk != 0:
                raise UpdateError("invalid_package")
            position += 46 + name_size + extra_size + file_comment_size
            if position > len(central):
                raise UpdateError("invalid_package")
        if actual_entries != entries:
            raise UpdateError("invalid_package")
        deadline.check()
        return entries
    except (OSError, struct.error):
        raise UpdateError("invalid_package") from None


def _archive_inventory(archive, deadline, expected_entries):
    deadline.check()
    infos = archive.infolist()
    if not infos or len(infos) > MAX_ARCHIVE_FILES or len(infos) != expected_entries:
        raise UpdateError("archive_too_large")
    entries, keys, file_keys, directories, total = [], set(), set(), set(), 0
    for info in infos:
        deadline.check()
        name, is_directory = _member_name(info)
        # NTFS case aliases include the dotless i, which casefold alone does
        # not merge. Uppercase then casefold conservatively covers both maps.
        key = name.upper().casefold()
        if key in keys:
            raise UpdateError("unsafe_archive")
        keys.add(key)
        if (info.flag_bits & 1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size < 0 or info.compress_size < 0
                or (is_directory and info.file_size != 0)):
            raise UpdateError("unsafe_archive")
        total += info.file_size
        if (info.file_size > MAX_MEMBER_BYTES or total > MAX_UNCOMPRESSED_BYTES
                or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO):
            raise UpdateError("archive_too_large")
        if is_directory:
            directories.add(name)
        else:
            file_keys.add(key)
        parents = name.split("/")[:-1]
        for index in range(1, len(parents) + 1):
            directories.add("/".join(parents[:index]))
        entries.append((info, name, is_directory))
    if file_keys & {name.upper().casefold() for name in directories}:
        raise UpdateError("unsafe_archive")
    directory_keys = {}
    for name in directories:
        previous = directory_keys.setdefault(name.upper().casefold(), name)
        if previous != name:
            raise UpdateError("unsafe_archive")
    files = {name for _, name, is_directory in entries if not is_directory}
    if not _REQUIRED_FILES.issubset(files):
        raise UpdateError("invalid_package")
    return entries, directories


def _scan_archive(path, release, *, destination=None, deadline):
    try:
        entry_count = _bounded_zip_directory(path, deadline)
        with zipfile.ZipFile(path) as archive:
            entries, directories = _archive_inventory(archive, deadline, entry_count)
            files, manifest_bytes = {}, bytearray()
            for info, name, is_directory in entries:
                deadline.check()
                output = destination / name if destination is not None else None
                if is_directory:
                    if output is not None:
                        output.mkdir(parents=True, exist_ok=True)
                    continue
                if name == "build-info.json" and info.file_size > MAX_MANIFEST_BYTES:
                    raise UpdateError("invalid_package")
                if output is not None:
                    output.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                target = output.open("xb") if output is not None else None
                try:
                    with archive.open(info) as source:
                        while chunk := source.read(_CHUNK_BYTES):
                            deadline.check()
                            size += len(chunk)
                            if size > info.file_size or size > MAX_MEMBER_BYTES:
                                raise UpdateError("archive_too_large")
                            digest.update(chunk)
                            if target is not None:
                                target.write(chunk)
                            if name == "build-info.json":
                                manifest_bytes.extend(chunk)
                finally:
                    if target is not None:
                        target.close()
                if size != info.file_size or size == 0 and name in _REQUIRED_FILES:
                    raise UpdateError("invalid_package")
                files[name] = (size, digest.hexdigest())
            manifest = _decode_json(bytes(manifest_bytes), "build_mismatch")
            rules, version, commit = (manifest.get("rulesetRevision"),
                                      manifest.get("applicationVersion"), manifest.get("sourceCommit"))
            try:
                _version(version)
            except UpdateError:
                raise UpdateError("build_mismatch") from None
            if (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                    or release is not None and (version != release.version or commit != release.source_commit)
                    or not isinstance(rules, str) or len(rules) > 40 or not _RULESET.fullmatch(rules)):
                raise UpdateError("build_mismatch")
            for field, name in (("applicationSha256", _APP_EXE), ("workerSha256", _WORKER_EXE)):
                expected = manifest.get(field)
                if not isinstance(expected, str) or not _SHA256.fullmatch(expected) or expected.lower() != files[name][1]:
                    raise UpdateError("build_mismatch")
            deadline.check()
            return _Package(version, commit, rules, files, directories)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, ValueError, OSError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError("invalid_package") from None


def _verify_tree(directory, package, deadline):
    try:
        _assert_safe_path(directory)
        if not directory.is_dir():
            raise UpdateError("not_downloaded")
        seen_files, seen_directories = set(), set()
        pending = [(directory, "")]
        while pending:
            deadline.check()
            parent, prefix = pending.pop()
            with os.scandir(parent) as entries:
                for entry in entries:
                    deadline.check()
                    name = prefix + entry.name
                    path = Path(entry.path)
                    if entry.is_symlink() or getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                        raise UpdateError("staged_tampered")
                    if entry.is_dir(follow_symlinks=False):
                        if name not in package.directories:
                            raise UpdateError("staged_tampered")
                        seen_directories.add(name)
                        pending.append((path, name + "/"))
                    else:
                        if name not in package.files:
                            raise UpdateError("staged_tampered")
                        if _hash_file(path, maximum=package.files[name][0], code="staged_tampered", deadline=deadline) != package.files[name]:
                            raise UpdateError("staged_tampered")
                        seen_files.add(name)
        if seen_files != set(package.files) or seen_directories != package.directories:
            raise UpdateError("staged_tampered")
    except OSError:
        raise UpdateError("staged_tampered") from None


class GitHubUpdates:
    """Check, download and prepare a per-user side-by-side stable application."""

    def __init__(self, data_dir: Path, *, current_version: str, transport=None):
        self.current_version = current_version
        self._current = _version(current_version)
        self.data_dir = Path(os.path.abspath(data_dir))
        self.transport = transport if transport is not None else _UrllibTransport()

    def _request(self, url, *, headers, deadline):
        try:
            response = self.transport.request(url, headers=headers, timeout=min(_TIMEOUT_SECONDS, deadline.remaining()))
            if type(response.status) is not int or not hasattr(response.headers, "items"):
                response.close()
                raise UpdateError("invalid_response")
            return response
        except UpdateError:
            raise
        except Exception:
            raise UpdateError("network_error") from None

    def _json_request(self, url, token, deadline):
        response = self._request(url, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "UPCAMTaskDigest-Updater",
        }, deadline=deadline)
        try:
            deadline.check()
            if response.status != 200:
                return response.status, None
            payload = bytearray()
            for chunk in response.iter_bytes(_CHUNK_BYTES):
                deadline.check()
                if not isinstance(chunk, bytes) or len(payload) + len(chunk) > MAX_METADATA_BYTES:
                    raise UpdateError("invalid_response")
                payload.extend(chunk)
            deadline.check()
            return 200, _decode_json(bytes(payload), "invalid_response")
        except UpdateError:
            raise
        except Exception:
            raise UpdateError("network_error") from None
        finally:
            response.close()

    def _release(self, token, deadline):
        deadline.check()
        if token == "":
            return None, _state("access_required")
        if (not isinstance(token, str) or not token or len(token) > 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise UpdateError("invalid_token")
        status, metadata = self._json_request(LATEST_RELEASE_API, token, deadline)
        if status in {401, 403}:
            return None, _state("access_required")
        if status == 404:
            repository_status, repository = self._json_request(REPOSITORY_API, token, deadline)
            if repository_status in {401, 403, 404}:
                return None, _state("access_required")
            if repository_status == 200 and repository.get("full_name") == REPOSITORY:
                return None, _state("no_release")
            return None, _state("unavailable")
        if status != 200:
            return None, _state("unavailable")
        return _parse_release(metadata), None

    def check(self, token="") -> dict:
        try:
            deadline = _Deadline()
            release, state = self._release(token, deadline)
            if state is not None:
                return state
            deadline.check()
            return release.public("available" if _version(release.version) > self._current else "up_to_date")
        except UpdateError as error:
            return _state("unavailable", code=error.code)
        except Exception:
            return _state("unavailable", code="update_failed")

    def _paths(self, release):
        updates = self.data_dir / "updates"
        archive = updates / "downloads" / (release.version + "-" + release.sha256 + ".zip")
        directory = updates / "versions" / (release.version + "-" + release.source_commit[:12] + "-" + release.sha256[:16])
        for path in (self.data_dir, updates, archive, directory):
            _assert_safe_path(path)
        return archive, directory

    def _download_to(self, destination, release, token, deadline):
        url = release.asset_url
        headers = {"Authorization": "Bearer " + token, "Accept": "application/octet-stream",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "UPCAMTaskDigest-Updater"}
        for redirects in range(_MAX_REDIRECTS + 1):
            response = self._request(url, headers=headers, deadline=deadline)
            try:
                deadline.check()
                if response.status in {301, 302, 303, 307, 308}:
                    target = _header(response, "Location")
                    if redirects == _MAX_REDIRECTS or not _asset_redirect(target):
                        raise UpdateError("unsafe_redirect")
                    url = target
                    headers = {"Accept": "application/octet-stream", "User-Agent": "UPCAMTaskDigest-Updater"}
                    continue
                if response.status != 200:
                    raise UpdateError("download_failed")
                length = _header(response, "Content-Length")
                if length is not None and (not length.isdecimal() or len(length) > 20 or int(length) != release.size):
                    raise UpdateError("download_failed")
                digest, size = hashlib.sha256(), 0
                with destination.open("wb") as stream:
                    for chunk in response.iter_bytes(_CHUNK_BYTES):
                        deadline.check()
                        if not isinstance(chunk, bytes):
                            raise UpdateError("invalid_response")
                        size += len(chunk)
                        if size > release.size or size > MAX_DOWNLOAD_BYTES:
                            raise UpdateError("download_too_large")
                        stream.write(chunk)
                        digest.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                deadline.check()
                if size != release.size:
                    raise UpdateError("download_failed")
                if digest.hexdigest() != release.sha256:
                    raise UpdateError("digest_mismatch")
                return
            except UpdateError:
                raise
            except Exception:
                raise UpdateError("download_failed") from None
            finally:
                response.close()

    def download(self, token="") -> dict:
        temporary_archive = temporary_directory = None
        try:
            deadline = _Deadline()
            release, state = self._release(token, deadline)
            if state is not None:
                return state
            if _version(release.version) <= self._current:
                return release.public("up_to_date")
            archive, directory = self._paths(release)
            for parent in (self.data_dir, archive.parent.parent, archive.parent, directory.parent):
                _writable_directory(parent, create=True)
            descriptor, name = tempfile.mkstemp(prefix=".download-", suffix=".part", dir=archive.parent)
            os.close(descriptor)
            temporary_archive = Path(name)
            self._download_to(temporary_archive, release, token, deadline)
            if directory.exists():
                package = _scan_archive(temporary_archive, release, deadline=deadline)
                _verify_tree(directory, package, deadline)
            else:
                temporary_directory = Path(tempfile.mkdtemp(prefix=".stage-", dir=directory.parent))
                package = _scan_archive(temporary_archive, release, destination=temporary_directory, deadline=deadline)
                _verify_tree(temporary_directory, package, deadline)
            deadline.check()
            os.replace(temporary_archive, archive)
            temporary_archive = None
            if temporary_directory is not None:
                os.replace(temporary_directory, directory)
                temporary_directory = None
            result = release.public("downloaded")
            result.update(archive=str(archive), directory=str(directory),
                          executable=str(directory / _APP_EXE), ruleset_revision=package.ruleset_revision)
            return result
        except UpdateError:
            raise
        except OSError:
            raise UpdateError("target_unwritable") from None
        except Exception:
            raise UpdateError("update_failed") from None
        finally:
            if temporary_archive is not None:
                try:
                    temporary_archive.unlink(missing_ok=True)
                except OSError:
                    pass
            if temporary_directory is not None:
                shutil.rmtree(temporary_directory, ignore_errors=True)

    def prepare_install(self, token="", *, current_executable: Path) -> dict:
        try:
            deadline = _Deadline()
            release, state = self._release(token, deadline)
            if state is not None:
                return state
            if _version(release.version) <= self._current:
                return release.public("up_to_date")
            archive, directory = self._paths(release)
            current = Path(current_executable)
            if not current.is_absolute() or current.name != _APP_EXE:
                raise UpdateError("invalid_current_executable")
            if _regular_file(current, "invalid_current_executable").st_size == 0:
                raise UpdateError("invalid_current_executable")
            if current.parent == directory or current.is_relative_to(directory) or directory.is_relative_to(current.parent):
                raise UpdateError("invalid_current_executable")
            if not archive.exists() or not directory.exists():
                raise UpdateError("not_downloaded")
            size, digest = _hash_file(archive, maximum=MAX_DOWNLOAD_BYTES, code="digest_mismatch", deadline=deadline)
            if size != release.size or digest != release.sha256:
                raise UpdateError("digest_mismatch")
            package = _scan_archive(archive, release, deadline=deadline)
            for parent in (self.data_dir, archive.parent.parent, directory.parent, directory):
                _writable_directory(parent)
            _verify_tree(directory, package, deadline)
            deadline.check()
            result = release.public("ready")
            result.update(executable=str(directory / _APP_EXE), directory=str(directory),
                          ruleset_revision=package.ruleset_revision, previous_executable=str(current),
                          previous_directory=str(current.parent), previous_preserved=True)
            return result
        except UpdateError:
            raise
        except OSError:
            raise UpdateError("target_unwritable") from None
        except Exception:
            raise UpdateError("update_failed") from None

    def verify_installed(self, executable: Path) -> dict:
        """Verify the current staged tree offline after a successful startup.

        This detects local corruption against the saved ZIP. It intentionally
        makes no claim of authenticated release provenance; initial installation
        must use prepare_install with a fresh authenticated Release check.
        """
        try:
            deadline = _Deadline()
            current = Path(executable)
            versions = self.data_dir / "updates" / "versions"
            _assert_safe_path(versions)
            if (not current.is_absolute() or current.name != _APP_EXE
                    or current.parent.parent != versions):
                raise UpdateError("invalid_current_executable")
            _regular_file(current, "invalid_current_executable")
            match = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)-([0-9a-f]{12})-([0-9a-f]{16})", current.parent.name)
            if not match or match[1] != self.current_version:
                raise UpdateError("invalid_current_executable")
            downloads = versions.parent / "downloads"
            _assert_safe_path(downloads)
            candidates = []
            # Do not glob an attacker-controlled directory into an unbounded list.
            with os.scandir(downloads) as entries:
                count = 0
                for entry in entries:
                    deadline.check()
                    count += 1
                    if count > MAX_ARCHIVE_FILES:
                        raise UpdateError("archive_too_large")
                    archive_match = re.fullmatch(re.escape(self.current_version) + r"-([0-9a-f]{64})\.zip", entry.name)
                    if archive_match and archive_match[1].startswith(match[3]):
                        candidates.append((Path(entry.path), archive_match[1]))
                        if len(candidates) > 1:
                            raise UpdateError("invalid_current_executable")
            if not candidates:
                raise UpdateError("not_downloaded")
            archive, expected_digest = candidates[0]
            size, digest = _hash_file(archive, maximum=MAX_DOWNLOAD_BYTES, code="digest_mismatch", deadline=deadline)
            if size <= 0 or digest != expected_digest:
                raise UpdateError("digest_mismatch")
            package = _scan_archive(archive, None, deadline=deadline)
            if package.version != self.current_version or not package.source_commit.startswith(match[2]):
                raise UpdateError("invalid_current_executable")
            local_release = _Release(package.version, package.source_commit, "", "", size, digest)
            expected_archive, expected_directory = self._paths(local_release)
            if archive != expected_archive or current.parent != expected_directory:
                raise UpdateError("invalid_current_executable")
            _verify_tree(current.parent, package, deadline)
            deadline.check()
            return {"status": "installed", "message": _MESSAGES["installed"],
                    "verification": "local_integrity", "version": package.version,
                    "source_commit": package.source_commit, "ruleset_revision": package.ruleset_revision,
                    "executable": str(current), "directory": str(current.parent)}
        except UpdateError:
            raise
        except OSError:
            raise UpdateError("invalid_current_executable") from None
        except Exception:
            raise UpdateError("update_failed") from None
