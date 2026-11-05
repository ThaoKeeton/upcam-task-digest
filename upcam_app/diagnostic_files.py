"""Create a private diagnostic without following a changed destination path.

The caller validates the chosen location and captures its parent's identity before
collecting any project content. This module binds the write to that same directory
and never replaces an existing destination. It deliberately has no pathname-only
fallback when the required filesystem primitives are unavailable.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
from pathlib import Path
import re
import secrets
import stat


_SAVE_ERROR = "Could not save the diagnostic file safely; choose a new local JSON filename."


class DiagnosticFileError(OSError):
    """A destination cannot be written safely; contains no underlying OS details."""


def write_private_diagnostic(path: Path, data: bytes, *,
                             expected_parent_identity: tuple[int, int]) -> None:
    """Atomically create a new JSON file in the previously checked directory.

    POSIX operations stay relative to an open directory descriptor, including the
    no-replace hard-link commit. Windows holds directory handles which deny write
    and delete sharing throughout creation and rename, using a volume GUID path
    so a drive-letter change cannot redirect the write. Windows destinations must
    be local, ordinary drive paths; reparse points are refused.
    """
    try:
        path = Path(path)
        if (not isinstance(data, bytes) or not path.is_absolute()
                or path.suffix.casefold() != ".json" or "\x00" in str(path)
                or ".." in path.parts
                or not isinstance(expected_parent_identity, tuple)
                or len(expected_parent_identity) != 2
                or any(type(value) is not int or value < 0 for value in expected_parent_identity)):
            raise DiagnosticFileError(_SAVE_ERROR)
        if os.name == "posix":
            _write_posix(path, data, expected_parent_identity)
        elif os.name == "nt":
            _write_windows(path, data, expected_parent_identity)
        else:
            raise DiagnosticFileError(_SAVE_ERROR)
    except Exception:
        # OS exception strings can include a selected path or other private text.
        raise DiagnosticFileError(_SAVE_ERROR) from None


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _write_all(descriptor: int, data: bytes) -> None:
    pending = memoryview(data)
    while pending:
        written = os.write(descriptor, pending)
        if written <= 0:
            raise DiagnosticFileError(_SAVE_ERROR)
        pending = pending[written:]


def _write_posix(path: Path, data: bytes, expected_identity: tuple[int, int]) -> None:
    if (not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")
            or any(function not in os.supports_dir_fd for function in (os.open, os.link, os.unlink, os.stat))
            or os.link not in os.supports_follow_symlinks):
        raise DiagnosticFileError(_SAVE_ERROR)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0))
    descriptor = None
    temporary = None
    try:
        parent_info = os.fstat(directory)
        if not stat.S_ISDIR(parent_info.st_mode) or _identity(parent_info) != expected_identity:
            raise DiagnosticFileError(_SAVE_ERROR)
        # No pre-existing target, including dangling symlinks, may be replaced.
        # The link operation below is the authoritative, atomic existence check.
        for _attempt in range(10):
            candidate = ".upcam-diagnostic-" + secrets.token_hex(16) + ".tmp"
            try:
                descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                                     0o600, dir_fd=directory)
                temporary = candidate
                break
            except FileExistsError:
                continue
        if descriptor is None:
            raise DiagnosticFileError(_SAVE_ERROR)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        # Keep the file open until after commit. Refuse a substituted temp entry.
        file_identity = _identity(os.fstat(descriptor))
        if _identity(os.stat(temporary, dir_fd=directory, follow_symlinks=False)) != file_identity:
            raise DiagnosticFileError(_SAVE_ERROR)
        os.link(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory,
                follow_symlinks=False)
        if _identity(os.stat(path.name, dir_fd=directory, follow_symlinks=False)) != file_identity:
            raise DiagnosticFileError(_SAVE_ERROR)
        os.unlink(temporary, dir_fd=directory)
        temporary = None
        os.fsync(directory)
    finally:
        try:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except OSError:
                    pass
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                os.close(directory)


def _ordinary_windows_path(value: str) -> str:
    """Reject Win32 aliases whose meaning differs from an ordinary local path."""
    drive, tail = ntpath.splitdrive(value)
    if (not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\")
            or "/" in value or "\x00" in value):
        raise DiagnosticFileError(_SAVE_ERROR)
    components = tail[1:].split("\\")
    for component in components:
        if (not component or component in (".", "..") or component.endswith((".", " "))
                or any(character in component for character in '<>:"|?*')
                or any(ord(character) < 32 for character in component)
                or re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³]",
                                component.split(".", 1)[0], re.IGNORECASE)):
            raise DiagnosticFileError(_SAVE_ERROR)
    return value


class _WindowsApi:
    """Small, typed wrapper around supported Win32 filesystem operations."""

    def __init__(self):
        from ctypes import wintypes

        self.wintypes = wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        signatures = {
            "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "GetFinalPathNameByHandleW": ([wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                                           wintypes.DWORD], wintypes.DWORD),
            "GetFileInformationByHandleEx": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD], wintypes.BOOL),
            "GetFileType": ([wintypes.HANDLE], wintypes.DWORD),
            "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            "FlushFileBuffers": ([wintypes.HANDLE], wintypes.BOOL),
            "SetFileInformationByHandle": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             wintypes.DWORD], wintypes.BOOL),
            "LocalFree": ([ctypes.c_void_p], ctypes.c_void_p),
            "GetCurrentProcess": ([], wintypes.HANDLE),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        for name, arguments in {
            "OpenProcessToken": [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
            "GetTokenInformation": [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
            "ConvertSidToStringSidW": [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)],
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": [wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)],
        }.items():
            function = getattr(self.security, name)
            function.argtypes, function.restype = arguments, wintypes.BOOL

    @staticmethod
    def require(succeeded):
        if not succeeded:
            raise DiagnosticFileError(_SAVE_ERROR)

    def open_directory(self, path: str):
        # FILE_READ_ATTRIBUTES; FILE_SHARE_READ only; OPEN_EXISTING;
        # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
        handle = self.kernel.CreateFileW(path, 0x80, 0x1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value or handle is None:
            raise DiagnosticFileError(_SAVE_ERROR)
        try:
            attributes = (self.wintypes.DWORD * 2)()
            self.require(self.kernel.GetFileInformationByHandleEx(handle, 9, attributes,
                                                                  ctypes.sizeof(attributes)))
            if not attributes[0] & 0x10 or attributes[0] & 0x400 or self.kernel.GetFileType(handle) != 1:
                raise DiagnosticFileError(_SAVE_ERROR)
            return handle
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def final_path(self, handle) -> str:
        # VOLUME_NAME_GUID avoids mutable drive-letter aliases.
        capacity = self.kernel.GetFinalPathNameByHandleW(handle, None, 0, 1)
        self.require(capacity and capacity < 32768)
        buffer = ctypes.create_unicode_buffer(capacity + 1)
        length = self.kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 1)
        self.require(length and length < len(buffer))
        return buffer.value

    def file_identity(self, handle) -> bytes:
        # FILE_ID_INFO contains the volume serial and 128-bit file identifier.
        value = ctypes.create_string_buffer(24)
        self.require(self.kernel.GetFileInformationByHandleEx(handle, 18, value, len(value)))
        return value.raw

    def private_security(self):
        """Protected DACL granting access only to the current token user and SYSTEM."""
        token = self.wintypes.HANDLE()
        self.require(self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x8,
                                                    ctypes.byref(token)))
        sid_text = self.wintypes.LPWSTR()
        try:
            needed = self.wintypes.DWORD()
            self.security.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
            self.require(0 < needed.value < 65536)
            token_user = ctypes.create_string_buffer(needed.value)
            self.require(self.security.GetTokenInformation(token, 1, token_user, len(token_user),
                                                           ctypes.byref(needed)))
            sid = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p))[0]
            self.require(self.security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)))
            descriptor = ctypes.c_void_p()
            self.require(self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;FA;;;SY)(A;;FA;;;" + sid_text.value + ")", 1,
                ctypes.byref(descriptor), None))
            return descriptor
        finally:
            if sid_text:
                self.kernel.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
            self.kernel.CloseHandle(token)

    def rename_new(self, handle, target: str) -> None:
        class RenameInfo(ctypes.Structure):
            _fields_ = [("ReplaceIfExists", self.wintypes.BOOLEAN),
                        ("RootDirectory", self.wintypes.HANDLE),
                        ("FileNameLength", self.wintypes.DWORD),
                        ("FileName", self.wintypes.WCHAR * 1)]

        encoded = target.encode("utf-16-le")
        # FileNameLength excludes the terminator, but Win32's FileName member
        # is a NUL-terminated string. Keep the terminator inside the allocated
        # and submitted buffer so path conversion cannot read past its end.
        length = RenameInfo.FileName.offset + len(encoded) + 2
        storage = ctypes.create_string_buffer(max(length, ctypes.sizeof(RenameInfo)))
        info = ctypes.cast(storage, ctypes.POINTER(RenameInfo)).contents
        info.ReplaceIfExists, info.RootDirectory, info.FileNameLength = 0, None, len(encoded)
        ctypes.memmove(ctypes.addressof(storage) + RenameInfo.FileName.offset, encoded, len(encoded))
        self.require(self.kernel.SetFileInformationByHandle(handle, 3, storage, len(storage)))
        # Verify the destination through the still-exclusive file handle. An
        # unexpected name must not be reported as a successfully saved file.
        if self.final_path(handle).casefold() != target.casefold():
            raise DiagnosticFileError(_SAVE_ERROR)

    def discard(self, handle) -> None:
        # FILE_DISPOSITION_INFO deletes this exact open file, never a path entry
        # which may have been substituted. Windows removes it on handle close.
        delete = self.wintypes.BOOLEAN(1)
        self.require(self.kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete),
                                                           ctypes.sizeof(delete)))


def _write_windows(path: Path, data: bytes, expected_identity: tuple[int, int]) -> None:
    original_path = _ordinary_windows_path(str(path))
    api = _WindowsApi()
    initial = api.open_directory(ntpath.dirname(original_path))
    handles = [initial]
    temporary_handle = None
    descriptor = None
    committed = False
    try:
        canonical = api.final_path(initial)
        match = re.fullmatch(r"(\\\\\?\\Volume\{[0-9A-Fa-f-]{36}\}\\)(.*)", canonical)
        if match is None:
            raise DiagnosticFileError(_SAVE_ERROR)
        root, remainder = match.groups()
        prefixes = [root]
        if remainder:
            for component in remainder.split("\\"):
                if not component or component in (".", ".."):
                    raise DiagnosticFileError(_SAVE_ERROR)
                prefixes.append(ntpath.join(prefixes[-1], component))
        for prefix in prefixes:
            handle = api.open_directory(prefix)
            handles.append(handle)
            if api.final_path(handle).rstrip("\\").casefold() != prefix.rstrip("\\").casefold():
                raise DiagnosticFileError(_SAVE_ERROR)
        if (api.file_identity(initial) != api.file_identity(handles[-1])
                or api.final_path(initial).rstrip("\\").casefold() != canonical.rstrip("\\").casefold()
                or _identity(os.stat(canonical, follow_symlinks=False)) != expected_identity):
            raise DiagnosticFileError(_SAVE_ERROR)
        # All canonical ancestors are now pinned against rename and reparse.
        # The source handle stays exclusive through writing and atomic rename.
        descriptor = api.private_security()

        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("nLength", api.wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", api.wintypes.BOOL)]

        attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
        for _attempt in range(10):
            temporary = ntpath.join(canonical, ".upcam-diagnostic-" + secrets.token_hex(16) + ".tmp")
            # GENERIC_WRITE | DELETE | FILE_READ_ATTRIBUTES, no sharing,
            # CREATE_NEW and FILE_FLAG_OPEN_REPARSE_POINT.
            handle = api.kernel.CreateFileW(temporary, 0x40010080, 0, ctypes.byref(attributes),
                                            1, 0x00200080, None)
            if handle != ctypes.c_void_p(-1).value and handle is not None:
                temporary_handle = handle
                break
            if ctypes.get_last_error() not in (80, 183):
                raise DiagnosticFileError(_SAVE_ERROR)
        if temporary_handle is None:
            raise DiagnosticFileError(_SAVE_ERROR)
        for offset in range(0, len(data), 1024 * 1024):
            block = data[offset:offset + 1024 * 1024]
            buffer = ctypes.create_string_buffer(block)
            written = api.wintypes.DWORD()
            api.require(api.kernel.WriteFile(temporary_handle, buffer, len(block),
                                             ctypes.byref(written), None))
            api.require(written.value == len(block))
        api.require(api.kernel.FlushFileBuffers(temporary_handle))
        api.rename_new(temporary_handle, ntpath.join(canonical, path.name))
        committed = True
    finally:
        try:
            if temporary_handle is not None:
                try:
                    if not committed:
                        api.discard(temporary_handle)
                finally:
                    api.kernel.CloseHandle(temporary_handle)
        finally:
            if descriptor is not None:
                api.kernel.LocalFree(descriptor)
            for handle in reversed(handles):
                api.kernel.CloseHandle(handle)
