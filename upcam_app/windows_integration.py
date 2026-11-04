"""Small Windows-only integration points with injectable test adapters."""

from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
import sys
import threading


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "UPCAMTaskDigest"
MUTEX_NAME = r"Local\UPCAMTaskDigest.Singleton.v1"
PIPE_NAME = r"\\.\pipe\UPCAMTaskDigest.Show.v1"
PIPE_AUTHKEY = b"UPCAMTaskDigest-local-show-window-v1"


class StartupIntegrationError(RuntimeError):
    """A requested current-user startup change is unavailable or failed."""


@dataclass(frozen=True)
class LaunchOptions:
    background: bool = False
    smoke_test: bool = False


def parse_launch_options(argv) -> LaunchOptions:
    args = list(argv)
    if len(args) != len(set(args)):
        raise ValueError("Duplicate application argument")
    if set(args) - {"--background", "--ui-smoke-test"}:
        raise ValueError("Unsupported application argument")
    return LaunchOptions(background="--background" in args,
                         smoke_test="--ui-smoke-test" in args)


def startup_command(*, executable=None, script=None, frozen=False) -> str:
    """Build the quoted current-user Run command that starts hidden to tray."""
    if frozen:
        if not executable:
            raise StartupIntegrationError("Packaged application path is unavailable")
        argv = [str(executable), "--background"]
    else:
        if not script:
            raise StartupIntegrationError("Application script path is unavailable")
        argv = [sys.executable, str(script), "--background"]
    return subprocess.list2cmdline(argv)


def configure_user_startup(enabled: bool, *, registry=None, is_windows=None,
                           executable=None, script=None, frozen=None) -> None:
    """Add/remove only this app's current-user Run value (no admin privileges)."""
    if type(enabled) is not bool:
        raise StartupIntegrationError("Startup setting must be true or false")
    windows = os.name == "nt" if is_windows is None else bool(is_windows)
    if not windows:
        raise StartupIntegrationError("Windows sign-in startup is available only on Windows")
    if registry is None:
        try:
            import winreg as registry
        except ImportError:
            raise StartupIntegrationError("Windows startup registry is unavailable") from None
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    command = startup_command(executable=executable or sys.executable,
                              script=script or sys.argv[0], frozen=frozen) if enabled else None
    try:
        key = registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0,
                                   registry.KEY_SET_VALUE)
        try:
            if enabled:
                registry.SetValueEx(key, RUN_VALUE, 0, registry.REG_SZ, command)
            else:
                try:
                    registry.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        finally:
            registry.CloseKey(key)
    except OSError:
        raise StartupIntegrationError("无法更新当前 Windows 用户的开机启动项") from None


class WindowsSingleInstance:
    """A named mutex prevents duplicates; a local named pipe asks the first to show."""

    def __init__(self, on_show, *, is_windows=None):
        self.on_show = on_show
        self.is_windows = os.name == "nt" if is_windows is None else bool(is_windows)
        self.handle = None
        self.listener = None
        self.listener_thread = None

    def acquire(self) -> bool:
        if not self.is_windows:
            return True
        try:
            kernel32, handle, last_error = self._create_mutex()
            if not handle:
                raise OSError("CreateMutexW failed")
            if last_error == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                self._notify_existing()
                return False
            self.handle = handle
            self._start_listener()
            return True
        except OSError:
            raise StartupIntegrationError("无法建立应用单实例锁") from None

    def _create_mutex(self):
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        return kernel32, handle, ctypes.get_last_error()

    def _notify_existing(self):
        try:
            from multiprocessing.connection import Client
            with Client(PIPE_NAME, family="AF_PIPE", authkey=PIPE_AUTHKEY) as connection:
                connection.send("show")
        except (OSError, EOFError):
            # The primary process may still be creating its listener. The new
            # instance exits instead of starting a second scheduler.
            pass

    def _start_listener(self):
        try:
            from multiprocessing.connection import Listener
            self.listener = Listener(PIPE_NAME, family="AF_PIPE", authkey=PIPE_AUTHKEY)
        except (OSError, ImportError):
            self.listener = None
            return
        self.listener_thread = threading.Thread(target=self._serve, daemon=True,
                                               name="upcam-instance-requests")
        self.listener_thread.start()

    def _serve(self):
        while self.listener is not None:
            try:
                connection = self.listener.accept()
            except (OSError, EOFError):
                return
            with connection:
                try:
                    message = connection.recv()
                except (OSError, EOFError):
                    continue
            if message == "show":
                try:
                    self.on_show()
                except Exception:
                    pass

    def close(self):
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        if self.handle is not None and self.is_windows:
            try:
                import ctypes
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
            except OSError:
                pass
            self.handle = None

