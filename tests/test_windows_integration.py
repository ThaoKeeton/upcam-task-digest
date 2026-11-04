"""Windows startup, launch, and window-lifecycle tests use synthetic adapters."""

import tempfile
import unittest
from pathlib import Path

from desktop_app import DigestDesktopApp
from upcam_app.config import DEFAULT_SETTINGS, SettingsStore
from upcam_app.windows_integration import (
    LaunchOptions, StartupIntegrationError, configure_user_startup,
    parse_launch_options, startup_command, WindowsSingleInstance,
)


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def CreateKeyEx(self, root, path, _reserved, _access):
        self.root, self.path = root, path
        return "fake-key"

    def SetValueEx(self, key, name, _reserved, kind, value):
        self.values[name] = (kind, value)

    def DeleteValue(self, _key, name):
        del self.values[name]

    def CloseKey(self, _key):
        pass


class FakeRoot:
    def __init__(self):
        self.calls = []

    def withdraw(self):
        self.calls.append("withdraw")

    def deiconify(self):
        self.calls.append("deiconify")

    def lift(self):
        self.calls.append("lift")

    def focus_force(self):
        self.calls.append("focus")

    def destroy(self):
        self.calls.append("destroy")


class FakeIcon:
    def __init__(self):
        self.stopped = False
        self.visible = True

    def stop(self):
        self.stopped = True


class _FakeKernel:
    def __init__(self):
        self.closed = []

    def CloseHandle(self, handle):
        self.closed.append(handle)


class _ExistingWindowsInstance(WindowsSingleInstance):
    def __init__(self):
        super().__init__(lambda: None, is_windows=True)
        self.kernel = _FakeKernel()
        self.notified = False
        self.listener_started = False

    def _create_mutex(self):
        return self.kernel, "existing-handle", 183

    def _notify_existing(self):
        self.notified = True

    def _start_listener(self):
        self.listener_started = True


class WindowsIntegrationTests(unittest.TestCase):
    def test_launch_options_accept_only_supported_startup_and_smoke_flags(self):
        self.assertEqual(parse_launch_options([]), LaunchOptions())
        self.assertEqual(parse_launch_options(["--background"]), LaunchOptions(background=True))
        self.assertEqual(parse_launch_options(["--ui-smoke-test"]), LaunchOptions(smoke_test=True))
        self.assertEqual(parse_launch_options(["--ui-smoke-test", "--background"]),
                         LaunchOptions(background=True, smoke_test=True))
        with self.assertRaises(ValueError):
            parse_launch_options(["--unknown"])

    def test_startup_command_quotes_exe_path_and_requests_tray_start(self):
        command = startup_command(
            executable=r"C:\Program Files\UPCAM\UPCAMTaskDigest.exe",
            script=r"C:\Program Files\UPCAM\desktop_app.py",
            frozen=True,
        )

        self.assertEqual(command, '"C:\\Program Files\\UPCAM\\UPCAMTaskDigest.exe" --background')

    def test_startup_setting_uses_current_user_run_key_and_is_removable(self):
        registry = FakeRegistry()
        configure_user_startup(True, registry=registry, is_windows=True,
                               executable=r"C:\Program Files\UPCAM\App.exe", frozen=True)

        self.assertEqual(registry.root, registry.HKEY_CURRENT_USER)
        self.assertEqual(registry.path, r"Software\Microsoft\Windows\CurrentVersion\Run")
        self.assertEqual(registry.values["UPCAMTaskDigest"], (registry.REG_SZ,
                         '"C:\\Program Files\\UPCAM\\App.exe" --background'))
        configure_user_startup(False, registry=registry, is_windows=True)
        self.assertNotIn("UPCAMTaskDigest", registry.values)

    def test_startup_toggle_never_touches_registry_on_non_windows(self):
        with self.assertRaises(StartupIntegrationError):
            configure_user_startup(True, registry=FakeRegistry(), is_windows=False)

    def test_second_process_notifies_existing_instance_and_exits(self):
        instance = _ExistingWindowsInstance()

        self.assertFalse(instance.acquire())
        self.assertTrue(instance.notified)
        self.assertFalse(instance.listener_started)
        self.assertEqual(instance.kernel.closed, ["existing-handle"])

    def test_close_hides_to_tray_and_explicit_exit_stops_without_rewriting_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings_path = Path(temporary) / "settings.json"
            store = SettingsStore(settings_path)
            store.save(dict(DEFAULT_SETTINGS, project_id="synthetic-project"))
            before = settings_path.read_bytes()
            app = DigestDesktopApp.__new__(DigestDesktopApp)
            app.root = FakeRoot()
            icon = FakeIcon()
            app._tray_icon = icon
            app.settings_store = store

            app._hide_to_tray()
            self.assertEqual(app.root.calls, ["withdraw"])
            app._show_window()
            self.assertEqual(app.root.calls[-3:], ["deiconify", "lift", "focus"])
            app._quit_application()

            self.assertTrue(icon.stopped)
            self.assertEqual(app.root.calls[-1], "destroy")
            self.assertEqual(settings_path.read_bytes(), before)

    def test_close_exits_if_no_tray_is_available(self):
        app = DigestDesktopApp.__new__(DigestDesktopApp)
        app.root = FakeRoot()
        app._tray_icon = None

        app._hide_to_tray()

        self.assertEqual(app.root.calls, ["destroy"])


if __name__ == "__main__":
    unittest.main()
