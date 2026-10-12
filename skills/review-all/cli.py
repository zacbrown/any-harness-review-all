"""Shared CLI. Tool/environment paths are independent of the reviewed repository."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

SKILL = Path(__file__).resolve().parent
TOOL = SKILL.parents[1]
sys.path.insert(0, str(SKILL / "scripts"))

from portable import execute, run
import discovery

OPERATIONS = {p.stem for p in (SKILL / "scripts").glob("*.py")
              if ("-" in p.stem or p.stem in {"checkpoint", "dedupe"}) and not p.stem.startswith("copilot-install")}


def environment_key():
    return hashlib.sha256((TOOL / "pyproject.toml").read_bytes() + (TOOL / "uv.lock").read_bytes()).hexdigest()


def setup(tool=TOOL):
    tool = Path(tool).resolve()
    environment = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(tool / ".venv"))
    environment.pop("VIRTUAL_ENV", None)
    result = run(["uv", "sync", "--project", str(tool), "--locked", "--no-python-downloads",
                  "--python", sys.executable], cwd=tool, env=environment, capture_output=True, timeout=300)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace"))
    key = hashlib.sha256((tool / "pyproject.toml").read_bytes() + (tool / "uv.lock").read_bytes()).hexdigest()
    (tool / ".venv/review-all-install.json").write_text(json.dumps({"key": key}), encoding="utf-8")
    print(json.dumps({"installed": True, "tool": str(tool)}))
    return 0


def ensure_environment():
    marker = TOOL / ".venv/review-all-install.json"
    if (Path(sys.prefix).resolve() != (TOOL / ".venv").resolve() or not marker.is_file()
            or json.loads(marker.read_text(encoding="utf-8")).get("key") != environment_key()):
        raise ValueError(f"incomplete or stale tool environment at {TOOL}; run just setup there. "
                         "Reviews never synchronize dependencies or download Python.")


def installation(args):
    from copilot_common import load_module, read_json
    installer = load_module("copilot-install")
    parser = argparse.ArgumentParser(prog="review-all install/uninstall")
    parser.add_argument("--harness", choices=["claude", "copilot"], default="claude")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--user", action="store_true")
    where.add_argument("--project", type=Path)
    parser.add_argument("--copilot", default="copilot")
    parser.add_argument("--config", type=Path)
    options = parser.parse_args(args[1:])
    harness = options.harness
    if options.project:
        destination = options.project.resolve() / (".github" if harness == "copilot" else ".claude")
    else:
        destination = Path(os.environ.get("COPILOT_HOME", Path.home() / ".copilot")) if harness == "copilot" else Path.home() / ".claude"
    if args[0] == "uninstall":
        retained = installer.uninstall(destination, harness=harness)
        print(json.dumps({"retained": retained, "destination": str(destination)}))
    else:
        if harness == "copilot":
            installer.cli_version(options.copilot)
        installer.install(destination, read_json(options.config) if options.config else None, harness=harness)
        installer.provision(destination, harness)
        print(json.dumps({"installed": True, "destination": str(destination)}))
    return 0


def main(argv=None):
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="review-all native tooling. Always pass --repo explicitly for repository operations.",
        epilog="Operations: discover, preflight, detect-toolchain, test-pattern-probe, dev-server-probe, "
               "http-probe, execute, diff, install, uninstall, setup, test, eval, " + ", ".join(sorted(OPERATIONS)))
    parser.add_argument("--repo", type=Path, help="reviewed repository / subprocess working directory")
    parser.add_argument("--input", type=Path, help="read legacy stdin input from this file without a shell pipe")
    parser.add_argument("operation", nargs="?", default="help")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    operation, args = options.operation, options.arguments
    original_cwd = Path.cwd()
    original_argv, original_stdin = sys.argv, sys.stdin
    try:
        if operation == "help":
            parser.print_help()
            return 0
        if operation == "setup":
            return setup()
        if operation == "uninstall":
            # Recovery/self-removal may use system Python rather than the
            # environment being removed (Windows locks running executables).
            return installation([operation, *args])
        ensure_environment()
        if operation == "review-self":
            print(f"Now run /review-all inside Claude Code in {TOOL}.")
            return 0
        if operation == "install":
            return installation([operation, *args])
        if operation == "test":
            from testing import main as test
            return test(TOOL, args)
        if operation in {"eval", "run-evals", "run-evals-headless"}:
            from evaluation import main as evaluate
            return evaluate(args, manual=operation == "run-evals")
        if operation in {"discover", "detect-toolchain", "test-pattern-probe", "execute", "diff", "mkdir"} and options.repo is None:
            parser.error(f"{operation} requires --repo; locating the tool never selects the review target")
        if options.input:
            sys.stdin = io.TextIOWrapper(io.BytesIO(options.input.read_bytes()), encoding="utf-8")
        if options.repo:
            os.chdir(options.repo.resolve(strict=True))
        result = None
        if operation == "integration":
            sys.argv = [str(TOOL / "tests/copilot_integration.py"), *args]
            runpy.run_path(sys.argv[0], run_name="__main__")
            return 0
        if operation == "mkdir":
            from copilot_common import safe_path
            for path in args:
                safe_path(Path.cwd(), path).mkdir(parents=True, exist_ok=True)
            result = {"created": args}
        elif operation == "discover":
            p = argparse.ArgumentParser(prog=operation)
            p.add_argument("--harness", choices=["claude", "copilot"], default="claude")
            result = discovery.discover(Path.cwd(), p.parse_args(args).harness)
        elif operation == "preflight":
            result = discovery.preflight()
            print(json.dumps(result))
            if not result["git"]:
                print("review-all: git is required", file=sys.stderr)
            return 0 if result["git"] else 2
        elif operation == "detect-toolchain":
            result = discovery.toolchain(Path.cwd())
        elif operation == "test-pattern-probe":
            result = discovery.test_pattern(Path.cwd())
        elif operation == "dev-server-probe":
            result = discovery.dev_server([int(p) for p in (args[0] if args else "4200,5173,3000,8080").split(",")])
        elif operation == "http-probe":
            p = argparse.ArgumentParser(prog=operation)
            p.add_argument("url")
            p.add_argument("--timeout", type=float, default=5)
            a = p.parse_args(args)
            result = discovery.http_probe(a.url, a.timeout)
        elif operation == "execute":
            p = argparse.ArgumentParser(prog=operation)
            p.add_argument("spec", type=Path, help='JSON: {"argv":[...],"timeoutSeconds":180} or {"shell":"bash","program":"..."}')
            spec = json.loads(p.parse_args(args).spec.read_text(encoding="utf-8"))
            if "shell" in spec:
                if "argv" in spec or spec["shell"] not in {"bash", "sh", "pwsh", "powershell", "cmd"}:
                    raise ValueError("use either argv or an explicitly supported required shell")
                flag = "/c" if spec["shell"] == "cmd" else "-Command" if spec["shell"] in {"pwsh", "powershell"} else "-c"
                command = [spec["shell"], flag, spec["program"]]
            else:
                command = spec["argv"]
            execution = execute(command, cwd=Path.cwd(), timeout=spec.get("timeoutSeconds", 180))
            result = {**execution.as_json(), "cwd": str(Path.cwd()), "requiredShell": spec.get("shell")}
            print(json.dumps(result))
            return 124 if execution.timed_out else 0 if execution.returncode == 0 else 1
        elif operation == "diff":
            p = argparse.ArgumentParser(prog=operation)
            p.add_argument("--output", required=True, type=Path)
            p.add_argument("git_args", nargs=argparse.REMAINDER)
            a = p.parse_args(args)
            git_args = a.git_args[1:] if a.git_args[:1] == ["--"] else a.git_args
            diff = execute(["git", "--no-pager", "diff", "--no-ext-diff", "--no-textconv", "--binary", *git_args], cwd=Path.cwd())
            if diff.returncode:
                raise ValueError(diff.stderr.decode("utf-8", errors="replace"))
            a.output.write_bytes(diff.stdout)
            result = {"path": str(a.output.resolve()), "bytes": len(diff.stdout),
                      "sha256": hashlib.sha256(diff.stdout).hexdigest()}
        elif operation in OPERATIONS:
            if operation in {"copilot-package", "copilot-gate"} and options.repo:
                # These legacy parsers already accept --repo after their action.
                args = [*args, "--repo", str(Path.cwd())] if operation == "copilot-package" and args[:1] == ["prepare"] else args
                if operation == "copilot-gate":
                    args = ["--repo", str(Path.cwd()), *args]
            sys.argv = [str(SKILL / "scripts" / (operation + ".py")), *args]
            runpy.run_path(sys.argv[0], run_name="__main__")
            return 0
        else:
            parser.error(f"unknown operation: {operation}")
        print(json.dumps(result))
        return 0
    except SystemExit as error:
        return error.code
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"review-all: {error}", file=sys.stderr)
        return 2
    finally:
        os.chdir(original_cwd)
        sys.argv, sys.stdin = original_argv, original_stdin


if __name__ == "__main__":
    sys.exit(main())
