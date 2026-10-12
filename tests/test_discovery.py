import hashlib
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from threading import Thread
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/review-all/scripts"))
import discovery
from portable import run


class Discovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-all-[space & unicode-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        run(["git", "init", "-q"], cwd=self.repo, capture_output=True, check=True)
        self.cache = self.repo / ".claude/cache/review-all-profile.json"

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def test_preflight_capabilities_and_empty_project(self):
        available = discovery.preflight()
        self.assertTrue(available["git"])
        self.assertTrue(all(available["capabilities"].values()))
        self.assertEqual("", discovery.toolchain(self.repo)["ecosystem"])
        with patch.object(discovery, "find_executable", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "git"):
                discovery.discover(self.repo)

    def test_package_manager_scripts_not_raw_shell(self):
        self.write("package.json", json.dumps({"scripts": {"test": "jest --ci && echo done", "build": "tsc -b"},
                                              "packageManager": "pnpm@10.0.0"}))
        data = discovery.toolchain(self.repo)
        self.assertEqual("js", data["ecosystem"])
        self.assertEqual("jest --ci && echo done", data["test"])
        self.assertEqual(["pnpm", "run", "test"], data["commands"]["test"]["argv"])
        self.write("package.json", "{bad")
        with self.assertRaises(ValueError):
            discovery.toolchain(self.repo)

    def test_test_patterns(self):
        self.write("src/a.spec.ts", "x")
        run(["git", "add", "."], cwd=self.repo, capture_output=True, check=True)
        self.assertEqual({"pattern": "co-located", "suffix": ".spec.ts", "framework": "unknown"},
                         discovery.test_pattern(self.repo))

    def test_null_scripts_discovers_without_checks(self):
        self.write("package.json", json.dumps({"scripts": None}))
        data = discovery.discover(self.repo)["toolchain"]
        self.assertEqual("js", data["ecosystem"])
        self.assertEqual({}, data["commands"])
        for scripts in ([], "npm test", False, 1):
            self.write("package.json", json.dumps({"scripts": scripts}))
            with self.subTest(scripts=scripts), self.assertRaisesRegex(ValueError, "scripts must be"):
                discovery.discover(self.repo)

    @unittest.skipIf(os.name == "nt", "POSIX filenames allow arbitrary byte sequences")
    def test_git_path_bytes_use_filesystem_decoding(self):
        data = b"bad-\xff.test.py\0"
        with patch.object(discovery, "execute", return_value=SimpleNamespace(returncode=0, stdout=data)):
            self.assertEqual([os.fsdecode(data[:-1])], discovery.tracked(self.repo))
        result = SimpleNamespace(returncode=0, timed_out=False, stdout=b"/repo-\xff\n")
        with patch.object(discovery, "execute", return_value=result):
            self.assertEqual(os.fsdecode(b"/repo-\xff"), str(discovery.root(self.repo)))

    @unittest.skipIf(os.name == "nt", "POSIX filenames allow arbitrary byte sequences")
    def test_non_utf8_git_paths_and_rule_directories(self):
        filename = os.fsdecode(b"bad-\xff.test.py")
        try:
            self.write(filename, "value = 1\n")
        except OSError as error:
            if error.errno == errno.EILSEQ:
                self.skipTest("filesystem rejects non-UTF-8 names")
            raise
        rules = self.write(os.fsdecode(b"module-\xff/CLAUDE.md"), "# Rules\n")
        run(["git", "add", "."], cwd=self.repo, capture_output=True, check=True)
        self.assertIn(filename, discovery.tracked(self.repo))
        result = discovery.discover(self.repo)
        self.assertEqual(".test.py", result["testPattern"]["suffix"])
        manifest = (b"v2\nmodule-\xff/CLAUDE.md " + hashlib.sha256(rules.read_bytes()).hexdigest().encode() + b"\n")
        self.assertEqual(hashlib.sha256(manifest).hexdigest(), result["cacheKey"])

    def test_typescript_fallback_requires_installed_compiler(self):
        self.write("package.json", "{}")
        self.write("tsconfig.json", "{}")
        data = discovery.toolchain(self.repo)
        self.assertEqual("", data["typecheck"])
        self.assertNotIn("typecheck", data["commands"])
        compiler = self.write("node_modules/typescript/bin/tsc", "// installed compiler")
        data = discovery.toolchain(self.repo)
        self.assertEqual(["node", str(compiler), "--noEmit"], data["commands"]["typecheck"]["argv"])
        self.write("package.json", json.dumps({"scripts": {"typecheck": "custom-check"}}))
        compiler.unlink()
        self.assertEqual(["npm", "run", "typecheck"], discovery.toolchain(self.repo)["commands"]["typecheck"]["argv"])

    def test_cache_contract_and_exact_input_bytes(self):
        rules = self.write("CLAUDE.md", b"# Rules\r\n")
        run(["git", "add", "."], cwd=self.repo, capture_output=True, check=True)
        initial = discovery.discover(self.repo)
        expected = hashlib.sha256(b"v2\nCLAUDE.md " + hashlib.sha256(rules.read_bytes()).hexdigest().encode() + b"\n").hexdigest()
        self.assertEqual(expected, initial["cacheKey"])
        self.assertEqual({"status": "MISS", "reason": "no-cache"}, initial["cache"])
        valid = {"schemaVersion": 2, "cacheKey": expected, "rules": {"global": "never eval"}}
        self.write(".claude/cache/review-all-profile.json", json.dumps(valid))
        self.assertEqual(valid, discovery.discover(self.repo)["cachedProfile"])
        for value, reason in (({"claudeMdHash": "old"}, "schema"), ("broken", "unreadable"), ([], "schema")):
            self.cache.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
            self.assertEqual(reason, discovery.discover(self.repo)["cache"]["reason"])
        self.cache.write_text(json.dumps(valid), encoding="utf-8")
        os.utime(self.cache, (time.time() - 8 * 86400,) * 2)
        self.assertEqual("expired", discovery.discover(self.repo)["cache"]["reason"])
        self.write("CLAUDE.md", b"# Rules\n")
        self.assertNotEqual(expected, discovery.discover(self.repo)["cacheKey"])
        self.write("CLAUDE.md", b"# Rules\r\n")
        self.write("nested/CLAUDE.md", "")
        self.assertNotEqual(expected, discovery.discover(self.repo)["cacheKey"])
        (self.repo / "nested/CLAUDE.md").unlink()
        self.write("CLAUDE.local.md", "local")
        self.assertNotEqual(expected, discovery.discover(self.repo)["cacheKey"])
        self.assertNotIn("cacheKey", discovery.discover(self.repo, "copilot"))
        self.assertEqual(discovery.discover(self.repo)["cacheKey"], discovery.discover(self.repo / "nested")["cacheKey"])

    def test_native_tcp_probe(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            self.assertEqual({"open": [port], "closed": []}, discovery.dev_server([port]))
        self.assertEqual({"open": [], "closed": [port]}, discovery.dev_server([port]))
        with self.assertRaises(ValueError):
            discovery.dev_server([-1])

    def test_worktree_and_subdirectory_find_same_root(self):
        self.write("CLAUDE.md", "worktree rules\n")
        run(["git", "add", "."], cwd=self.repo, capture_output=True, check=True)
        run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "commit", "-qm", "baseline"], cwd=self.repo,
            capture_output=True, check=True)
        worktree = self.repo / "other-worktree"
        run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=self.repo, capture_output=True, check=True)
        (worktree / "nested").mkdir()
        result = discovery.discover(worktree / "nested")
        self.assertEqual(str(worktree.resolve()), result["repo"])
        self.assertEqual(discovery.discover(worktree)["cacheKey"], result["cacheKey"])

    def test_http_health_classes_and_retry_contract(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(int(self.path[1:]))
                if self.path == "/302":
                    self.send_header("Location", "/500")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            for code, health in ((200, "healthy"), (302, "healthy"), (401, "auth-gated"),
                                 (403, "auth-gated"), (500, "unhealthy")):
                with self.subTest(code=code), patch.object(discovery.time, "sleep") as sleep:
                    result = discovery._http_retries(f"{url}/{code}", 1)
                    self.assertEqual(code, result["status"])
                    self.assertEqual(health, result["health"])
                    self.assertEqual(2 if code == 500 else 0, sleep.call_count)
            self.assertEqual("healthy", discovery.http_probe(url + "/200", 1)["health"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
