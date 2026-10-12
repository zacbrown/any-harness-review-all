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
from portable import run

MARKER = "review-all-install.json"


def environment_path(destination, name):
    path = Path(name)
    if not name.startswith(".review-all-tool/.venv/"):
        raise ValueError(f"environment file outside tool environment: {name}")
    return safe_path(destination, path.parent) / path.name


def tool_files():
    root = SKILL.parents[1]
    files = {}
    for name in ("pyproject.toml", "uv.lock", "justfile", "README.md", "LICENSE"):
        files[".review-all-tool/" + name] = (root / name).read_bytes()
    for directory in (root / "skills", root / "tests", root / "docs"):
        for path in sorted(directory.rglob("*")):
            if (path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
                    and path.suffix in {".py", ".json", ".md", ".txt"}
                    and path.name != "anonymization-blocklist.txt"):
                files[".review-all-tool/" + path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def assemble(config=None, harness="copilot"):
    config = config or {}
    files = {}
    for path in sorted(SKILL.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(SKILL)
        if path.suffix != ".sh":
            files["skills/review-all/" + relative.as_posix()] = path.read_bytes()
    files.update(tool_files())
    if harness == "claude":
        return files
    files["skills/review-all/SKILL.md"] = (SKILL / "copilot/SKILL.md").read_bytes()
    files["skills/review-all/references/workflow.md"] = adapt(body((SKILL / "SKILL.md").read_text(encoding="utf-8"))).encode()
    shared = adapt(body((SKILL / "agents/_shared.md").read_text(encoding="utf-8")))
    shared_gate = "## 3-question gate\n" + shared.split("## 3-question gate\n", 1)[1].split("\n## ", 1)[0]
    protocol = (SKILL / "copilot/dispatch.md").read_text(encoding="utf-8")
    axes = load_module("select-agents").AGENT_IDS + ["verifier", "deep-dive"]
    for axis in axes:
        persona = ((SKILL / "copilot/deep-dive.md").read_text(encoding="utf-8") if axis == "deep-dive" else persona_text(axis))
        tools = ["read", "search"]
        if axis == "04-consistency-history":
            tools.append("execute")
        header = {
            "name": agent_id(axis),
            "description": ("Read-only investigation of a selected review finding" if axis == "deep-dive" else
                            f"review-all {axis}: independent, evidence-based review; requires a dispatch package"),
            "infer": axis != "deep-dive",
            "tools": tools,
            "model": model_for(axis, config),
            "modelPolicy": "required",
            "reasoningEffort": reasoning_for(config, axis),
            "include-custom-instructions": True,
        }
        frontmatter = "\n".join(f"{k}: {json.dumps(v)}" for k, v in header.items())
        role_protocol = "" if axis == "deep-dive" else protocol
        reviewer_guidance = "" if axis == "deep-dive" else (
            f"{shared_gate}\n\nRead the supplied shared-rules component in full. "
            "It contains the canonical severity, quotas, claim classes, dismissal, "
            "evidence, and exploration rules, resolved for this run.\n")
        text = f"---\n{frontmatter}\n---\n\n{role_protocol}\n\n{persona}\n\n{reviewer_guidance}"
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        if len(body(text)) > 30000:
            raise ValueError(f"{axis} exceeds Copilot's 30,000-character profile limit")
        files[f"agents/{agent_id(axis)}.agent.md"] = text.encode()
    return files


def install(destination, config=None, harness="copilot"):
    destination = Path(destination).resolve()
    marker = destination / MARKER
    owner = "review-all-" + harness
    previous = read_json(marker) if marker.exists() else {"owner": owner, "files": {}}
    if previous.get("owner") != owner:
        raise ValueError(f"unowned installation marker: {marker}")
    files = assemble(config, harness)
    environment = previous.get("environment", {})
    envdir = destination / ".review-all-tool/.venv"
    if envdir.exists() and not environment:
        raise ValueError(f"refusing unowned environment: {envdir}")
    for name, expected in environment.items():
        if file_hash(environment_path(destination, name)) != expected:
            raise ValueError(f"refusing locally modified environment: {name}")
    for name in set(files) | set(previous["files"]):
        target = (environment_path(destination, name) if name in previous.get("environment", {})
                  else safe_path(destination, name))
        if target.is_symlink() or (target.exists() and file_hash(target) != previous["files"].get(name)):
            raise ValueError(f"refusing to overwrite unowned or locally modified file: {target}")
    for name, data in files.items():
        atomic(safe_path(destination, name), data)
    for name in previous["files"].keys() - files.keys():
        target = safe_path(destination, name)
        if target.exists():
            target.unlink()
    atomic(marker, {"owner": owner, "schemaVersion": 1, "environment": environment,
                    "files": {name: digest(data) for name, data in files.items()}})
    return len(files)


def record_environment(destination, harness="copilot"):
    destination = Path(destination)
    marker = destination / MARKER
    previous = read_json(marker)
    if previous["owner"] != "review-all-" + harness:
        raise ValueError("installation marker belongs to another package")
    previous["environment"] = {
        path.relative_to(destination).as_posix(): file_hash(path)
        for path in (destination / ".review-all-tool/.venv").rglob("*")
        if path.is_file() or path.is_symlink()}
    atomic(marker, previous)


def provision(destination, harness="copilot"):
    tool = Path(destination) / ".review-all-tool"
    try:
        result = run([sys.executable, str(tool / "skills/review-all/cli.py"), "setup"],
                     cwd=tool, capture_output=True, timeout=300)
        if result.returncode:
            raise ValueError("tool setup failed: " + result.stderr.decode("utf-8", errors="replace"))
    finally:
        # A failed sync must remain repairable without claiming its environment
        # as an unrelated collision on the next install.
        record_environment(destination, harness)


def uninstall(destination, harness="copilot"):
    destination = Path(destination).resolve()
    if os.name == "nt" and Path(sys.prefix).resolve() == destination / ".review-all-tool/.venv":
        raise ValueError("Windows cannot remove its running Python environment; run the installed cli.py "
                         "uninstall with system Python outside this .venv (see references/cli.md)")
    marker = destination / MARKER
    if not marker.exists():
        raise ValueError(f"no package ownership manifest at {marker}; nothing removed")
    previous = read_json(marker)
    if previous.get("owner") != "review-all-" + harness:
        raise ValueError("installation marker belongs to another package")
    retained = {}
    retained_environment = {}
    owned = {**previous["files"], **previous.get("environment", {})}
    for name, expected in owned.items():
        target = (environment_path(destination, name) if name in previous.get("environment", {})
                  else safe_path(destination, name))
        if ((target.is_symlink() and name not in previous.get("environment", {}))
                or ((target.exists() or target.is_symlink()) and file_hash(target) != expected)):
            (retained_environment if name in previous.get("environment", {}) else retained)[name] = expected
            print(f"preserved modified file: {target}", file=sys.stderr)
        elif target.exists() or target.is_symlink():
            target.unlink()
    if retained or retained_environment:
        atomic(marker, {**previous, "files": retained, "environment": retained_environment})
    else:
        marker.unlink()
    # Only remove empty directories which contained our files.
    parents = {(environment_path(destination, n) if n in previous.get("environment", {})
                else safe_path(destination, n)).parent for n in owned}
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        while parent != destination and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return len(retained) + len(retained_environment)


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
            provision(dest)
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
