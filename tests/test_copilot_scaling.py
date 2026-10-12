"""Regressions for large native Copilot packages and compact result transport."""

import io
import json
from pathlib import Path
import sys
from contextlib import redirect_stdout
from unittest.mock import patch

from test_copilot import Fixture, PACKAGE, RESULTS, INTEGRATION, atomic


class TestScaling(Fixture):
    def compact(self, dispatch, **changes):
        return {"dispatchToken": dispatch["dispatchToken"], "status": "complete",
                "allMandatoryRead": True, "missingInputs": [], "effectiveModel": None,
                "sourceFilesRead": [], "findings": [], **changes}

    def test_large_rust_plan_matches_readable_package_without_checks(self):
        for i in range(102):
            self.write(f"src/gameplay/system_{i:03d}.rs", f"pub fn update_{i}() {{}}\n")
        self.write("assets/scene.json", json.dumps({"data": "x" * 42000}))
        self.write("assets/world.json", json.dumps({"data": "y" * 64000}))
        self.git("add", "src/gameplay", "assets")
        self.request["target"] = {"kind": "staged"}
        atomic(self.request_path, self.request)
        with patch.object(PACKAGE, "checks", side_effect=AssertionError("plan ran checks")):
            preview = PACKAGE.plan(self.repo, self.request_path)
        self.assertFalse((self.repo / ".copilot/review-all/runs").exists())
        directory, manifest = self.prepare()
        self.assertEqual(104, preview["reviewableFiles"])
        self.assertEqual(len(manifest["assignments"]), preview["reviewerAssignments"])
        self.assertGreater(preview["estimatedReviewerInputTokens"], 0)
        self.assertGreater(max(p.stat().st_size for p in (directory / "assignments").glob("*.json")), 20000)
        for assignment in manifest["assignments"].values():
            raw = (directory / "assignments" / (assignment["id"] + ".json")).read_bytes()
            self.assertEqual(assignment, json.loads(raw))
            lines = raw.splitlines(keepends=True)
            # A native view's 20KB cap can be respected without shrinking chunks.
            for i in range(0, len(lines), 40):
                self.assertLess(len(b"".join(lines[i:i + 40])), 20000)
            for path in assignment["mandatory"]:
                self.assertLessEqual(Path(path).stat().st_size, PACKAGE.PART_BYTES)
        for name in ("assets/scene.json", "assets/world.json"):
            parts = manifest["sources"][name]["parts"]
            self.assertEqual((self.repo / name).read_bytes(), b"".join(Path(p).read_bytes() for p in parts))
        self.assertTrue(PACKAGE.fresh(directory))
        self.write(".copilot/review-all.json", '{"chunkMaxFiles":10}')
        smaller = PACKAGE.plan(self.repo, self.request_path)
        self.assertGreater(smaller["reviewerAssignments"], preview["reviewerAssignments"])

    def test_compact_stdin_round_trip_and_full_ledger(self):
        directory, manifest = self.prepare()
        assignment = next(iter(manifest["assignments"].values()))
        dispatch = RESULTS.start(directory, assignment["id"], "review-all")
        self.assertEqual("review-all:" + assignment["agent"], dispatch["agent"])
        payload = self.compact(dispatch, sourceFilesRead=list(assignment["sources"]))
        self.assertLess(len(json.dumps(payload)), 400)
        with patch.object(sys, "argv", ["copilot-results", "accept", str(directory),
                                       "--assignment", assignment["id"], "--stdin"]), \
                patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()):
            self.assertEqual(0, RESULTS.main())
        result = RESULTS.state(directory)["jobs"][assignment["id"]]["result"]
        self.assertEqual(payload, RESULTS.state(directory)["jobs"][assignment["id"]]["rawResult"])
        self.assertEqual(assignment["mandatory"], result["readInputs"])
        self.assertEqual(assignment["packageHash"], result["packageHash"])
        self.assertEqual(result, RESULTS.validate(result, assignment, 1))

    def test_compact_result_still_correlates_exposed_runtime_model(self):
        stream = [{"type": "assistant.message", "agentId": "child", "data": {
            "model": "wrong-model", "content": json.dumps({"dispatchToken": "token"})}}]
        errors = INTEGRATION.runtime_model_errors(stream, {"01-standards": "gpt-6-luna"},
                                                  {"token": "01-standards"})
        self.assertEqual(1, len(errors))

    def test_successful_retry_does_not_hide_first_attempt_model_mismatch(self):
        directory, manifest = self.prepare()
        assignment = next(iter(manifest["assignments"].values()))
        first = RESULTS.start(directory, assignment["id"])
        incomplete = self.compact(first, status="incomplete", allMandatoryRead=False,
                                  missingInputs=["truncated source"])
        self.assertTrue(RESULTS.accept(directory, assignment["id"],
                                      result_text=json.dumps(incomplete))["retry"])
        retry = RESULTS.start(directory, assignment["id"])
        complete = self.compact(retry, sourceFilesRead=list(assignment["sources"]))
        self.assertEqual("complete", RESULTS.accept(directory, assignment["id"],
                         result_text=json.dumps(complete))["status"])
        # Runtime identity can be exposed before the incomplete final response.
        stream = [
            {"type": "assistant.message", "agentId": "first", "data": {"model": "wrong-model"}},
            {"type": "assistant.message", "agentId": "first", "data": {"content": json.dumps(incomplete)}},
            {"type": "assistant.message", "agentId": "retry", "data": {
                "model": assignment["model"], "content": json.dumps(complete)}},
        ]
        jobs = RESULTS.state(directory)["jobs"]
        errors = INTEGRATION.runtime_model_errors(stream, {assignment["axis"]: assignment["model"]},
                                                  INTEGRATION.dispatch_token_roles(jobs))
        self.assertEqual(1, len(errors), errors)
        self.assertIn("wrong-model", errors[0])
        self.assertIn("agent first", errors[0])

    def test_token_roles_include_verifier_attempts_and_legacy_ledgers(self):
        jobs = {
            "verifier--01-standards--1": {"dispatchToken": "verify-retry",
                                         "dispatchTokens": ["verify-first", "verify-retry"]},
            "01-standards--chunk-0001": {"dispatchToken": "legacy-reviewer"},
            "02-bugs-security--chunk-0001": {"status": "pending"},
        }
        self.assertEqual({"verify-first": "verifier", "verify-retry": "verifier",
                          "legacy-reviewer": "01-standards"}, INTEGRATION.dispatch_token_roles(jobs))

    def test_retry_carries_forward_token_from_pre_history_job(self):
        directory, manifest = self.prepare()
        job_id = next(iter(manifest["assignments"]))
        original = RESULTS.start(directory, job_id)
        RESULTS.accept(directory, job_id, failure="timeout")
        ledger = RESULTS.state(directory)
        del ledger["jobs"][job_id]["dispatchTokens"]
        atomic(directory / "completion.json", ledger)
        retry = RESULTS.start(directory, job_id)
        self.assertEqual([original["dispatchToken"], retry["dispatchToken"]],
                         RESULTS.state(directory)["jobs"][job_id]["dispatchTokens"])

    def test_compact_findings_survive_independent_verification_and_finalization(self):
        directory, manifest = self.prepare()
        finding = {"id": "positive-value", "root_cause_key": "other:src/a.py:value",
                   "file": "src/a.py", "line": 1, "severity": "IMPORTANT", "confidence": "HIGH",
                   "title": "Value must be positive", "evidence": "value = 0",
                   "impact": "Violates the explicit requirement", "fix": "Use a positive value",
                   "claim_class": "static", "needs_observation": None}
        for index, assignment in enumerate(manifest["assignments"].values()):
            dispatch = RESULTS.start(directory, assignment["id"])
            payload = self.compact(dispatch, sourceFilesRead=list(assignment["sources"]),
                                   findings=[finding] if index == 0 else [])
            self.assertEqual("complete", RESULTS.accept(directory, assignment["id"],
                             result_text=json.dumps(payload))["status"])
        RESULTS.make_verifiers(directory)
        plan = json.loads((directory / "verification.json").read_text())
        assignment = next(iter(plan["assignments"].values()))
        dispatch = RESULTS.start(directory, assignment["id"], "review-all")
        self.assertEqual("review-all:review-all-verifier", dispatch["agent"])
        candidate = assignment["candidates"][0]
        judgment = {"finding_id": candidate["id"], "root_cause_key": candidate["root_cause_key"],
                    "score": 90, "verdict": "keep", "claim_class": "static", "needs_observation": None,
                    "reason": "The original source violates the explicit requirement",
                    "reread_evidence": "value = 0", "cross_confirmed_by": []}
        payload = self.compact(dispatch, sourceFilesRead=list(assignment["sources"]), findings=[judgment])
        self.assertEqual("complete", RESULTS.accept(directory, assignment["id"],
                         result_text=json.dumps(payload))["status"])
        verdict = RESULTS.finalize(directory, severity="important")
        self.assertFalse(verdict["partial"])
        self.assertFalse(verdict["pass"])
        self.assertEqual(1, verdict["blockingCount"])

    def test_compact_invalid_receipts_cannot_complete(self):
        directory, manifest = self.prepare()
        ids = list(manifest["assignments"])
        cases = [{"allMandatoryRead": False}, {"allMandatoryRead": 1},
                 {"status": "incomplete", "missingInputs": ["source"]},
                 {"dispatchToken": "another-dispatch"}, {"missingInputs": ["truncated"]},
                 {"sourceFilesRead": [123]}]
        for job_id, change in zip(ids, cases):
            dispatch = RESULTS.start(directory, job_id)
            result = RESULTS.accept(directory, job_id, result_text=json.dumps(self.compact(dispatch, **change)))
            self.assertEqual("failed", result["status"], change)
        RESULTS.make_verifiers(directory)
        self.assertTrue(RESULTS.finalize(directory)["partial"])

    def test_compact_token_cannot_be_reused_for_retry_or_other_job(self):
        directory, manifest = self.prepare()
        first, second = list(manifest["assignments"])[:2]
        original = RESULTS.start(directory, first)
        RESULTS.accept(directory, first, failure="timeout")
        retry = RESULTS.start(directory, first)
        self.assertNotEqual(original["dispatchToken"], retry["dispatchToken"])
        self.assertEqual("failed", RESULTS.accept(directory, first,
                         result_text=json.dumps(self.compact(original)))["status"])
        RESULTS.start(directory, second)
        self.assertEqual("failed", RESULTS.accept(directory, second,
                         result_text=json.dumps(self.compact(retry)))["status"])

    def test_batch_is_atomic_checks_freshness_once_and_enforces_capacity(self):
        self.write(".copilot/review-all.json", '{"maxConcurrentAgents":3}')
        directory, manifest = self.prepare()
        ids = list(manifest["assignments"])
        with self.assertRaisesRegex(ValueError, "unknown assignment"):
            RESULTS.start_many(directory, [ids[0], "missing"])
        self.assertEqual({}, RESULTS.state(directory)["jobs"])
        with patch.object(RESULTS.PACKAGE, "fresh", wraps=RESULTS.PACKAGE.fresh) as fresh:
            wave = RESULTS.start_many(directory, ids[:3], "review-all")
            self.assertEqual(1, fresh.call_count)
        self.assertEqual(ids[:3], [d["assignmentId"] for d in wave])
        with self.assertRaisesRegex(ValueError, "maxConcurrentAgents"):
            RESULTS.start(directory, ids[3])
        self.assertNotIn(ids[3], RESULTS.state(directory)["jobs"])
        for job_id in ids[:3]:
            RESULTS.accept(directory, job_id, failure="dispatch refused")
        output = io.StringIO()
        with patch.object(sys, "argv", ["copilot-results", "start", str(directory),
                                       "--assignments", ",".join(ids[:3])]), redirect_stdout(output):
            self.assertEqual(0, RESULTS.main())
        self.assertEqual(3, len([json.loads(line) for line in output.getvalue().splitlines()]))

    def test_abandon_stale_run_keeps_evidence_and_closes_all_unfinished_jobs(self):
        directory, manifest = self.prepare()
        first, second = list(manifest["assignments"])[:2]
        self.accept(directory, manifest["assignments"][first])
        RESULTS.start(directory, second)
        self.write(".copilot/review-all.json", "{}")
        with self.assertRaisesRegex(ValueError, "config was created.*prepare a new run"):
            PACKAGE.fresh(directory)
        RESULTS.abandon(directory, "config changed")
        jobs = RESULTS.state(directory)["jobs"]
        self.assertEqual("complete", jobs[first]["status"])
        self.assertTrue(all(j["status"] == "abandoned" for k, j in jobs.items() if k != first))
        (self.repo / ".copilot/review-all.json").unlink()
        for action in (lambda: RESULTS.start(directory, second), lambda: RESULTS.finalize(directory),
                       lambda: RESULTS.make_verifiers(directory)):
            with self.assertRaisesRegex(ValueError, "abandoned"):
                action()

    def test_invalid_concurrency_and_profile_rejected(self):
        for cap in (0, -1, True, "8"):
            self.write(".copilot/review-all.json", json.dumps({"maxConcurrentAgents": cap}))
            with self.assertRaisesRegex(ValueError, "positive integer"):
                self.prepare()
        self.write(".copilot/review-all.json", "{}")
        atomic(self.profile, [])
        with self.assertRaisesRegex(ValueError, "profile must be a JSON object"):
            self.prepare()
