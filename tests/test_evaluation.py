"""Harness namespaces and explicit model opt-in are preserved by the common CLI."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/review-all/scripts"))
import evaluation
from copilot_common import load_module


class Evaluation(unittest.TestCase):
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
