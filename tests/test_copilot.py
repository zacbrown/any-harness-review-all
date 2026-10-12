"""Deterministic Copilot handoff tests; no model, network, or authentication."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/review-all/scripts"
sys.path.insert(0, str(SCRIPTS))
from copilot_common import atomic, body, cli_version, digest, load_module, model_for, now, read_json, reasoning_for  # noqa: E402
from copilot_instructions import collect  # noqa: E402

PACKAGE = load_module("copilot-package")
RESULTS = load_module("copilot-results")
INSTALL = load_module("copilot-install")
GATE = load_module("copilot-gate")
EVALS = load_module("run-evals-copilot")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-all-copilot-test-")
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.env = patch.dict(os.environ, {"COPILOT_HOME": str(self.home), "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": ""})
        self.env.start()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.write("src/a.py", "value = 1\n")
        self.write("src/b.py", "value = 2\n")
        self.write("docs/c.md", "Old documentation\n")
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "baseline")
        self.write("src/a.py", "value = 0\n")
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("value must be positive\n")
        self.profile = self.root / "profile.json"
        atomic(self.profile, {"language": "python", "rules": {"global": ""}})
        self.request_path = self.root / "request.json"
        self.request = {
            "schemaVersion": 1, "target": {"kind": "worktree"},
            "scope": {"paths": [], "exclude": [], "requestedTarget": "--unstaged"},
            "requirements": str(self.requirements), "profile": str(self.profile),
            "instructions": {"mode": "enabled", "settingsConfirmed": True,
                             "disabledPaths": [], "parentSources": [], "confirmedExtraDirs": []},
            "checks": [{"name": n, "status": "N/A", "reason": "fixture"} for n in
                       ("Typecheck", "Lint", "Tests", "Spec Existence", "Dependencies")],
        }

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True).stdout

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def prepare(self):
        atomic(self.request_path, self.request)
        with patch.object(PACKAGE, "cli_version", return_value="1.0.95"):
            directory = PACKAGE.prepare(self.repo, self.request_path)
        return directory, read_json(directory / "manifest.json")

    def result(self, assignment, attempt=1, findings=None):
        return {"schemaVersion": 1, "runId": assignment["runId"], "packageHash": assignment["packageHash"],
                "assignmentId": assignment["id"], "axis": assignment["axis"], "attempt": attempt,
                "status": "complete", "chunks": assignment["chunks"], "readInputs": assignment["mandatory"],
                "missingInputs": [], "effectiveModel": None,
                "sourceFilesRead": list(assignment["sources"]), "findings": findings or []}

    def accept(self, directory, assignment, findings=None):
        dispatch = RESULTS.start(directory, assignment["id"])
        output = directory / "output.json"
        atomic(output, self.result(assignment, dispatch["attempt"], findings))
        accepted = RESULTS.accept(directory, assignment["id"], output)
        self.assertEqual("complete", accepted["status"], accepted)

    def finish_empty(self, directory, manifest):
        for assignment in manifest["assignments"].values():
            self.accept(directory, assignment)
        RESULTS.make_verifiers(directory)
        return RESULTS.finalize(directory)

    def divergent_target(self, kind):
        base = self.git("rev-parse", "HEAD").decode().strip()
        self.git("add", "src/a.py")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "reviewed revision")
        reviewed = self.git("rev-parse", "HEAD").decode().strip()
        self.write("src/b.py", "unrelated = True\n")
        self.git("add", "src/b.py")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "local checkout")
        self.request["target"] = {"kind": "range", "base": base, "head": reviewed} if kind == "range" else {
            "kind": "pr", "number": 1}
        metadata = {"baseRefOid": base, "headRefOid": reviewed, "title": "fixture", "body": ""}
        return reviewed, patch.object(PACKAGE, "run", return_value=json.dumps(metadata).encode()) if kind == "pr" else nullcontext()


class TestCopilotPackages(Fixture):
    def test_state_paths_reject_traversal_absolute_and_symlink_escapes(self):
        outside = self.root / "outside.json"
        outside.write_text("preserve me")
        link = self.repo / ".copilot/review-all/link"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.root, target_is_directory=True)
        for key in ("stateFile", "historyFile", "checkpointDir", "snoozeFile"):
            for value in (".copilot/review-all/../../../outside.json", str(outside),
                          ".copilot/review-all/link/outside.json", ".copilot/review-all"):
                with self.subTest(key=key, value=value):
                    self.write(".copilot/review-all.json", json.dumps({key: value}))
                    with self.assertRaises(ValueError):
                        self.prepare()
                    self.assertEqual("preserve me", outside.read_text())

    def test_runtime_directory_cannot_be_a_symlink_outside_namespace(self):
        (self.repo / ".copilot").mkdir()
        (self.repo / ".copilot/review-all").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertFalse((self.root / ".gitignore").exists())

    def test_secret_rename_never_serializes_diff_or_source(self):
        for old, new in ((".env", "src/env.py"), ("credentials.pem", "src/key.py"),
                         ("src/public.py", ".env.production")):
            self.write(old, f"SECRET_RENAME_SENTINEL {old}\n")
            self.git("add", old)
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "tracked secrets")
        for old, new in ((".env", "src/env.py"), ("credentials.pem", "src/key.py"),
                         ("src/public.py", ".env.production")):
            self.git("mv", old, new)
        self.request["scope"]["paths"] = ["src", ".env.production"]
        directory, manifest = self.prepare()
        excluded = {r["path"]: r["reason"] for r in manifest["selection"]["excluded"]}
        self.assertEqual({"src/env.py": "secret", "src/key.py": "secret", ".env.production": "secret"}, excluded)
        self.assertEqual(3, manifest["selection"]["counts"]["excluded"]["secret"])
        self.assertEqual(["src/a.py"], list(manifest["sources"]))
        for path in directory.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SECRET_RENAME_SENTINEL", path.read_bytes(), str(path))
        self.assertTrue(PACKAGE.fresh(directory))

    def test_symlink_snapshots_match_git_blobs_and_detect_retargeting(self):
        link = self.repo / "src/link.py"
        link.symlink_to("a.py")
        self.git("add", "src/link.py")
        directory, manifest = self.prepare()
        source = manifest["sources"]["src/link.py"]
        blob = self.git("show", ":src/link.py")
        self.assertEqual(b"a.py", blob)
        self.assertEqual(blob, Path(source["snapshotPath"]).read_bytes())
        self.assertEqual(blob, b"".join(Path(p).read_bytes() for p in source["parts"]))
        self.assertEqual(digest(blob), source["sha256"])
        self.assertTrue(PACKAGE.fresh(directory))
        link.unlink()
        link.symlink_to("b.py")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            PACKAGE.fresh(directory)

    def test_renamed_secret_cannot_be_reintroduced_as_supporting_context(self):
        self.write(".env", "SECRET_SUPPORT_SENTINEL\n")
        self.git("add", ".env")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "tracked secret")
        self.git("mv", ".env", "src/innocent.py")
        self.request["scope"]["paths"] = ["src/a.py"]
        self.request["supportingFiles"] = ["src/innocent.py"]
        with self.assertRaisesRegex(ValueError, "secret exclusion"):
            self.prepare()
        for path in (self.repo / ".copilot/review-all").rglob("*"):
            if path.is_file():
                self.assertNotIn(b"SECRET_SUPPORT_SENTINEL", path.read_bytes())

    def test_changed_symlink_snapshots_use_new_link_text(self):
        link = self.repo / "src/link.py"
        link.symlink_to("a.py")
        self.git("add", "src/link.py")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "baseline symlink")
        link.unlink()
        link.symlink_to("b.py")
        _, manifest = self.prepare()
        self.assertEqual(b"b.py", Path(manifest["sources"]["src/link.py"]["snapshotPath"]).read_bytes())

    def test_dangling_internal_symlink_and_supporting_symlink_use_link_text(self):
        (self.repo / "src/link.py").symlink_to("missing.py")
        (self.repo / "src/support.py").symlink_to("b.py")
        self.git("add", "src/link.py", "src/support.py")
        self.request["supportingFiles"] = ["src/support.py"]
        _, manifest = self.prepare()
        for name, expected in (("src/link.py", b"missing.py"), ("src/support.py", b"b.py")):
            self.assertEqual(expected, Path(manifest["sources"][name]["snapshotPath"]).read_bytes())

    def test_model_defaults_and_explicit_overrides(self):
        for axis in PACKAGE.AXES.AGENT_IDS + ["verifier", "coordinator", "judge", "deep-dive"]:
            expected = {"01-standards": "gpt-6-luna", "03-dry-smells": "gpt-6-luna",
                        "verifier": "claude-opus-5.5", "judge": "claude-opus-5.5", "deep-dive": "gpt-6-astra"}
            self.assertEqual(expected.get(axis, "gpt-6.1-sol"), model_for(axis, {}))
            self.assertEqual("medium" if axis in {"01-standards", "03-dry-smells"} else "high",
                             reasoning_for({}, axis))
        self.assertEqual("high", reasoning_for({}))
        config = {"models": {"01-standards": "gpt-5.4-mini"}, "verifierModel": "gpt-5.4",
                  "reasoningEffort": "medium"}
        self.assertEqual("gpt-5.4-mini", model_for("01-standards", config))
        self.assertEqual("gpt-5.4", model_for("verifier", config))
        self.assertEqual("gpt-6.1-sol", model_for("02-bugs-security", config))
        self.assertEqual("medium", reasoning_for(config))
        with self.assertRaisesRegex(ValueError, "reasoningEffort"):
            reasoning_for({"reasoningEffort": "invalid"})

    def test_reasoning_effort_is_bound_to_the_package_and_dispatch(self):
        self.write(".copilot/review-all.json", '{"reasoningEffort":"medium"}')
        directory, manifest = self.prepare()
        assignment = next(iter(manifest["assignments"].values()))
        self.assertEqual("medium", assignment["reasoningEffort"])
        dispatch = RESULTS.start(directory, assignment["id"])
        self.assertEqual("medium", dispatch["reasoning_effort"])
        self.write(".copilot/review-all.json", '{"reasoningEffort":"high"}')
        with self.assertRaises(ValueError):
            PACKAGE.fresh(directory)

    def test_per_role_effort_precedence_and_package_freshness(self):
        config = {"reasoningEffort": "low", "reasoningEfforts": {"01-standards": "high"}}
        self.write(".copilot/review-all.json", json.dumps(config))
        directory, manifest = self.prepare()
        for assignment in manifest["assignments"].values():
            expected = "high" if assignment["axis"] == "01-standards" else "low"
            self.assertEqual(expected, assignment["reasoningEffort"])
            self.assertEqual(expected, RESULTS.start(directory, assignment["id"])["reasoning_effort"])
        config["reasoningEfforts"]["01-standards"] = "medium"
        self.write(".copilot/review-all.json", json.dumps(config))
        with self.assertRaises(ValueError):
            PACKAGE.fresh(directory)

    def test_invalid_efforts_are_rejected_including_inactive_roles(self):
        for config in ({"reasoningEfforts": []}, {"reasoningEfforts": {"deep-dive": "invalid"}},
                       {"reasoningEffort": "invalid", "reasoningEfforts": {"coordinator": "high"}}):
            with self.subTest(config=config):
                self.write(".copilot/review-all.json", json.dumps(config))
                with self.assertRaisesRegex(ValueError, "reasoningEffort"):
                    self.prepare()

    def test_deep_dive_settings_resolve_defaults_and_repository_overrides(self):
        for config, model, effort in (({}, "gpt-6-astra", "high"),
                ({"models": {"deep-dive": "gpt-6.1-sol"}, "reasoningEffort": "low",
                  "reasoningEfforts": {"deep-dive": "medium"}}, "gpt-6.1-sol", "medium")):
            self.write(".copilot/review-all.json", json.dumps(config))
            output = io.StringIO()
            with redirect_stdout(output), patch.object(sys, "argv", ["copilot-package.py", "model-settings",
                    "--repo", str(self.repo), "--role", "deep-dive"]):
                self.assertEqual(0, PACKAGE.main())
            self.assertEqual({"role": "deep-dive", "model": model, "reasoning_effort": effort},
                             json.loads(output.getvalue()))

    def test_unsupported_cli_versions_are_explicit(self):
        with patch("copilot_common.run", return_value=b"GitHub Copilot CLI 1.0.85"):
            with self.assertRaisesRegex(ValueError, "minimum tested"):
                cli_version()

    def test_complete_package_and_canonical_selection(self):
        self.write("AGENTS.md", "Root rule.\n")
        self.write("src/AGENTS.md", "Only src.\n")
        self.write(".github/instructions/python.instructions.md", '---\napplyTo: "src/**/*.py"\n---\nPython rule.\n')
        directory, manifest = self.prepare()
        self.assertEqual(6, len(manifest["assignments"]))
        self.assertTrue(PACKAGE.fresh(directory))
        for assignment in manifest["assignments"].values():
            payload = "".join(Path(p).read_text() for p in assignment["mandatory"])
            for phrase in ("value must be positive", "Root rule.", "Only src.", "Python rule.", "3-question gate"):
                self.assertIn(phrase, payload)
            self.assertTrue(all(Path(p).is_absolute() for p in assignment["mandatory"]))
            self.assertEqual(["src/a.py"], assignment["files"])
            light = assignment["axis"] in {"01-standards", "03-dry-smells"}
            self.assertEqual("gpt-6-luna" if light else "gpt-6.1-sol", assignment["model"])
            self.assertEqual("medium" if light else "high", assignment["reasoningEffort"])

    def test_scoped_instructions_imports_and_disabled_sources(self):
        root = self.write("AGENTS.md", "Root @guide.txt\n")
        self.write("guide.txt", "Imported source\n")
        self.write("src/AGENTS.md", "Nested only\n")
        self.write("GEMINI.md", "Do not import @absent.md\n")
        module = self.write(".github/instructions/scoped.instructions.md", '---\napplyTo: "src/*.py"\n---\nScoped\n')
        self.write("REVIEW.md", "Verbatim @not-imported.md\n")
        policy = self.request["instructions"]
        entries, watched, errors = collect(self.repo, ["src/a.py", "docs/c.md"], policy)
        self.assertEqual([], errors)
        nested = next(e for e in entries if e["source"].endswith("/src/AGENTS.md"))
        self.assertEqual(["src/a.py"], nested["files"])
        self.assertEqual(["src/a.py"], next(e for e in entries if e["source"] == str(module.resolve()))["files"])
        self.assertIn(str(self.repo / "guide.txt"), watched)
        policy["disabledPaths"] = [str(root), str(module)]
        entries, watched, errors = collect(self.repo, ["src/a.py", "docs/c.md"], policy)
        self.assertNotIn(str(self.repo / "guide.txt"), watched)
        self.assertTrue(all("content" not in e for e in entries if e["status"] == "disabled"))
        policy["mode"] = "disabled"
        directory, manifest = self.prepare()
        self.assertFalse(any("content" in e for e in manifest["instructions"]))
        self.assertNotIn("Verbatim", "".join(Path(p).read_text() for p in manifest["context"]["reviewInstructions"]))
        self.assertTrue(PACKAGE.fresh(directory))

    def test_parent_sources_are_explicit_and_scoped(self):
        parent = self.root / "parent.md"
        parent.write_text("Parent-only requirement\n")
        self.request["instructions"]["parentSources"] = [{
            "path": str(parent), "boundary": str(self.root), "files": ["src/a.py"], "enabled": True}]
        _, manifest = self.prepare()
        transfer = next(i for i in manifest["instructions"] if i["origin"] == "parent-transfer")
        self.assertEqual(["src/a.py"], transfer["files"])
        self.assertIn("Parent-only", transfer["content"])

    def test_missing_instruction_is_unavailable_not_disabled(self):
        self.write("AGENTS.md", "@does-not-exist.md\n")
        directory, manifest = self.prepare()
        self.assertIn("missing instruction import", manifest["contextErrors"][0])
        self.assertTrue(PACKAGE.fresh(directory))
        assignment = next(iter(manifest["assignments"].values()))
        with self.assertRaisesRegex(ValueError, "context unavailable"):
            RESULTS.validate(self.result(assignment), assignment, 1)

    def test_unknown_instruction_settings_fail_closed(self):
        self.request["instructions"]["settingsConfirmed"] = False
        directory, manifest = self.prepare()
        self.assertTrue(manifest["contextErrors"])
        RESULTS.make_verifiers(directory)
        self.assertTrue(RESULTS.finalize(directory)["partial"])

    def test_changes_invalidate_all_inputs(self):
        for resource in ("AGENTS.md", ".github/instructions/new.instructions.md", "src/a.py", "requirements", "profile", "config"):
            with self.subTest(resource=resource):
                directory, _ = self.prepare()
                if resource == "requirements":
                    self.requirements.write_text(self.requirements.read_text() + "\nchanged")
                elif resource == "profile":
                    self.profile.write_text('{"changed":true}')
                elif resource == "config":
                    self.write(".copilot/review-all.json", "{}\n")
                else:
                    self.write(resource, "changed " + resource + "\n")
                with self.assertRaises(ValueError):
                    PACKAGE.fresh(directory)

    def test_package_tampering_and_missing_part_rejected(self):
        directory, manifest = self.prepare()
        path = Path(next(iter(manifest["assignments"].values()))["mandatory"][0])
        path.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            PACKAGE.fresh(directory)

    def test_staged_source_is_index_not_dirty_worktree(self):
        self.git("add", "src/a.py")
        self.write("src/a.py", "value = 999\n")
        self.request["target"] = {"kind": "staged"}
        directory, manifest = self.prepare()
        data = "".join(Path(p).read_text() for p in manifest["sources"]["src/a.py"]["parts"])
        self.assertEqual("value = 0\n", data)
        self.git("add", "src/a.py")
        with self.assertRaisesRegex(ValueError, "diff changed"):
            PACKAGE.fresh(directory)

    def test_checks_cannot_claim_to_test_a_different_staged_revision(self):
        self.git("add", "src/a.py")
        self.write("src/a.py", "value = 999\n")
        self.request["target"] = {"kind": "staged"}
        self.request["checks"][2] = {"name": "Tests", "command": [sys.executable, "-c", "raise SystemExit(1)"]}
        _, manifest = self.prepare()
        self.assertEqual("SKIP", manifest["checks"][2]["status"])
        self.assertIsNone(manifest["checks"][2]["exitCode"])
        self.assertFalse(manifest["checksMatchTarget"])

    def test_staged_checks_skip_untracked_sources_without_executing(self):
        self.git("add", "src/a.py")
        self.write("tests/untracked_test.py", "raise AssertionError('not in index')\n")
        self.request["target"] = {"kind": "staged"}
        marker = self.root / "check-ran"
        self.request["checks"][2] = {"name": "Tests", "command": [
            sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]}
        _, manifest = self.prepare()
        self.assertFalse(marker.exists())
        self.assertFalse(manifest["checksMatchTarget"])
        self.assertEqual("SKIP", manifest["checks"][2]["status"])
        self.assertIsNone(manifest["checks"][2]["exitCode"])

    def test_staged_checks_run_when_only_untracked_files_are_runtime_artifacts(self):
        self.git("add", "src/a.py")
        self.write(".copilot/reports/old.md", "old report\n")
        self.request["target"] = {"kind": "staged"}
        self.request["checks"][2] = {"name": "Tests", "command": [sys.executable, "-c", "print('index checked')"]}
        directory, manifest = self.prepare()
        self.assertTrue(manifest["checksMatchTarget"])
        self.assertEqual("PASS", manifest["checks"][2]["status"])
        self.assertTrue(PACKAGE.fresh(directory))

    def test_large_unicode_parts_are_lossless_and_bounded(self):
        data = ("wide: \u00e9\n" * 5000).encode()
        parts = PACKAGE.split_bytes(data, 11)
        self.assertEqual(data, b"".join(parts))
        self.assertTrue(all(len(p) <= 11 for p in parts))
        for part in parts:
            part.decode("utf-8")

    def test_no_custom_instructions_does_not_read_disabled_import(self):
        self.write("AGENTS.md", "@missing-secret\n")
        self.request["instructions"]["mode"] = "disabled"
        _, manifest = self.prepare()
        self.assertEqual([], manifest["contextErrors"])
        self.assertFalse(any("missing-secret" in k for k in manifest["watched"]))

    def test_rename_and_deleted_source(self):
        self.git("mv", "src/b.py", "src/renamed.py")
        (self.repo / "docs/c.md").unlink()
        directory, manifest = self.prepare()
        types = {f["path"]: f["status"] for f in manifest["selection"]["reviewable"]}
        self.assertEqual("R", types["src/renamed.py"])
        self.assertEqual("D", types["docs/c.md"])
        self.assertTrue(manifest["sources"]["docs/c.md"]["deleted"])
        self.assertTrue(PACKAGE.fresh(directory))

    def test_unstaged_deletion_reads_the_index_baseline(self):
        self.write("src/new.py", "staged_new = True\n")
        self.git("add", "src/new.py")
        (self.repo / "src/new.py").unlink()
        self.request["target"] = {"kind": "unstaged"}
        _, manifest = self.prepare()
        source = manifest["sources"]["src/new.py"]
        self.assertEqual("index", source["revision"])
        self.assertEqual("staged_new = True\n", "".join(Path(p).read_text() for p in source["parts"]))

    def test_canonical_chunks_and_full_byte_coverage(self):
        self.write("src/b.py", "value = 20\n")
        self.write("docs/c.md", "New docs\n")
        self.write(".copilot/review-all.json", '{"chunkMaxFiles":2,"chunkMaxBytes":120}')
        _, manifest = self.prepare()
        first_axis = [a for a in manifest["assignments"].values() if a["axis"] == "01-standards"]
        second_axis = [a for a in manifest["assignments"].values() if a["axis"] == "02-bugs-security"]
        self.assertEqual([a["diffParts"] for a in first_axis], [a["diffParts"] for a in second_axis])
        self.assertEqual(len(manifest["canonicalChunks"]), len(first_axis))
        parts = [Path(p).read_bytes() for a in first_axis for p in a["mandatory"] if "-diff-" in p]
        self.assertEqual(manifest["diffSha256"], digest(b"".join(parts)))

    def test_check_evidence_is_executed_not_claimed(self):
        self.request["checks"][2] = {"name": "Tests", "command": [sys.executable, "-c", "print('FAILED value must be 13'); raise SystemExit(7)"]}
        directory, manifest = self.prepare()
        check = manifest["checks"][2]
        self.assertEqual(7, check["exitCode"])
        self.assertEqual("FAIL", check["status"])
        self.assertIn("FAILED", Path(check["output"][0]).read_text())
        verdict = self.finish_empty(directory, manifest)
        self.assertFalse(verdict["pass"])
        self.assertEqual("VERIFIED", verdict["blocking"][0]["confidence"])

    def test_input_mutation_by_check_aborts(self):
        self.request["checks"][2] = {"name": "Tests", "command": [
            sys.executable, "-c", "from pathlib import Path; Path('src/a.py').write_text('mutated')"]}
        with self.assertRaisesRegex(ValueError, "changed while checks"):
            self.prepare()


class TestCopilotResults(Fixture):
    def assert_reviewed_identity(self, kind):
        reviewed, mock_pr = self.divergent_target(kind)
        local_head = self.git("rev-parse", "HEAD").decode().strip()
        with mock_pr:
            directory, manifest, _ = self.prepare_vote(votes=1)
        self.assertEqual(reviewed, manifest["head"])
        self.assertEqual(local_head, manifest["localHead"])
        self.assertNotEqual(reviewed, local_head)
        self.assertTrue(PACKAGE.fresh(directory))
        verdict = RESULTS.finalize(directory)
        self.assertEqual(reviewed, verdict["reviewedSha"])
        self.assertEqual(1, GATE.validate_verdict(directory, manifest["runId"], manifest["createdAt"], "--unstaged"))
        RESULTS.persist_state(directory)
        lifecycle = read_json(self.repo / ".copilot/review-all/state.json")["findings"][self.candidate()["root_cause_key"]]
        self.assertEqual(reviewed, lifecycle["first_seen_sha"])
        self.assertEqual(reviewed, lifecycle["last_seen_sha"])

    def test_range_identity_uses_resolved_head_in_verdict_and_lifecycle(self):
        self.assert_reviewed_identity("range")

    def test_pr_identity_uses_resolved_head_in_verdict_and_lifecycle(self):
        self.assert_reviewed_identity("pr")

    def test_local_head_change_invalidates_historical_review(self):
        _, mock_pr = self.divergent_target("range")
        with mock_pr:
            directory, _ = self.prepare()
        self.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "new checkout commit")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            PACKAGE.fresh(directory)

    def assert_supporting_context(self, kind):
        self.write("support/limits.py", "threshold = 1\n")
        self.git("add", "support/limits.py")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "supporting baseline")
        self.write("support/AGENTS.md", "Supporting scope rule.\n")
        self.request["supportingFiles"] = ["support/limits.py"]
        if kind == "staged":
            self.git("add", "src/a.py")
            self.request["target"] = {"kind": "staged"}
        else:
            self.divergent_target("range")
        self.write("support/limits.py", "threshold = 999\n")
        directory, _, first = self.prepare_vote(votes=2)
        RESULTS.make_verifiers(directory, votes=True)
        plan = read_json(directory / "verification.json")
        self.assertEqual(2, len(plan["assignments"]))
        for verifier in plan["assignments"].values():
            source = verifier["sources"]["support/limits.py"]
            self.assertEqual("threshold = 1\n", "".join(Path(p).read_text() for p in source["parts"]))
            self.assertTrue(set(source["parts"]).issubset(verifier["mandatory"]))
            instruction_paths = [p for p in verifier["mandatory"] if "-instructions." in p]
            scoped = json.loads("".join(Path(p).read_text() for p in instruction_paths))
            rule = next(i for i in scoped if i["source"].endswith("/support/AGENTS.md"))
            self.assertEqual(["support/limits.py"], rule["files"])
            self.assertIn("Supporting scope rule.", rule["content"])
            result = self.result(verifier, findings=[self.judgment(verifier)])
            result["readInputs"] = [p for p in result["readInputs"] if p not in source["parts"]]
            with self.assertRaisesRegex(ValueError, "read coverage"):
                RESULTS.validate(result, verifier, 1)
        second = next(a for a in plan["assignments"].values() if a["vote"] == 2)
        self.accept(directory, second, [self.judgment(second)])
        self.assertFalse(RESULTS.finalize(directory)["partial"])

    def test_staged_verifiers_and_votes_receive_supporting_snapshots_and_rules(self):
        self.assert_supporting_context("staged")

    def test_historical_verifiers_and_votes_receive_supporting_snapshots_and_rules(self):
        self.assert_supporting_context("range")

    def test_nested_state_paths_persist_inside_namespace(self):
        config = {"stateFile": ".copilot/review-all/custom/state.json",
                  "historyFile": ".copilot/review-all/custom/history.jsonl"}
        self.write(".copilot/review-all.json", json.dumps(config))
        directory, manifest = self.prepare()
        self.finish_empty(directory, manifest)
        self.assertTrue(RESULTS.persist_state(directory)["persisted"])
        self.assertEqual("copilot", read_json(self.repo / config["stateFile"])["harness"])
        self.assertTrue((self.repo / config["historyFile"]).is_file())

    def test_persistence_rechecks_state_namespace_after_symlink_change(self):
        self.write(".copilot/review-all.json", json.dumps({
            "stateFile": ".copilot/review-all/custom/state.json",
            "historyFile": ".copilot/review-all/custom/history.jsonl"}))
        directory, manifest = self.prepare()
        self.finish_empty(directory, manifest)
        outside = self.root / "outside"
        outside.mkdir()
        (self.repo / ".copilot/review-all/custom").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            RESULTS.persist_state(directory)
        self.assertEqual([], list(outside.iterdir()))

    def test_concurrent_completions_do_not_lose_receipts(self):
        directory, manifest = self.prepare()
        work = list(manifest["assignments"].values())

        def complete(assignment):
            dispatch = RESULTS.start(directory, assignment["id"])
            self.assertEqual(assignment["model"], dispatch["model"])
            self.assertEqual(assignment["reasoningEffort"], dispatch["reasoning_effort"])
            output = directory / (assignment["id"] + "-result.json")
            atomic(output, self.result(assignment, dispatch["attempt"]))
            return RESULTS.accept(directory, assignment["id"], output)
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertTrue(all(r["status"] == "complete" for r in pool.map(complete, work)))
        self.assertEqual(len(work), len(RESULTS.state(directory)["jobs"]))

    def candidate(self):
        return {"id": "f1", "root_cause_key": "other:src/a.py:value", "file": "src/a.py", "line": 1,
                "severity": "CRITICAL", "confidence": "HIGH", "title": "Wrong literal",
                "evidence": "value = 0", "impact": "Contract violation", "fix": "Use 1",
                "claim_class": "static", "needs_observation": None}

    def judgment(self, assignment, verdict="keep"):
        candidate = assignment["candidates"][0]
        return {"finding_id": candidate["id"], "root_cause_key": candidate["root_cause_key"],
                "score": 90 if verdict in ("keep", "unverified") else 25, "verdict": verdict,
                "claim_class": "runtime" if verdict == "unverified" else "static",
                "needs_observation": "execute the function" if verdict == "unverified" else None,
                "reason": "Checked original source", "reread_evidence": "value = 0", "cross_confirmed_by": []}

    def prepare_vote(self, votes=2):
        self.write(".copilot/review-all.json", json.dumps({"verifierVotes": votes}))
        directory, manifest = self.prepare()
        for index, assignment in enumerate(manifest["assignments"].values()):
            self.accept(directory, assignment, [self.candidate()] if index == 0 else [])
        RESULTS.make_verifiers(directory)
        plan = read_json(directory / "verification.json")
        first = next(iter(plan["assignments"].values()))
        self.assertEqual("claude-opus-5.5", first["model"])
        self.assertEqual("high", first["reasoningEffort"])
        self.accept(directory, first, [self.judgment(first)])
        return directory, manifest, first

    def test_independent_votes_tie_and_missing_coverage(self):
        directory, _, first = self.prepare_vote()
        self.assertTrue(RESULTS.finalize(directory)["partial"])
        RESULTS.make_verifiers(directory, votes=True)
        plan = read_json(directory / "verification.json")
        second = next(a for a in plan["assignments"].values() if a["vote"] == 2)
        self.assertEqual("claude-opus-5.5", second["model"])
        self.assertEqual("high", second["reasoningEffort"])
        self.assertNotIn('"score"', json.dumps(second))
        self.assertNotIn('"verdict"', json.dumps(second))
        self.assertEqual(first["candidates"], second["candidates"])
        self.assertTrue(RESULTS.finalize(directory)["partial"])
        self.accept(directory, second, [self.judgment(second, "drop")])
        verdict = RESULTS.finalize(directory)
        self.assertFalse(verdict["partial"])
        self.assertTrue(verdict["pass"])
        self.assertEqual("appendix", read_json(directory / "adjudicated.json")[0]["verdict"])

    def test_verifier_role_overrides_reach_all_votes_and_dispatches(self):
        self.write(".copilot/review-all.json", json.dumps({
            "verifierVotes": 3, "models": {"verifier": "gpt-6-astra"}, "verifierModel": "gpt-6.1-sol",
            "reasoningEffort": "low", "reasoningEfforts": {"verifier": "high"}}))
        directory, manifest = self.prepare()
        for i, assignment in enumerate(manifest["assignments"].values()):
            self.accept(directory, assignment, [self.candidate()] if i == 0 else [])
        RESULTS.make_verifiers(directory)
        first = next(iter(read_json(directory / "verification.json")["assignments"].values()))
        self.accept(directory, first, [self.judgment(first)])
        RESULTS.make_verifiers(directory, votes=True)
        votes = read_json(directory / "verification.json")["assignments"]
        self.assertEqual(3, len(votes))
        for assignment in votes.values():
            self.assertEqual("gpt-6.1-sol", assignment["model"])
            self.assertEqual("high", assignment["reasoningEffort"])
            if assignment["vote"] > 1:
                dispatch = RESULTS.start(directory, assignment["id"])
                self.assertEqual("gpt-6.1-sol", dispatch["model"])
                self.assertEqual("high", dispatch["reasoning_effort"])
    def test_any_unverified_vote_never_blocks(self):
        directory, _, _ = self.prepare_vote()
        RESULTS.make_verifiers(directory, votes=True)
        second = next(a for a in read_json(directory / "verification.json")["assignments"].values() if a["vote"] == 2)
        self.accept(directory, second, [self.judgment(second, "unverified")])
        self.assertTrue(RESULTS.finalize(directory)["pass"])
        finding = read_json(directory / "adjudicated.json")[0]
        self.assertEqual("unverified", finding["verdict"])
        self.assertEqual("QUESTION", finding["severity"])
        self.assertEqual("execute the function", finding["needs_observation"])

    def test_human_dismissals_outside_prompt_digest_still_apply(self):
        dismissed = {}
        for n in range(31):
            dismissed[f"other:src/a.py:old-{n}"] = {
                "status": "wontfix", "file_line": "src/a.py:1", "severity": "CRITICAL",
                "code_hash": RESULTS.CODE_HASH.anchored(self.repo, "src/a.py:1"),
                "last_seen_at": f"2026-09-{n + 1:02d}"}
        key = self.candidate()["root_cause_key"]
        dismissed[key] = {**dismissed["other:src/a.py:old-0"], "last_seen_at": "2025-01-01"}
        self.write(".copilot/review-all/state.json", json.dumps({"harness": "copilot", "findings": dismissed}))
        directory, manifest = self.prepare()
        prompt_digest = json.loads("".join(Path(p).read_text() for p in manifest["context"]["dismissals"]))
        self.assertEqual(30, len(prompt_digest))
        self.assertNotIn(key, [d["root_cause_key"] for d in prompt_digest])
        assignment = next(iter(manifest["assignments"].values()))
        self.accept(directory, assignment, [self.candidate()])
        self.assertEqual([], RESULTS.candidates(directory, manifest)["kept"])

    def test_lifecycle_is_copilot_only_and_persisted_once(self):
        directory, _, _ = self.prepare_vote(votes=1)
        RESULTS.finalize(directory)
        self.assertTrue(RESULTS.persist_state(directory)["persisted"])
        state = read_json(self.repo / ".copilot/review-all/state.json")
        self.assertEqual("copilot", state["harness"])
        self.assertEqual("open", state["findings"][self.candidate()["root_cause_key"]]["status"])
        self.assertFalse(RESULTS.persist_state(directory)["persisted"])
        self.assertFalse((self.repo / ".claude").exists())

    def test_headless_main_returns_verified_codes_not_cli_success(self):
        for expected in (0, 1, 2):
            with self.subTest(code=expected):
                with patch.object(GATE, "cli_version", return_value="1.0.95"), \
                     patch.object(GATE, "launch", return_value=subprocess.CompletedProcess([], 0)), \
                     patch.object(GATE, "validate_verdict", side_effect=ValueError("stale") if expected == 2 else None,
                                  return_value=expected), \
                     patch.object(GATE.RESULTS, "persist_state"), \
                     patch.object(sys, "argv", ["copilot-gate.py", "--repo", str(self.repo), "--allow-tools", "--", "--staged"]):
                    self.assertEqual(expected, GATE.main())

    def test_headless_model_and_reasoning_flags_have_defaults_and_overrides(self):
        cases = [
            ({}, [], "gpt-6.1-sol", "high"),
            ({"models": {"coordinator": "gpt-5.4"}, "reasoningEffort": "low"}, [], "gpt-5.4", "low"),
            ({"models": {"coordinator": "gpt-5.4"}, "reasoningEffort": "low"},
             ["--model", "gpt-5.4-mini", "--reasoning-effort", "medium"], "gpt-5.4-mini", "medium"),
        ]
        for config, extra_args, model, effort in cases:
            with self.subTest(model=model, effort=effort):
                self.write(".copilot/review-all.json", json.dumps(config))
                with patch.object(GATE, "cli_version", return_value="1.0.95"), \
                     patch.object(GATE, "launch", return_value=subprocess.CompletedProcess([], 0)) as launch, \
                     patch.object(GATE, "validate_verdict", return_value=0), \
                     patch.object(GATE.RESULTS, "persist_state"), \
                     patch.object(sys, "argv", ["copilot-gate.py", "--repo", str(self.repo), "--allow-tools"] + extra_args):
                    self.assertEqual(0, GATE.main())
                command = launch.call_args.args[0]
                self.assertEqual(model, command[command.index("--model") + 1])
                self.assertEqual(effort, command[command.index("--reasoning-effort") + 1])

    def test_eval_coordinator_and_judge_use_default_model_and_effort(self):
        report = subprocess.CompletedProcess([], 0, stdout=b"review report", stderr=b"")
        judgment = subprocess.CompletedProcess([], 0, stdout=b"PASS\n", stderr=b"")
        with patch.dict(os.environ, {"REVIEW_ALL_COPILOT_EVALS": "1", "REVIEW_ALL_COPILOT_CONFIG_JSON": "{}"}), \
             patch.object(EVALS, "cli_version", return_value="1.0.95"), \
             patch.object(EVALS, "run"), patch.object(EVALS.INSTALL, "install"), \
             patch.object(EVALS.TALLY, "tally", return_value=([0, 0, 0, 0, 0], "comment")), \
             patch.object(EVALS.subprocess, "run", side_effect=[report, judgment]) as launch, \
             patch.object(sys, "argv", ["run-evals-copilot.py", "01", "--out", str(self.root / "evals")]):
            self.assertEqual(0, EVALS.main())
        self.assertEqual(2, len(launch.call_args_list))
        for call, model in zip(launch.call_args_list, ("gpt-6.1-sol", "claude-opus-5.5")):
            command = call.args[0]
            self.assertEqual(model, command[command.index("--model") + 1])
            self.assertEqual("high", command[command.index("--reasoning-effort") + 1])

    def test_eval_judge_overrides_are_independent_and_recorded(self):
        config = {"models": {"coordinator": "gpt-6-luna", "judge": "gpt-6-astra"},
                  "reasoningEfforts": {"coordinator": "low", "judge": "high"}}
        scenarios = [([], ("gpt-6-luna", "low"), ("gpt-6-astra", "high")),
                     (["--model", "gpt-6.1-sol", "--reasoning-effort", "medium"],
                      ("gpt-6.1-sol", "medium"), ("gpt-6-astra", "high")),
                     (["--judge-model", "claude-opus-5.5", "--judge-reasoning-effort", "medium"],
                      ("gpt-6-luna", "low"), ("claude-opus-5.5", "medium"))]
        for i, (flags, coordinator, judge) in enumerate(scenarios):
            out = self.root / f"eval-{i}"
            with self.subTest(flags=flags), \
                 patch.dict(os.environ, {"REVIEW_ALL_COPILOT_EVALS": "1",
                                         "REVIEW_ALL_COPILOT_CONFIG_JSON": json.dumps(config)}), \
                 patch.object(EVALS, "cli_version", return_value="1.0.95"), patch.object(EVALS, "run"), \
                 patch.object(EVALS.INSTALL, "install") as install, \
                 patch.object(EVALS.TALLY, "tally", return_value=([0, 0, 0, 0, 0], "comment")), \
                 patch.object(EVALS.subprocess, "run", side_effect=[
                     subprocess.CompletedProcess([], 0, stdout=b"report", stderr=b""),
                     subprocess.CompletedProcess([], 0, stdout=b"PASS\n", stderr=b"")]) as launch, \
                 patch.object(sys, "argv", ["run-evals-copilot.py", "01", "--out", str(out)] + flags):
                self.assertEqual(0, EVALS.main())
            for call, expected in zip(launch.call_args_list, (coordinator, judge)):
                command = call.args[0]
                self.assertEqual(expected, (command[command.index("--model") + 1],
                                            command[command.index("--reasoning-effort") + 1]))
            self.assertEqual(config, install.call_args.args[1])
            evidence = read_json(next(out.glob("*/model-settings.json")))
            self.assertEqual({"model": judge[0], "reasoningEffort": judge[1]}, evidence["judge"])
            self.assertEqual(config, read_json(next(out.glob("*/repo/.copilot/review-all.json"))))

    def test_eval_rejects_automatic_judge_before_launch(self):
        with patch.dict(os.environ, {"REVIEW_ALL_COPILOT_EVALS": "1",
                                     "REVIEW_ALL_COPILOT_CONFIG_JSON": "{}"}), \
             patch.object(EVALS, "cli_version") as version, \
             patch.object(sys, "argv", ["run-evals-copilot.py", "01", "--out", str(self.root),
                                        "--judge-model", "auto"]), self.assertRaises(SystemExit) as error:
            EVALS.main()
        self.assertEqual(2, error.exception.code)
        version.assert_not_called()

    def test_empty_completed_results_pass_and_missing_chunks_block(self):
        directory, manifest = self.prepare()
        RESULTS.make_verifiers(directory)
        verdict = RESULTS.finalize(directory)
        self.assertTrue(verdict["partial"])
        self.assertFalse(verdict["pass"])
        (directory / "verification.json").unlink()
        verdict = self.finish_empty(directory, manifest)
        self.assertFalse(verdict["partial"])
        self.assertTrue(verdict["pass"])
        self.assertFalse(manifest["runtime"]["reuseEnabled"])

    def test_bad_result_retries_once_and_never_becomes_empty(self):
        directory, manifest = self.prepare()
        assignment = next(iter(manifest["assignments"].values()))
        output = directory / "bad.json"
        for attempt in (1, 2):
            dispatch = RESULTS.start(directory, assignment["id"])
            self.assertEqual(attempt, dispatch["attempt"])
            output.write_text("not JSON")
            status = RESULTS.accept(directory, assignment["id"], output)
            self.assertEqual("failed", status["status"])
            self.assertEqual(attempt == 1, status["retry"])
        with self.assertRaisesRegex(ValueError, "exhausted"):
            RESULTS.start(directory, assignment["id"])
        RESULTS.make_verifiers(directory)
        self.assertTrue(RESULTS.finalize(directory)["partial"])

    def test_run_axis_coverage_and_read_receipts_are_validated(self):
        _, manifest = self.prepare()
        assignment = next(iter(manifest["assignments"].values()))
        for key, value in (("runId", "old"), ("axis", "unknown"), ("chunks", []), ("readInputs", []),
                           ("findings", {}), ("attempt", 2), ("missingInputs", ["source"])):
            result = self.result(assignment)
            result[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                RESULTS.validate(result, assignment, 1)
        result = self.result(assignment)
        result["readInputs"] = result["readInputs"] + [str(self.repo / "AGENTS.md")]
        self.assertEqual(result, RESULTS.validate(result, assignment, 1))

    def test_fresh_verifier_payload_and_citation_validation(self):
        directory, manifest = self.prepare()
        finding = {"id": "one", "root_cause_key": "other:src/a.py:value", "file": "src/a.py", "line": 1,
                   "severity": "CRITICAL", "confidence": "HIGH", "title": "Nonpositive value",
                   "evidence": "value = 0", "impact": "Violates requirement", "fix": "Use a positive value",
                   "claim_class": "static", "needs_observation": None, "exploratory_reasoning": "PRIVATE"}
        for i, assignment in enumerate(manifest["assignments"].values()):
            self.accept(directory, assignment, [finding] if i < 2 else [])
        RESULTS.make_verifiers(directory)
        plan = read_json(directory / "verification.json")
        self.assertEqual(1, len(plan["assignments"]))
        assignment = next(iter(plan["assignments"].values()))
        self.assertNotIn("PRIVATE", json.dumps(assignment))
        candidate = assignment["candidates"][0]
        self.assertEqual(2, candidate["corroborating_agents"])
        judgment = {"finding_id": candidate["id"], "root_cause_key": candidate["root_cause_key"],
                    "score": 90, "verdict": "keep", "claim_class": "static", "needs_observation": None,
                    "reason": "Reread source violates the explicit positive-value requirement",
                    "reread_evidence": "value = 0", "cross_confirmed_by": []}
        bad = self.result(assignment, findings=[{**judgment, "reread_evidence": "fabricated"}])
        with self.assertRaisesRegex(ValueError, "original source"):
            RESULTS.validate(bad, assignment, 1)
        self.accept(directory, assignment, [judgment])
        verdict = RESULTS.finalize(directory)
        self.assertFalse(verdict["pass"])
        self.assertFalse(verdict["partial"])
        self.assertEqual(1, verdict["blockingCount"])

    def test_wrapper_rejects_stale_mismatched_and_fabricated_verdicts(self):
        started = now()
        directory, manifest = self.prepare()
        verdict = self.finish_empty(directory, manifest)
        self.assertEqual(0, GATE.validate_verdict(directory, manifest["runId"], started, "--unstaged"))
        for field, value in (("runId", "stale"), ("generated_at", "2000-01-01"), ("pass", False)):
            atomic(directory / "gate-verdict.json", {**verdict, field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                GATE.validate_verdict(directory, manifest["runId"], started, "--unstaged")
        atomic(directory / "gate-verdict.json", verdict)
        with self.assertRaisesRegex(ValueError, "target"):
            GATE.validate_verdict(directory, manifest["runId"], started, "vs main")
        (directory / "gate-verdict.json").unlink()
        with self.assertRaises(OSError):
            GATE.validate_verdict(directory, manifest["runId"], started, "--unstaged")


class TestCopilotInstallation(Fixture):
    def test_owned_installation_never_overwrites_user_files(self):
        destination = self.root / "project/.github"
        unrelated = destination / "agents/my-agent.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("mine")
        INSTALL.install(destination)
        profiles = list((destination / "agents").glob("review-all-*.agent.md"))
        self.assertEqual(12, len(profiles))
        for profile in profiles:
            text = profile.read_text()
            self.assertIn("include-custom-instructions: true", text)
            self.assertLessEqual(len(body(text)), 30000)
            header = text.split("---", 2)[1]
            light = profile.stem in {"review-all-standards.agent", "review-all-dry-smells.agent"}
            model = "gpt-6-luna" if light else "gpt-6.1-sol"
            if profile.name == "review-all-verifier.agent.md":
                model = "claude-opus-5.5"
            elif profile.name == "review-all-deep-dive.agent.md":
                model = "gpt-6-astra"
            self.assertIn(f'model: "{model}"', header)
            effort = "medium" if light else "high"
            self.assertIn(f'reasoningEffort: "{effort}"', header)
            self.assertIn('modelPolicy: "required"', header)
            self.assertNotIn('"edit"', header)
            self.assertNotIn('"agent"', header)
            if "consistency-history" not in profile.name:
                self.assertNotIn('"execute"', header)
        skill = destination / "skills/review-all/SKILL.md"
        self.assertIn("Native Copilot", skill.read_text())
        INSTALL.install(destination)
        skill.write_text("local modifications")
        with self.assertRaisesRegex(ValueError, "locally modified"):
            INSTALL.install(destination)
        self.assertEqual(1, INSTALL.uninstall(destination))
        self.assertEqual("local modifications", skill.read_text())
        self.assertEqual("mine", unrelated.read_text())

    def test_collision_is_preflighted_before_writes(self):
        destination = self.root / "project/.github"
        conflict = destination / "agents/review-all-verifier.agent.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("not ours")
        with self.assertRaisesRegex(ValueError, "unowned"):
            INSTALL.install(destination)
        self.assertFalse((destination / "skills").exists())
        self.assertEqual("not ours", conflict.read_text())

    def test_generated_profile_preserves_explicit_overrides(self):
        files = INSTALL.assemble({"models": {"01-standards": "gpt-5.4-mini"}, "verifierModel": "gpt-5.4",
                                  "reasoningEffort": "low", "reasoningEfforts": {"01-standards": "medium",
                                                                                              "verifier": "high"}})
        for axis, model, effort in (("standards", "gpt-5.4-mini", "medium"), ("verifier", "gpt-5.4", "high"),
                                     ("deep-dive", "gpt-6-astra", "low")):
            header = files[f"agents/review-all-{axis}.agent.md"].decode().split("---", 2)[1]
            self.assertIn(f'model: "{model}"', header)
            self.assertIn(f'reasoningEffort: "{effort}"', header)
        deep_dive = files["agents/review-all-deep-dive.agent.md"].decode()
        self.assertNotIn("Return ONLY one JSON object", deep_dive)
        self.assertIn("New findings or proposed fixes must", deep_dive)


if __name__ == "__main__":
    unittest.main()
