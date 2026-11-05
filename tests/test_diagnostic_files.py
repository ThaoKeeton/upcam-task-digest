"""Synthetic private-file and destination-retargeting regression checks."""

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from upcam_app import diagnostic_files as files


def identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino


class DiagnosticFileTests(unittest.TestCase):
    def test_bytes_private_mode_and_no_temporary_file_remains(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "synthetic-详细.json"
            content = b'{"synthetic":true,"bytes":"\x00\xff"}'
            files.write_private_diagnostic(target, content, expected_parent_identity=identity(parent))
            self.assertEqual(target.read_bytes(), content)
            self.assertEqual(list(parent.iterdir()), [target])
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_existing_file_and_directory_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            for is_directory in (False, True):
                target = parent / ("directory.json" if is_directory else "settings.json")
                if is_directory:
                    target.mkdir()
                else:
                    target.write_bytes(b"SYNTHETIC_EXISTING_SETTINGS")
                before = set(parent.iterdir())
                with self.assertRaises(files.DiagnosticFileError):
                    files.write_private_diagnostic(target, b"SYNTHETIC_PRIVATE_BODY",
                                                   expected_parent_identity=identity(parent))
                self.assertEqual(set(parent.iterdir()), before)
                if is_directory:
                    self.assertTrue(target.is_dir())
                else:
                    self.assertEqual(target.read_bytes(), b"SYNTHETIC_EXISTING_SETTINGS")

    def test_existing_symlinks_are_never_replaced_or_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            destination = parent / "synthetic-settings"
            destination.write_bytes(b"SYNTHETIC_SETTINGS")
            for dangling in (False, True):
                target = parent / ("dangling.json" if dangling else "link.json")
                try:
                    target.symlink_to(parent / "missing" if dangling else destination)
                except (OSError, NotImplementedError):
                    self.skipTest("This runner cannot create a synthetic file symlink")
                with self.assertRaises(files.DiagnosticFileError):
                    files.write_private_diagnostic(target, b"SYNTHETIC_PRIVATE_BODY",
                                                   expected_parent_identity=identity(parent))
                self.assertTrue(target.is_symlink())
                self.assertEqual(destination.read_bytes(), b"SYNTHETIC_SETTINGS")
                self.assertFalse((parent / "missing").exists())
            self.assertFalse(list(parent.glob(".upcam-diagnostic-*.tmp")))

    def test_parent_identity_mismatch_refuses_before_creating_any_file(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            expected = identity(parent)
            with self.assertRaises(files.DiagnosticFileError):
                files.write_private_diagnostic(parent / "synthetic.json", b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=(expected[0], expected[1] + 1))
            self.assertEqual(list(parent.iterdir()), [])

    def test_parent_replaced_at_writer_entry_cannot_redirect_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chosen, original, protected = (root / name for name in ("chosen", "original", "app-data"))
            chosen.mkdir()
            protected.mkdir()
            settings = protected / "settings.json"
            settings.write_bytes(b"SYNTHETIC_SETTINGS")
            expected = identity(chosen)
            chosen.rename(original)
            try:
                chosen.symlink_to(protected, target_is_directory=True)
            except (OSError, NotImplementedError):
                # Identity validation is still exercised when symlinks are not
                # available, as on Windows runners without developer mode.
                chosen.mkdir()
            with self.assertRaises(files.DiagnosticFileError):
                files.write_private_diagnostic(chosen / "new.json", b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=expected)
            self.assertEqual(settings.read_bytes(), b"SYNTHETIC_SETTINGS")
            self.assertFalse((protected / "new.json").exists())
            self.assertEqual(list(original.iterdir()), [])

    def test_failure_after_writing_cleans_temp_and_hides_underlying_error(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            target = parent / "synthetic.json"
            if os.name == "nt":
                failure = patch.object(files._WindowsApi, "rename_new",
                                       side_effect=OSError("SYNTHETIC_PRIVATE_ERROR_BODY"))
            else:
                failure = patch.object(files.os, "fsync",
                                       side_effect=OSError("SYNTHETIC_PRIVATE_ERROR_BODY"))
            with failure, self.assertRaises(files.DiagnosticFileError) as caught:
                files.write_private_diagnostic(target, b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=identity(parent))
            self.assertEqual(str(caught.exception), files._SAVE_ERROR)
            self.assertNotIn("SYNTHETIC_PRIVATE", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertEqual(list(parent.iterdir()), [])

    def test_missing_parent_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "missing"
            with self.assertRaises(files.DiagnosticFileError):
                files.write_private_diagnostic(parent / "synthetic.json", b"{}",
                                               expected_parent_identity=(0, 0))
            self.assertFalse(parent.exists())

    def test_invalid_suffix_and_relative_path_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            for path in (Path("synthetic.json"), parent / "synthetic.txt", parent / ".." / "synthetic.json"):
                with self.subTest(path=path), self.assertRaises(files.DiagnosticFileError):
                    files.write_private_diagnostic(path, b"{}", expected_parent_identity=identity(parent))
            self.assertEqual(list(parent.iterdir()), [])

    def test_windows_path_aliases_streams_and_reserved_names_fail_closed(self):
        self.assertEqual(files._ordinary_windows_path(r"C:\synthetic\snapshot.json"),
                         r"C:\synthetic\snapshot.json")
        for value in (r"\\?\C:\synthetic\snapshot.json", r"\\.\C:\synthetic\snapshot.json",
                      r"\??\C:\synthetic\snapshot.json", r"\\server\share\snapshot.json",
                      r"C:synthetic\snapshot.json", r"C:\synthetic\snapshot.json:stream",
                      r"C:\synthetic\CON.json", r"C:\synthetic\COM¹.json",
                      "C:\\synthetic \\snapshot.json", r"C:\synthetic\..\snapshot.json"):
            with self.subTest(path=value), self.assertRaises(files.DiagnosticFileError):
                files._ordinary_windows_path(value)


@unittest.skipUnless(os.name == "posix", "POSIX directory descriptor guarantees")
class PosixDiagnosticFileTests(unittest.TestCase):
    def test_parent_retargeted_after_open_stays_in_original_directory(self):
        self._retarget_after_open(False)

    def test_ancestor_retargeted_after_open_stays_in_original_directory(self):
        self._retarget_after_open(True)

    def _retarget_after_open(self, replace_ancestor):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ancestor = root / "selected-tree"
            chosen = ancestor / "chosen"
            protected = root / "app-data"
            chosen.mkdir(parents=True)
            protected.mkdir()
            (protected / "chosen").mkdir()
            protected_target = (protected / "chosen" if replace_ancestor else protected) / "snapshot.json"
            protected_target.write_bytes(b"SYNTHETIC_EXISTING_SETTINGS")
            original = root / "original"
            expected = identity(chosen)
            real_write = files._write_all

            def retarget(descriptor, data):
                replaced = ancestor if replace_ancestor else chosen
                replaced.rename(original)
                replaced.symlink_to(protected, target_is_directory=True)
                real_write(descriptor, data)

            with patch.object(files, "_write_all", side_effect=retarget):
                files.write_private_diagnostic(chosen / "snapshot.json", b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=expected)
            actual = (original / "chosen" if replace_ancestor else original) / "snapshot.json"
            self.assertEqual(actual.read_bytes(), b"SYNTHETIC_PRIVATE_BODY")
            self.assertEqual(protected_target.read_bytes(), b"SYNTHETIC_EXISTING_SETTINGS")
            self.assertEqual(list(actual.parent.iterdir()), [actual])

    def test_short_writes_are_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            real_write = os.write

            def short_write(descriptor, data):
                return real_write(descriptor, data[:3])

            with patch.object(files.os, "write", side_effect=short_write):
                files.write_private_diagnostic(parent / "snapshot.json", b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=identity(parent))
            self.assertEqual((parent / "snapshot.json").read_bytes(), b"SYNTHETIC_PRIVATE_BODY")

    def test_no_pathname_fallback_without_required_primitives(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            with patch.object(files.os, "supports_dir_fd", set()), self.assertRaises(files.DiagnosticFileError):
                files.write_private_diagnostic(parent / "snapshot.json", b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=identity(parent))
            self.assertEqual(list(parent.iterdir()), [])


@unittest.skipUnless(os.name == "nt", "Actual Windows directory handles and security")
class WindowsDiagnosticFileTests(unittest.TestCase):
    def test_pinned_parent_and_ancestor_cannot_be_renamed_during_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ancestor = root / "synthetic-tree"
            parent = ancestor / "chosen"
            parent.mkdir(parents=True)
            original_rename = files._WindowsApi.rename_new
            attempted = []

            def check_pins(api, handle, target):
                for path in (parent, ancestor):
                    with self.assertRaises(OSError):
                        path.rename(root / (path.name + "-moved"))
                    attempted.append(path)
                return original_rename(api, handle, target)

            with patch.object(files._WindowsApi, "rename_new", new=check_pins):
                files.write_private_diagnostic(parent / "snapshot.json", b"SYNTHETIC_PRIVATE_BODY",
                                               expected_parent_identity=identity(parent))
            self.assertEqual(attempted, [parent, ancestor])
            self.assertEqual((parent / "snapshot.json").read_bytes(), b"SYNTHETIC_PRIVATE_BODY")
            # Every pinned handle was released after returning.
            parent.rename(ancestor / "released")


if __name__ == "__main__":
    unittest.main()
