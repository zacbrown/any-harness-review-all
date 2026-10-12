#!/usr/bin/env python3
"""Generate or check the committed Copilot plugin components from canonical sources."""

import argparse
import json
from pathlib import Path
import sys
import tomllib

from copilot_common import SKILL, atomic, load_module

ROOT = SKILL.parents[1]
DESTINATION = ROOT / ".github/plugin"
INSTALL = load_module("copilot-install")
REPOSITORY = "https://github.com/zacbrown/any-harness-review-all"


def assemble():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    manifest = {
        "name": "review-all",
        "version": metadata["version"],
        "description": "Native Copilot multi-axis code review with twelve custom agent profiles, "
                       "deterministic checks, and independent verification. Requires Python >=3.11, uv, "
                       "Git, and explicit runtime setup.",
        "author": {"name": "zacbrown", "url": "https://github.com/zacbrown"},
        "homepage": REPOSITORY + "/blob/main/docs/COPILOT.md",
        "repository": REPOSITORY,
        "license": "MIT",
        "keywords": ["code-review", "review", "multi-agent", "verification", "copilot"],
        "agents": "./.github/plugin/agents/",
        "skills": ["./.github/plugin/skills/"],
    }
    skill = (SKILL / "copilot/SKILL.md").read_text(encoding="utf-8")
    before, start, rest = skill.partition("## Installation layout\n")
    _, end, after = rest.partition("## Preconditions and boundaries\n")
    if not start or not end:
        raise ValueError("Copilot skill is missing its installation layout section")
    layout = (SKILL / "copilot/plugin-layout.md").read_text(encoding="utf-8")
    files = {
        "plugin.json": (json.dumps(manifest, indent=2) + "\n").encode(),
        "skills/review-all/SKILL.md": (before + layout.rstrip() + "\n\n" + end + after).encode(),
        "workflow.md": INSTALL.workflow_text().encode(),
    }
    files.update(INSTALL.profiles())
    return files


def generate(destination=DESTINATION, check=False):
    destination = Path(destination)
    files = assemble()
    existing = {p.relative_to(destination).as_posix() for p in destination.rglob("*")
                if p.is_file() or p.is_symlink()}
    unexpected = existing - files.keys()
    if unexpected:
        raise ValueError("unexpected plugin files; review and remove obsolete generated files explicitly: "
                         + ", ".join(sorted(unexpected)))
    changed = []
    for name, content in files.items():
        path = destination / name
        if path.is_symlink() or not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"refusing symlinked plugin output: {path}")
        if not path.is_file() or path.read_bytes() != content:
            changed.append(name)
    if check and changed:
        raise ValueError("generated plugin is stale; run just package-copilot: " + ", ".join(changed))
    if not check:
        for name in changed:
            atomic(destination / name, files[name])
    return {"files": len(files), "changed": changed, "checked": check}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail on missing, stale, or unexpected output")
    args = parser.parse_args()
    try:
        print(json.dumps(generate(check=args.check)))
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"copilot-plugin: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
