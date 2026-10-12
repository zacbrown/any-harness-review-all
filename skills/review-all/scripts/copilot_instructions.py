"""Capture enabled instructions without widening path scope or trusting inheritance."""

import os
from pathlib import Path
import re

from copilot_common import file_hash, load_module, safe_path

SELECT = load_module("select-files")
AGENT_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md", ".claude/CLAUDE.md",
               ".github/copilot-instructions.md")


def patterns(value):
    # Split commas outside brace groups, matching the documented applyTo grammar.
    return re.split(r",(?![^{}]*\})", value)


def applies(pattern, path):
    if any(c in pattern for c in "[]!"):
        raise ValueError(f"unsupported instruction glob {pattern!r}; provide resolved scope explicitly")
    return any(re.fullmatch(SELECT.glob_to_regex(p.strip()), path)
               for p in SELECT.expand_braces(pattern.strip()))


def apply_to(text):
    if not text.startswith("---\n"):
        raise ValueError("modular instruction is missing applyTo frontmatter")
    header = text.split("---", 2)[1]
    match = re.search(r"^applyTo:\s*(.*?)\s*$", header, re.M)
    if not match:
        raise ValueError("modular instruction is missing applyTo")
    value = match.group(1).strip()
    if value.startswith(('"', "'")) and value.endswith(value[0]):
        value = value[1:-1]
    if not value or value[0] in "|>[":
        raise ValueError("unsupported applyTo syntax; use a quoted comma-separated glob string")
    return value


def collect(repo, files, policy):
    if policy.get("mode") not in {"enabled", "disabled"} or policy.get("settingsConfirmed") is not True:
        return [], {}, ["instruction settings unavailable: coordinator must confirm mode and disabledPaths"]
    if policy["mode"] == "disabled":
        return [{"status": "disabled", "source": "--no-custom-instructions", "files": files}], {}, []
    disabled = {str(Path(p).expanduser().resolve()) for p in policy.get("disabledPaths", [])}
    entries, watched, errors, seen = [], {}, [], set()
    repo = Path(repo).resolve()

    def add(path, scope_files, boundary, origin, modular=False, imported=False):
        path = Path(path).absolute()
        key = (str(path), tuple(scope_files))
        if key in seen:
            return
        seen.add(key)
        entry = {"source": str(path), "origin": origin, "files": scope_files,
                 "status": "enabled", "imported": imported}
        if str(path.resolve()) in disabled:
            entries.append({**entry, "status": "disabled"})
            return
        watched[str(path)] = file_hash(path)
        if not path.exists():
            if imported:
                errors.append(f"missing instruction import: {path}")
            return
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(boundary.resolve()):
                raise ValueError("instruction path escapes its boundary or is a symlink")
            text = path.read_text(encoding="utf-8")
            if modular:
                glob = apply_to(text)
                entry["applyTo"] = glob
                entry["files"] = [f for f in scope_files if any(applies(p, f) for p in patterns(glob))]
            if not entry["files"]:
                return
            entry["content"] = text
            entries.append(entry)
            expandable = path.name in {"AGENTS.md", "CLAUDE.md", "copilot-instructions.md"} or imported
            if expandable:
                for reference in re.findall(r"(?<![\w/])@([^\s`<>\"']+)", text):
                    if reference.startswith(("/", "~/")):
                        continue  # Native CLI deliberately does not expand absolute/home imports.
                    target = (path.parent / reference).resolve()
                    if not target.is_relative_to(boundary.resolve()):
                        errors.append(f"instruction import outside boundary: {path}: {reference}")
                        continue
                    add(target, entry["files"], boundary, origin, imported=True)
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"unavailable instruction {path}: {error}")

    directories = {repo}
    for name in files:
        parent = safe_path(repo, name).parent
        while parent.is_relative_to(repo):
            directories.add(parent)
            if parent == repo:
                break
            parent = parent.parent
    for directory in sorted(directories):
        prefix = directory.relative_to(repo).as_posix()
        scoped = files if prefix == "." else [f for f in files if f.startswith(prefix + "/")]
        for name in AGENT_FILES:
            add(directory / name, scoped, repo, "repository")
        instruction_dir = directory / ".github/instructions"
        for path in sorted(instruction_dir.rglob("*.instructions.md")):
            # applyTo is relative to this instruction directory's repository scope.
            local = [f[len(prefix) + 1:] if prefix != "." else f for f in scoped]
            before = len(entries)
            add(path, local, repo, "repository-modular", modular=True)
            for entry in entries[before:]:
                entry["files"] = [prefix + "/" + f if prefix != "." else f for f in entry["files"]]

    home = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")).expanduser().resolve()
    add(home / "copilot-instructions.md", files, home, "user")
    for path in sorted((home / "instructions").rglob("*.instructions.md")):
        add(path, files, home, "user-modular", modular=True)

    # These locations may be known only to the parent (including plugins and
    # COPILOT_CUSTOM_INSTRUCTIONS_DIRS). Never guess that their settings are enabled.
    supplied = policy.get("parentSources", [])
    env_dirs = [str(Path(p).expanduser().resolve()) for p in
                os.environ.get("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "").split(",") if p]
    declared = policy.get("confirmedExtraDirs", [])
    if sorted(env_dirs) != sorted(declared):
        errors.append("COPILOT_CUSTOM_INSTRUCTIONS_DIRS not explicitly accounted for by coordinator")
    for source in supplied:
        if source.get("enabled") is False:
            entries.append({"source": source["path"], "files": source["files"], "status": "disabled"})
            continue
        if source.get("enabled") is not True or not set(source["files"]).issubset(files):
            errors.append(f"parent-only instruction scope/settings unavailable: {source.get('path')}")
            continue
        path = Path(source["path"]).expanduser().resolve()
        add(path, source["files"], Path(source["boundary"]).expanduser().resolve(), "parent-transfer",
            modular=path.name.endswith(".instructions.md"), imported=True)
    return entries, watched, errors
