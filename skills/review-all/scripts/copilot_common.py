"""Shared, deterministic Copilot adapter primitives (no model calls)."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone

SCHEMA = 1
MIN_CLI = (1, 0, 95)
SKILL = Path(__file__).resolve().parent.parent
RUNTIME = ".copilot/review-all"
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def load_module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SKILL / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True).encode()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".review-all-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value if isinstance(value, bytes) else encoded(value) + b"\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def now():
    return datetime.now(timezone.utc).isoformat()


def run(argv, cwd=None, **kwargs):
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, **kwargs)
    if proc.returncode:
        raise ValueError(f"{argv!r} exited {proc.returncode}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout


def git(repo, *args):
    return run(["git", "--no-pager", "-C", str(repo), *args])


def cli_version(binary="copilot"):
    output = run([binary, "--version"]).decode()
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    if not match or tuple(map(int, match.groups())) < MIN_CLI:
        raise ValueError("Copilot CLI >= 1.0.95 required (minimum tested); "
                         "older versions may silently omit custom-agent instructions. " + output.strip())
    return match.group(0)


def safe_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"expected a repository-relative path: {relative!r}")
    result = Path(root) / path
    if not result.resolve().is_relative_to(Path(root).resolve()):
        raise ValueError(f"path escapes root: {relative!r}")
    return result


def file_hash(path):
    path = Path(path)
    if path.is_symlink():
        return "symlink:" + digest(os.readlink(path).encode())
    return digest(path.read_bytes()) if path.exists() else None


def state_path(repo, relative):
    """Resolve lifecycle paths without allowing traversal or symlink escapes."""
    root = Path(repo).resolve() / RUNTIME
    path = safe_path(repo, relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError(f"state path must stay inside the Copilot state namespace {RUNTIME}/")
    return path


def body(text):
    return text.split("---", 2)[2].lstrip() if text.startswith("---\n") else text


def adapt(text):
    """Mechanical host vocabulary changes, never a second copy of review rules."""
    for old, new in [
        (".claude/cache", RUNTIME + "/cache"), (".claude/", ".copilot/"),
        ("AskUserQuestion", "ask_user"), ("CLAUDE.md rules", "applicable project instructions"),
        ("`Read`", "`view`"), ("`Grep`", "`grep`"), ("`Glob`", "`glob`"),
        ("`Bash`", "`bash`"), ("`Write`", "`create`"), ("`Edit`", "`edit`"),
    ]:
        text = text.replace(old, new)
    return text


def persona_text(axis):
    # Preserve methodology, replace only the legacy bare-array return format
    # with the versioned per-dispatch envelope.
    text = adapt(body((SKILL / "agents" / (axis + ".md")).read_text()))
    return text.split("## Return format", 1)[0]


def models():
    return read_json(SKILL / "copilot" / "models.json")


def agent_id(axis):
    return "review-all-" + re.sub(r"^\d\d-", "", axis)


def model_for(axis, config):
    defaults = models()
    value = config.get("models", {}).get(axis, defaults["roles"].get(axis, defaults["default"]))
    if axis == "verifier":
        value = config.get("verifierModel", value)
    if not isinstance(value, str) or not value or value in {"opus", "sonnet", "haiku", "inherit", "auto"}:
        raise ValueError(f"{axis}: use an explicit Copilot model ID, not {value!r}")
    return value


def reasoning_for(config):
    value = config.get("reasoningEffort", models()["reasoningEffort"])
    if value not in REASONING_EFFORTS:
        raise ValueError(f"unsupported reasoningEffort: {value!r}")
    return value


def resource_hashes():
    return {str(p.relative_to(SKILL)): file_hash(p) for p in sorted(SKILL.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
