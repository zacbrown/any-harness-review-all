#!/usr/bin/env python3
"""Install/uninstall only hash-tracked package-owned files; never rsync --delete."""

import argparse
import json
import os
from pathlib import Path
import re
import sys

from copilot_common import SKILL, adapt, agent_id, atomic, body, cli_version, digest, file_hash
from copilot_common import load_module, model_for, persona_text, read_json, reasoning_for, safe_path

MARKER = "review-all-install.json"


def assemble(config=None):
    config = config or {}
    files = {}
    for path in sorted(SKILL.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(SKILL)
        files["skills/review-all/" + str(relative)] = path.read_bytes()
    files["skills/review-all/SKILL.md"] = (SKILL / "copilot/SKILL.md").read_bytes()
    files["skills/review-all/references/workflow.md"] = adapt(body((SKILL / "SKILL.md").read_text())).encode()
    shared = adapt(body((SKILL / "agents/_shared.md").read_text()))
    shared_gate = "## 3-question gate\n" + shared.split("## 3-question gate\n", 1)[1].split("\n## ", 1)[0]
    protocol = (SKILL / "copilot/dispatch.md").read_text()
    axes = load_module("select-agents").AGENT_IDS + ["verifier", "deep-dive"]
    for axis in axes:
        persona = ((SKILL / "copilot/deep-dive.md").read_text() if axis == "deep-dive" else persona_text(axis))
        tools = ["read", "search"]
        if axis == "04-consistency-history":
            tools.append("execute")
        header = {
            "name": agent_id(axis),
            "description": ("Read-only investigation of a selected review finding" if axis == "deep-dive" else
                            f"review-all {axis}: independent, evidence-based review; requires a dispatch package"),
            "tools": tools,
            "model": model_for(axis, config),
            "modelPolicy": "required",
            "reasoningEffort": reasoning_for(config, axis),
            "include-custom-instructions": True,
        }
        frontmatter = "\n".join(f"{k}: {json.dumps(v)}" for k, v in header.items())
        role_protocol = "" if axis == "deep-dive" else protocol
        text = (f"---\n{frontmatter}\n---\n\n{role_protocol}\n\n{persona}\n\n{shared_gate}\n\n"
                "Read the supplied shared-rules component in full. "
                "It contains the canonical severity, quotas, claim classes, dismissal, "
                "evidence, and exploration rules, resolved for this run.\n")
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        if len(body(text)) > 30000:
            raise ValueError(f"{axis} exceeds Copilot's 30,000-character profile limit")
        files[f"agents/{agent_id(axis)}.agent.md"] = text.encode()
    return files


def install(destination, config=None):
    destination = Path(destination).resolve()
    marker = destination / MARKER
    previous = read_json(marker) if marker.exists() else {"owner": "review-all-copilot", "files": {}}
    if previous.get("owner") != "review-all-copilot":
        raise ValueError(f"unowned installation marker: {marker}")
    files = assemble(config)
    for name in set(files) | set(previous["files"]):
        target = safe_path(destination, name)
        if target.is_symlink() or (target.exists() and file_hash(target) != previous["files"].get(name)):
            raise ValueError(f"refusing to overwrite unowned or locally modified file: {target}")
    for name, data in files.items():
        atomic(safe_path(destination, name), data)
    for name in previous["files"].keys() - files.keys():
        target = safe_path(destination, name)
        if target.exists():
            target.unlink()
    atomic(marker, {"owner": "review-all-copilot", "schemaVersion": 1,
                    "files": {name: digest(data) for name, data in files.items()}})
    return len(files)


def uninstall(destination):
    destination = Path(destination).resolve()
    marker = destination / MARKER
    if not marker.exists():
        raise ValueError(f"no package ownership manifest at {marker}; nothing removed")
    previous = read_json(marker)
    if previous.get("owner") != "review-all-copilot":
        raise ValueError("installation marker belongs to another package")
    retained = {}
    for name, expected in previous["files"].items():
        target = safe_path(destination, name)
        if target.is_symlink() or (target.exists() and file_hash(target) != expected):
            retained[name] = expected
            print(f"preserved modified file: {target}", file=sys.stderr)
        elif target.exists():
            target.unlink()
    if retained:
        atomic(marker, {**previous, "files": retained})
    else:
        marker.unlink()
    # Only remove empty directories which contained our files.
    parents = {safe_path(destination, n).parent for n in previous["files"]}
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        while parent != destination and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return len(retained)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall"])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--project", type=Path, help="reviewed repository, not the package checkout")
    where.add_argument("--user", action="store_true")
    parser.add_argument("--config", type=Path, help="model configuration used to generate profile defaults")
    parser.add_argument("--copilot", default="copilot")
    args = parser.parse_args()
    dest = args.project.resolve() / ".github" if args.project else Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot"))
    try:
        if args.action == "install":
            version = cli_version(args.copilot)
            count = install(dest, read_json(args.config) if args.config else None)
            print(f"Installed {count} owned files into {dest}; Copilot {version}. Restart Copilot to load profiles.")
        else:
            retained = uninstall(dest)
            print(f"Uninstalled unchanged package files; {retained} modified files preserved.")
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"copilot-install: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
