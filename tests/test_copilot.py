"""Deterministic Copilot handoff tests; no model, network, or authentication."""

from concurrent.futures import ThreadPoolExecutor
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


class TestCopilotPackages(Fixture):
    def test_model_defaults_and_explicit_overrides(self):
        for axis in PACKAGE.AXES.AGENT_IDS + ["verifier", "coordinator"]:
            self.assertEqual("gpt-6.1-sol", model_for(axis, {}))
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
            self.assertEqual("gpt-6.1-sol", assignment["model"])
            self.assertEqual("high", assignment["reasoningEffort"])

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
    def test_concurrent_completions_do_not_lose_receipts(self):
        directory, manifest = self.prepare()
        work = list(manifest["assignments"].values())

        def complete(assignment):
            dispatch = RESULTS.start(directory, assignment["id"])
            self.assertEqual("gpt-6.1-sol", dispatch["model"])
            self.assertEqual("high", dispatch["reasoning_effort"])
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
        self.assertEqual("gpt-6.1-sol", first["model"])
        self.assertEqual("high", first["reasoningEffort"])
        self.accept(directory, first, [self.judgment(first)])
        return directory, manifest, first

    def test_independent_votes_tie_and_missing_coverage(self):
        directory, _, first = self.prepare_vote()
        self.assertTrue(RESULTS.finalize(directory)["partial"])
        RESULTS.make_verifiers(directory, votes=True)
        plan = read_json(directory / "verification.json")
        second = next(a for a in plan["assignments"].values() if a["vote"] == 2)
        self.assertEqual("gpt-6.1-sol", second["model"])
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
        with patch.dict(os.environ, {"REVIEW_ALL_COPILOT_EVALS": "1"}), \
             patch.object(EVALS, "cli_version", return_value="1.0.95"), \
             patch.object(EVALS, "run"), patch.object(EVALS.INSTALL, "install"), \
             patch.object(EVALS.TALLY, "tally", return_value=([0, 0, 0, 0, 0], "comment")), \
             patch.object(EVALS.subprocess, "run", side_effect=[report, judgment]) as launch, \
             patch.object(sys, "argv", ["run-evals-copilot.py", "01", "--out", str(self.root / "evals")]):
            self.assertEqual(0, EVALS.main())
        for call in launch.call_args_list:
            command = call.args[0]
            self.assertEqual("gpt-6.1-sol", command[command.index("--model") + 1])
            self.assertEqual("high", command[command.index("--reasoning-effort") + 1])

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
        self.assertEqual(11, len(profiles))
        for profile in profiles:
            text = profile.read_text()
            self.assertIn("include-custom-instructions: true", text)
            self.assertLessEqual(len(body(text)), 30000)
            header = text.split("---", 2)[1]
            self.assertIn('model: "gpt-6.1-sol"', header)
            self.assertIn('reasoning-effort: "high"', header)
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
                                  "reasoningEffort": "medium"})
        for axis, model in (("standards", "gpt-5.4-mini"), ("verifier", "gpt-5.4")):
            header = files[f"agents/review-all-{axis}.agent.md"].decode().split("---", 2)[1]
            self.assertIn(f'model: "{model}"', header)
            self.assertIn('reasoning-effort: "medium"', header)


if __name__ == "__main__":
    unittest.main()
