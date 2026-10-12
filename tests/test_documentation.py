"""Portable equivalents of all previous release-documentation gates."""

import json
import os
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/review-all"
sys.path.insert(0, str(SKILL / "scripts"))
from copilot_common import load_module


def read(path):
    return path.read_text(encoding="utf-8")


class Documentation(unittest.TestCase):
    def test_prose_invariants(self):
        assertions = json.loads(read(ROOT / "tests/documentation-invariants.json"))
        self.assertGreater(len(assertions), 150)
        for item in assertions:
            with self.subTest(gate=item["gate"], label=item["label"]):
                self.assertIsNotNone(re.search(item["pattern"], read(ROOT / item["path"]), re.I | re.M),
                                     item["path"] + ": " + item["label"])

    def test_config_sets_match(self):
        schema = read(SKILL / "SKILL.md").split("```jsonc\n", 1)[1].split("```", 1)[0]
        keys = set(re.findall(r'^\s*"(\w+)"\s*:', schema, re.M))
        table = set(re.findall(r'^\| *`(\w+)` *\|', read(SKILL / "references/config-keys.md"), re.M))
        self.assertTrue(keys)
        self.assertEqual(keys, table)

    def test_models_and_spawn_sites(self):
        frontmatter = {}
        for path in (SKILL / "agents").glob("[0-9][0-9]-*.md"):
            header = read(path).split("---", 2)[1]
            model = re.search(r"^model:\s*(\w+)", header, re.M)
            self.assertIsNotNone(model, path)
            self.assertIn(model[1], {"opus", "sonnet"})
            frontmatter[path.name] = model[1]
        table = dict(re.findall(r"\| *`(\d\d[^`]*\.md)` *\| *`([a-z]+)` *\|",
                                read(SKILL / "references/phase-2-agents.md")))
        self.assertTrue(frontmatter)
        self.assertEqual(frontmatter, table)
        menu = read(SKILL / "references/phase-4-menu.md")
        spawns = re.findall(r"Spawn [A-Za-z ]+agent[^\n]*", menu)
        self.assertGreaterEqual(len(spawns), 3)
        self.assertTrue(all(re.search(r"`model: (haiku|sonnet|opus)`", s) for s in spawns))
        self.assertNotIn("inherits the session tier", menu.lower())
        row = re.search(r"^\| *`verifierModel`.*", read(SKILL / "references/config-keys.md"), re.M)[0]
        for tier in ("haiku", "sonnet", "opus"):
            self.assertIn(f'"{tier}"', row)
        self.assertNotRegex(row, r"(?i)choices[^|]*inherit")

    def test_selection_contracts(self):
        axes = load_module("select-agents")
        self.assertEqual(set(axes.AGENT_IDS), {p.stem for p in (SKILL / "agents").glob("[0-9][0-9]-*.md")})
        classes = json.loads(read(SKILL / "scripts/file-classes.json"))
        assigned = {c for values in classes["classes"].values() for c in values}
        documented = set(re.findall(r"class `([a-z0-9]+)`", read(SKILL / "references/phase-2-agents.md")))
        self.assertTrue(documented)
        self.assertTrue(documented <= assigned)
        for pattern in classes["exclude"]:
            self.assertNotRegex(pattern, re.compile(r"test|spec|\bTest\b|\.md\b|\.mdx\b", re.I))
        for pack in classes["rule_packs"].values():
            self.assertTrue((SKILL / "rules" / pack).is_file(), pack)
        selection = load_module("select-files")
        selected = selection.select([], [], [], [], [], 160000, [], [])
        for reason in selected["counts"]["excluded"]:
            self.assertIn(f"`{reason}`", read(SKILL / "SKILL.md"))
        report = read(SKILL / "references/phase-3-report.md")
        self.assertIn("Files excluded", report)
        self.assertIn("Agents skipped", report)
        self.assertIn("test files are *classified*", read(SKILL / "SKILL.md"))

    def test_rule_packs(self):
        paths = list((SKILL / "rules").glob("*.md"))
        self.assertGreaterEqual(len(paths) - 1, 9)
        for path in paths:
            with self.subTest(path=path.name):
                text = read(path)
                self.assertTrue(text.startswith("#"))
                self.assertLessEqual(len(text.splitlines()), 30 if path.name == "README.md" else 60)
                if path.name != "README.md":
                    self.assertIn("#### Do not report\n", text)
                    section = text.split("#### Do not report\n", 1)[1].split("\n#### ", 1)[0]
                    self.assertGreaterEqual(len(re.findall(r"^- ", section, re.M)), 4)

    def test_tally_keys(self):
        template = re.search(r"review-all-severity: \{([^}]+)\}", read(SKILL / "references/phase-3-report.md"))[1]
        self.assertEqual({"critical", "important", "debt", "suggested", "question"},
                         set(re.findall(r'"(\w+)"', template)))

    def test_anonymization(self):
        blocklist = ROOT / "tests/anonymization-blocklist.txt"
        if not blocklist.exists():
            blocklist = ROOT / "tests/anonymization-blocklist.example.txt"
        patterns = [line for line in read(blocklist).splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertTrue(patterns, "empty anonymization blocklist")
        pattern = re.compile("|".join(patterns), re.I)
        for path in [ROOT / "README.md", *(ROOT / "skills").rglob("*")]:
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assertIsNone(pattern.search(read(path)))

    def test_eval_schema(self):
        validator = load_module("validate-evals")
        from portable import run
        result = run([sys.executable, str(SKILL / "scripts/validate-evals.py")],
                     cwd=ROOT, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
