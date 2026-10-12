from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/review-all/scripts"))
import portable
from portable import command_line, execute, file_lock, find_executable, run


class NativeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-all-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()


class Portable(NativeCase):
    def test_windows_lookup_ignores_repo_relative_path_and_cwd(self):
        original_search_control = os.environ.get("NoDefaultCurrentDirectoryInExePath")
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "bin").mkdir()
        working = repo / "subdirectory"
        working.mkdir()
        trusted = self.root / "trusted"
        trusted.mkdir()
        for directory in (repo, repo / "bin", working, trusted):
            for name in ("git.exe", "powershell.exe"):
                binary = directory / name
                binary.write_bytes(b"planted binary")
                binary.chmod(0o755)
        env = {"PATH": os.pathsep.join(["", ".", "bin", str(repo), str(repo / "bin"), str(trusted)]),
               "PATHEXT": ".exe;.cmd", "NoDefaultCurrentDirectoryInExePath": ""}
        with patch.object(portable, "WINDOWS", True):
            self.assertEqual(str(trusted / "git.exe"), find_executable("git", env, working))
            self.assertEqual(str(trusted / "powershell.exe"), find_executable("powershell", env, working))
            command_line(["git", "status"], env, working)
            self.assertEqual(str(trusted), env["PATH"])
            self.assertEqual("1", env["NoDefaultCurrentDirectoryInExePath"])
            (trusted / "git.exe").unlink()
            with self.assertRaises(FileNotFoundError):
                command_line(["git", "status"], env, working)
        self.assertEqual(original_search_control, os.environ.get("NoDefaultCurrentDirectoryInExePath"))

    @unittest.skipUnless(os.name == "nt", "Windows planted executable regression")
    def test_windows_discovery_does_not_execute_planted_git(self):
        import discovery
        trusted_git = shutil.which("git")
        self.assertIsNotNone(trusted_git)
        (self.root / "git.exe").write_bytes(b"this binary must never execute")
        (self.root / ".git").mkdir()
        working = self.root / "nested"
        working.mkdir()
        environment = {**os.environ, "PATH": os.pathsep.join(["", ".", str(self.root), str(Path(trusted_git).parent)]),
                       "NoDefaultCurrentDirectoryInExePath": ""}
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.dict(os.environ, environment):
                self.assertTrue(discovery.preflight(working)["git"])
                self.assertEqual(str(self.root), str(discovery.root(self.root)))
                result = execute(["git", "--version"], cwd=working)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(b"git version", result.stdout)
                child = execute([sys.executable, "-c", "import os; print(os.environ['NoDefaultCurrentDirectoryInExePath'])"], cwd=working)
                self.assertEqual(b"1", child.stdout.strip())
        finally:
            os.chdir(original)

    def test_bytes_arguments_large_output_and_errors(self):
        args = ["space value", "\u03bb\u4e16", "[a]", "$x; & ! %PATH% ^", 'a"b', "ends\\"]
        result = execute([sys.executable, "-c", "import sys,json; print(json.dumps(sys.argv[1:])); "
                          "sys.stderr.buffer.write(b'e'*300000)", *args], cwd=self.root)
        self.assertEqual(args, json.loads(result.stdout))
        self.assertEqual(300000, len(result.stderr))
        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.returncode)
        result = execute([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\r\\n'); sys.exit(7)"], cwd=self.root)
        self.assertEqual(b"\xff\r\n", result.stdout)
        self.assertEqual(7, result.returncode)
        with self.assertRaises(FileNotFoundError):
            execute(["review-all-definitely-missing-executable"], cwd=self.root)
        with self.assertRaises(ValueError):
            execute([sys.executable], cwd=self.root, timeout=0)

    def test_merged_output_keeps_write_order(self):
        script = ("import os; os.write(1,b'out-1\\n'); os.write(2,b'err-1\\n'); "
                  "os.write(1,b'out-2\\n'); os.write(2,b'err-2\\n')")
        result = execute([sys.executable, "-c", script], cwd=self.root, merge_stderr=True)
        self.assertEqual(0, result.returncode)
        self.assertEqual(b"out-1\nerr-1\nout-2\nerr-2\n", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_timeout_and_normal_exit_clean_descendants(self):
        for timeout in (True, False):
            marker = self.root / ("late-" + str(timeout))
            child = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text('survived')"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
            parent += "print('spawned',flush=True); " + ("time.sleep(10)" if timeout else "")
            result = execute([sys.executable, "-c", parent, child], cwd=self.root, timeout=0.4)
            self.assertEqual(timeout, result.timed_out)
            self.assertEqual(124 if timeout else 0, result.returncode)
            self.assertIn(b"spawned", result.stdout)
            self.assertLess(result.elapsed, 5)
            time.sleep(1.1)
            self.assertFalse(marker.exists(), "a descendant survived cleanup")

    def test_lock_serializes_processes_and_times_out(self):
        count = self.root / "count"
        count.write_text("0", encoding="utf-8")
        code = ("import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from portable import file_lock; "
                "p=Path(sys.argv[2]);\nwith file_lock(str(p)+'.lock'):\n"
                " n=int(p.read_text()); time.sleep(.02); p.write_text(str(n+1))\n")
        def increment(_):
            return execute([sys.executable, "-c", code, str(ROOT / "skills/review-all/scripts"), str(count)], cwd=self.root)
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(r.returncode == 0 for r in pool.map(increment, range(8))))
        self.assertEqual("8", count.read_text(encoding="utf-8"))
        with file_lock(self.root / "lock"):
            with self.assertRaises(TimeoutError):
                with file_lock(self.root / "lock", timeout=0.05):
                    self.fail("acquired an already locked file")

    @unittest.skipUnless(os.name == "nt", "Windows command launcher contract")
    def test_windows_batch_and_powershell_launchers(self):
        helper = self.root / "args.py"
        helper.write_text("import sys,json; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
        for suffix in (".cmd", ".bat"):
            launcher = self.root / ("launcher & [test]" + suffix)
            launcher.write_text(f'@"{sys.executable}" "{helper}" %*\n', encoding="utf-8")
            args = ["a b", "[brackets]", "a&b", "x|y", "x>y", "%PATH%", "!wow!", "\u03bb", "a^b", "C:\\ends\\"]
            result = execute([str(launcher), *args], cwd=self.root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(args, json.loads(result.stdout))
            with self.assertRaisesRegex(ValueError, "quotes"):
                execute([str(launcher), 'a"b'], cwd=self.root)
        if shutil.which("powershell") or shutil.which("pwsh"):
            script = self.root / "args.ps1"
            script.write_text('Write-Output "powershell-ok"\n', encoding="utf-8")
            result = execute([str(script)], cwd=self.root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn(b"powershell-ok", result.stdout)


class Entrypoints(NativeCase):
    def command(self, *args, tool=ROOT, cwd=None, env=None):
        environment = dict(os.environ if env is None else env)
        environment.pop("UV_PROJECT_ENVIRONMENT", None)
        result = subprocess.run(["uv", "run", "--project", str(tool), "--locked", "--no-sync",
                                 "--no-python-downloads", "review-all", *args],
                                cwd=cwd or self.root, env=environment, capture_output=True, timeout=180)
        return result

    def repo(self):
        repo = self.root / "unrelated [\u03bb] & % !"
        repo.mkdir()
        run(["git", "init", "-q"], cwd=repo, capture_output=True, check=True)
        (repo / "pyproject.toml").write_text("[project]\nname='unrelated'\nrequires-python='>=99'\nversion='1'\n", encoding="utf-8")
        (repo / ".python-version").write_text("99.9\n", encoding="utf-8")
        return repo

    def test_uv_and_just_from_unrelated_repo(self):
        repo = self.repo()
        for command in (
                ["uv", "run", "--project", str(ROOT), "--locked", "--no-sync", "--no-python-downloads",
                 "review-all", "--repo", str(repo), "discover"],
                ["just", "--justfile", str(ROOT / "justfile"), "discover", str(repo)]):
            result = subprocess.run(command, cwd=repo, capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(str(repo), json.loads(result.stdout)["repo"])
        self.assertFalse((repo / ".venv").exists())
        self.assertFalse((repo / "uv.lock").exists())
        result = self.command("discover")
        self.assertEqual(2, result.returncode)
        self.assertIn(b"requires --repo", result.stderr)

    def test_raw_diff_and_file_input(self):
        repo = self.repo()
        file = repo / "source.txt"
        file.write_bytes(b"before\n")
        run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        file.write_bytes(b"after\r\n")
        expected = run(["git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--binary"],
                       cwd=repo, capture_output=True, check=True).stdout
        output = self.root / "diff bytes"
        result = self.command("--repo", str(repo), "diff", "--output", str(output))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(expected, output.read_bytes())
        result = self.command("--repo", str(repo), "--input", str(output), "checkpoint", "load", "--head", "test")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("runKey", json.loads(result.stdout))

    def test_model_settings_use_explicit_repository_and_role(self):
        repo = self.repo()
        config = repo / ".copilot/review-all.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"models": {"deep-dive": "gpt-6-luna"},
                                      "reasoningEfforts": {"deep-dive": "medium"}}), encoding="utf-8")
        result = self.command("--repo", str(repo), "copilot-package", "model-settings", "--role", "deep-dive")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"role": "deep-dive", "model": "gpt-6-luna", "reasoning_effort": "medium"},
                         json.loads(result.stdout))

    def test_execute_reports_failure_timeout_and_missing_executable(self):
        spec = self.root / "check.json"
        for command, timeout, expected in (([sys.executable, "-c", "raise SystemExit(9)"], 10, 1),
                                           ([sys.executable, "-c", "import time; time.sleep(10)"], 0.1, 124),
                                           (["review-all-missing-tool"], 10, 2)):
            spec.write_text(json.dumps({"argv": command, "timeoutSeconds": timeout}), encoding="utf-8")
            result = self.command("--repo", str(self.root), "execute", str(spec))
            self.assertEqual(expected, result.returncode, result.stderr)
            if expected != 2:
                receipt = json.loads(result.stdout)
                self.assertEqual(expected == 124, receipt["timedOut"])
                self.assertEqual(str(self.root), receipt["cwd"])
            else:
                self.assertIn(b"executable not found", result.stderr)

    def test_installed_unit_lifecycle_without_checkout(self):
        repo = self.repo()
        install = self.command("install", "--project", str(repo))
        self.assertEqual(0, install.returncode, install.stderr)
        destination = repo / ".claude"
        tool = destination / ".review-all-tool"
        self.assertTrue((tool / "uv.lock").is_file())
        self.assertTrue((tool / "justfile").is_file())
        result = self.command("--repo", str(repo), "discover", tool=tool)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(repo), json.loads(result.stdout)["repo"])
        module = subprocess.run(["uv", "run", "--project", str(tool), "--locked", "--no-sync",
                                 "--no-python-downloads", "python", "-c", "import review_all; print(review_all.__file__)"],
                                cwd=repo, capture_output=True, timeout=30,
                                env={k: v for k, v in os.environ.items() if k != "UV_PROJECT_ENVIRONMENT"})
        self.assertEqual(0, module.returncode, module.stderr)
        self.assertTrue(Path(module.stdout.decode("utf-8").strip()).is_relative_to(tool))
        upgrade = self.command("install", "--project", str(repo))
        self.assertEqual(0, upgrade.returncode, upgrade.stderr)
        edited = destination / "skills/review-all/SKILL.md"
        edited.write_text("user edit", encoding="utf-8")
        unrelated = destination / "keep.txt"
        unrelated.write_text("unrelated", encoding="utf-8")
        collision = self.command("install", "--project", str(repo))
        self.assertEqual(2, collision.returncode)
        result = subprocess.run([sys._base_executable, str(tool / "skills/review-all/cli.py"),
                                 "uninstall", "--project", str(repo)],
                                cwd=repo, capture_output=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("user edit", edited.read_text(encoding="utf-8"))
        self.assertEqual("unrelated", unrelated.read_text(encoding="utf-8"))
        self.assertFalse((tool / "pyproject.toml").exists())

    def test_plugin_runtime_is_self_contained_and_requires_explicit_setup(self):
        from copilot_common import load_module
        installer = load_module("copilot-install")
        tool = self.root / "installed plugin"
        for name, content in installer.tool_files().items():
            target = tool / Path(name).relative_to(".review-all-tool")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        entry = tool / ".github/plugin/skills/review-all"
        self.assertEqual(tool, (entry / "../../../..").resolve())
        cli = tool / "skills/review-all/cli.py"
        repo = self.repo()
        result = subprocess.run([sys._base_executable, str(cli), "preflight"], cwd=repo,
                                capture_output=True, timeout=30)
        self.assertEqual(2, result.returncode)
        self.assertIn(b"incomplete or stale tool environment", result.stderr)
        self.assertFalse((tool / ".venv").exists())
        setup = subprocess.run([sys._base_executable, str(cli), "setup"], cwd=repo,
                               capture_output=True, timeout=180)
        self.assertEqual(0, setup.returncode, setup.stderr)
        result = self.command("--repo", str(repo), "discover", "--harness", "copilot", tool=tool, cwd=repo)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(repo), json.loads(result.stdout)["repo"])
        self.assertFalse((repo / ".venv").exists())
        self.assertFalse((repo / "uv.lock").exists())
        result = self.command("test", "--pattern", "test_copilot_plugin.py", tool=tool, cwd=repo)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_gate_verdict_contract_through_uv_and_just(self):
        repo = self.repo()
        source = repo / "source.py"
        source.write_text("value = 1\n", encoding="utf-8")
        run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "baseline"],
            cwd=repo, capture_output=True, check=True)
        source.write_text("value = 2\n", encoding="utf-8")
        common = ["--copilot", str(ROOT / "tests/fake_copilot.py"), "--allow-tools", "--", "--unstaged"]
        for launcher in ("uv", "just"):
            for mode, expected in (("pass", 0), ("partial", 1), ("missing", 2), ("stale", 2),
                                   ("malformed", 2), ("fabricated", 2), ("mismatched", 2)):
                with self.subTest(launcher=launcher, mode=mode):
                    env = {**os.environ, "REVIEW_ALL_FAKE_MODE": mode}
                    if launcher == "uv":
                        result = self.command("--repo", str(repo), "copilot-gate", *common, env=env)
                    else:
                        result = subprocess.run(["just", "--justfile", str(ROOT / "justfile"),
                                                 "gate", str(repo), *common],
                                                cwd=repo, env=env, capture_output=True, timeout=60)
                    self.assertEqual(expected, result.returncode, result.stderr)
