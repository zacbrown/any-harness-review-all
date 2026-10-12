"""The restricted Windows PATH works with either native Git entry point."""

from pathlib import Path
import tempfile
import unittest

from native_ci import windows_git_directory


class WindowsGitDirectory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "Program Files" / "Git"
        for name in ("bin", "cmd", "usr/bin"):
            directory = self.root / name
            directory.mkdir(parents=True)
            (directory / "git.exe").touch()
        (self.root / "bin/bash.exe").touch()

    def test_cmd_entry_point_is_preserved(self):
        self.assertEqual(self.root / "cmd", windows_git_directory(self.root / "cmd/git.exe"))

    def test_bin_entry_point_uses_cmd_without_exposing_bash(self):
        directory = windows_git_directory(self.root / "bin/git.exe")
        self.assertEqual(self.root / "cmd", directory)
        self.assertFalse((directory / "bash.exe").exists())

    def test_missing_cmd_executable_fails_closed(self):
        (self.root / "cmd/git.exe").unlink()
        for name in ("bin", "cmd"):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "cmd/git.exe not found"):
                windows_git_directory(self.root / name / "git.exe")

    def test_missing_git_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, "Git for Windows missing"):
            windows_git_directory(None)

    def test_unix_git_does_not_enter_native_path(self):
        with self.assertRaisesRegex(RuntimeError, "cmd/git.exe not found"):
            windows_git_directory(self.root / "usr/bin/git.exe")
