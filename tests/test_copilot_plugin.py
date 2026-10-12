"""Marketplace packaging checks without a model, authentication, or Copilot installation."""

import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/review-all/scripts"))
from copilot_common import body, load_module

PLUGIN = load_module("copilot-plugin")
INSTALL = load_module("copilot-install")


class TestCopilotPlugin(unittest.TestCase):
    def test_committed_components_match_canonical_sources(self):
        self.assertEqual([], PLUGIN.generate(check=True)["changed"])

    def test_manifest_exposes_only_native_copilot_components(self):
        files = PLUGIN.assemble()
        manifest = json.loads(files["plugin.json"])
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["version"], manifest["version"])
        self.assertEqual("review-all", manifest["name"])
        self.assertEqual(PLUGIN.REPOSITORY, manifest["repository"])
        self.assertEqual("MIT", manifest["license"])
        self.assertEqual(["./.github/plugin/skills/"], manifest["skills"])
        self.assertEqual("./.github/plugin/agents/", manifest["agents"])
        self.assertNotIn("hooks", manifest)
        self.assertEqual(["skills/review-all/SKILL.md"],
                         [name for name in files if name.endswith("SKILL.md")])
        for directory in [manifest["agents"], *manifest["skills"]]:
            self.assertTrue((ROOT / directory).is_dir())

    def test_profiles_are_shared_with_the_installer(self):
        files = PLUGIN.assemble()
        profiles = {name: content for name, content in files.items() if name.startswith("agents/")}
        self.assertEqual(12, len(profiles))
        self.assertEqual(INSTALL.profiles(), profiles)
        for content in profiles.values():
            text = content.decode()
            self.assertIn("include-custom-instructions: true", text)
            self.assertIn('modelPolicy: "required"', text)
            self.assertLessEqual(len(body(text)), 30000)
        self.assertEqual(INSTALL.workflow_text().encode(), files["workflow.md"])

    def test_installer_tool_includes_plugin_generation_inputs_and_outputs(self):
        files = INSTALL.tool_files()
        for name, content in PLUGIN.assemble().items():
            self.assertEqual(content, files[".review-all-tool/.github/plugin/" + name])
        self.assertIn(".review-all-tool/skills/review-all/copilot/plugin-layout.md", files)

    def test_skill_layout_resolves_inside_plugin_root(self):
        entry = ROOT / ".github/plugin/skills/review-all"
        tool = (entry / "../../../..").resolve()
        self.assertEqual(ROOT, tool)
        for resource in ("pyproject.toml", "uv.lock", "justfile", "skills/review-all/cli.py",
                         "skills/review-all/agents/_shared.md", "skills/review-all/references/cli.md",
                         ".github/plugin/workflow.md"):
            self.assertTrue((tool / resource).is_file(), resource)
        text = PLUGIN.assemble()["skills/review-all/SKILL.md"].decode()
        self.assertIn("ENTRY_ROOT/../../../..", text)
        self.assertIn("SKILL_ROOT` as `TOOL/skills/review-all", text)
        self.assertIn("references/workflow.md` means `TOOL/.github/plugin/workflow.md", text)
        self.assertNotIn("SKILL_ROOT/../../.review-all-tool", text)
        self.assertIn("do not synchronize dependencies as", text)
        source = (ROOT / "skills/review-all/copilot/SKILL.md").read_text(encoding="utf-8")
        self.assertEqual(source.split("## Preconditions and boundaries\n", 1)[1],
                         text.split("## Preconditions and boundaries\n", 1)[1])

    def test_generation_is_repeatable_and_check_never_repairs_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "plugin"
            with self.assertRaisesRegex(ValueError, "stale"):
                PLUGIN.generate(destination, check=True)
            self.assertFalse(destination.exists())
            result = PLUGIN.generate(destination)
            self.assertEqual(result["files"], len(result["changed"]))
            self.assertEqual([], PLUGIN.generate(destination)["changed"])
            self.assertEqual([], PLUGIN.generate(destination, check=True)["changed"])
            skill = destination / "skills/review-all/SKILL.md"
            skill.write_text("stale", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stale"):
                PLUGIN.generate(destination, check=True)
            self.assertEqual("stale", skill.read_text(encoding="utf-8"))
            self.assertEqual(["skills/review-all/SKILL.md"], PLUGIN.generate(destination)["changed"])
            skill.unlink()
            with self.assertRaisesRegex(ValueError, "stale"):
                PLUGIN.generate(destination, check=True)

    def test_unexpected_output_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            extra = destination / "unexpected.md"
            extra.write_text("keep", encoding="utf-8")
            for check in (False, True):
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    PLUGIN.generate(destination, check=check)
            self.assertEqual("keep", extra.read_text(encoding="utf-8"))
            self.assertFalse((destination / "plugin.json").exists())


if __name__ == "__main__":
    unittest.main()
