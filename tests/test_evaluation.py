"""Harness namespaces and explicit model opt-in are preserved by the common CLI."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/review-all/scripts"))
import evaluation
from copilot_common import load_module


class Evaluation(unittest.TestCase):
    def test_timed_out_reviews_retry_and_keep_attempt_evidence(self):
        for timed_out_attempts, expected_code in ((1, 0), (2, 1)):
            attempts = []
            with self.subTest(timed_out_attempts=timed_out_attempts), tempfile.TemporaryDirectory() as temp:
                def fake_run(argv, **kwargs):
                    if "materialize-fixture.py" in str(argv[1]):
                        Path(argv[-1]).mkdir()
                        return subprocess.CompletedProcess(argv, 0, b"", b"")
                    if "input" in kwargs:
                        return subprocess.CompletedProcess(argv, 0, b"PASS\n", b"")
                    attempts.append(argv)
                    if len(attempts) <= timed_out_attempts:
                        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial report", stderr=b"partial error")
                    report = b'<!-- review-all-severity: {"CRITICAL":0,"IMPORTANT":0,"DEBT":0,"SUGGESTED":0,"QUESTION":0} -->'
                    return subprocess.CompletedProcess(argv, 0, report, b"")

                with patch.dict(os.environ, {"REVIEW_ALL_CLAUDE_EVALS": "1", "REVIEW_ALL_CONFIG_JSON": ""}), \
                     patch.object(evaluation, "run", side_effect=fake_run), \
                     redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                    self.assertEqual(expected_code, evaluation.main(["01-", "--out", temp, "--runs", "1"]))
                self.assertEqual(2, len(attempts))
                self.assertEqual(b"partial report", next(Path(temp).rglob("report-0.txt")).read_bytes())
                self.assertEqual(b"partial error", next(Path(temp).rglob("stderr-0.txt")).read_bytes())
                self.assertTrue(list(Path(temp).rglob("report-1.txt")))
                self.assertIn("ERROR (0/0; requested 1)" if expected_code else "PASS (1/1; requested 1)", output.getvalue())

    def test_claude_config_treatment_is_recorded_and_applied(self):
        headers = []
        with tempfile.TemporaryDirectory() as temp:
            for treatment in ({"verifierVotes": 1}, {"verifierVotes": 3}, None):
                config_json = json.dumps(treatment, indent=2) if treatment is not None else ""
                configs = []

                def fake_run(argv, **kwargs):
                    if "materialize-fixture.py" in str(argv[1]):
                        Path(argv[-1]).mkdir()
                        return subprocess.CompletedProcess(argv, 0, b"", b"")
                    repo = Path(kwargs["cwd"])
                    if "--effort" in argv:
                        config = repo / ".claude/review-all.json"
                        configs.append(json.loads(config.read_text()) if config.exists() else None)
                        report = b'<!-- review-all-severity: {"CRITICAL":0,"IMPORTANT":0,"DEBT":0,"SUGGESTED":0,"QUESTION":0} -->'
                        return subprocess.CompletedProcess(argv, 0, report, b"")
                    return subprocess.CompletedProcess(argv, 0, b"PASS\n", b"")

                with patch.dict(os.environ, {"REVIEW_ALL_CLAUDE_EVALS": "1", "REVIEW_ALL_CONFIG_JSON": config_json}), \
                     patch.object(evaluation, "run", side_effect=fake_run), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(0, evaluation.main(["01-", "--out", temp, "--runs", "1", "--effort", "high"]))
                header = output.getvalue().splitlines()[0]
                headers.append(header)
                self.assertEqual(config_json, json.loads(header.split(",config_json=", 1)[1]))
                self.assertEqual([treatment], configs)
                self.assertEqual(1, output.getvalue().count("CONFIG,"))
        self.assertEqual(3, len(set(headers)))

    def test_claude_skips_without_opt_in_and_rejects_no_cases(self):
        with patch.dict(os.environ, {"REVIEW_ALL_CLAUDE_EVALS": ""}), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, evaluation.main(["01"]))
        self.assertIn("SKIP", output.getvalue())
        self.assertEqual(3, evaluation.main(["not-a-fixture"]))

    def test_copilot_does_not_inherit_claude_model_or_timeout_environment(self):
        copilot = load_module("run-evals-copilot")
        with patch.dict(os.environ, {"REVIEW_ALL_EVAL_MODEL": "opus", "REVIEW_ALL_EVAL_EFFORT": "low",
                                     "REVIEW_ALL_EVAL_RUNS": "9", "REVIEW_ALL_EVAL_TIMEOUT": "2"}), \
             patch.object(evaluation, "load_module", return_value=copilot), \
             patch.object(copilot, "main", side_effect=lambda: list(sys.argv)) as main:
            argv = evaluation.main(["01", "--harness", "copilot", "--out", "evidence"])
        self.assertEqual(["run-evals-copilot", "01", "--out", "evidence"], argv)
        main.assert_called_once()
