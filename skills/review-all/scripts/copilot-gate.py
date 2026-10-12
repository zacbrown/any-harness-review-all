#!/usr/bin/env python3
"""Thin headless wrapper: a Copilot process exit is not a review verdict."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import uuid

from copilot_common import REASONING_EFFORTS, RUNTIME, SCHEMA, cli_version, load_module, model_for, now
from copilot_common import read_json, reasoning_for
from portable import run as process_run

PACKAGE = load_module("copilot-package")
RESULTS = load_module("copilot-results")


def launch(command, timeout):
    return process_run(command, cwd=command[command.index("-C") + 1], timeout=timeout,
                       stdout=sys.stderr)


def validate_verdict(directory, run_id, started, target, severity=None):
    directory = Path(directory)
    manifest = PACKAGE.fresh(directory)
    verdict = read_json(directory / "gate-verdict.json")
    if not isinstance(verdict, dict):
        raise ValueError("verdict must be a JSON object")
    if (manifest["runId"] != run_id or verdict.get("runId") != run_id
            or verdict.get("schemaVersion") != SCHEMA or verdict.get("harness") != "copilot"
            or verdict.get("packageHash") != manifest["packageHash"]
            or verdict.get("reviewedSha") != manifest["head"]):
        raise ValueError("missing or mismatched run-bound verdict")
    if manifest.get("requestedTarget") != target:
        # The coordinator records the original request in scope, separate from
        # resolved target revisions so equivalent targets stay auditable.
        if manifest["scope"].get("requestedTarget") != target:
            raise ValueError("verdict does not match the requested target")
    if not isinstance(verdict.get("generated_at"), str) or not started <= verdict["generated_at"] <= now():
        raise ValueError("stale or future verdict timestamp")
    # Recompute from validated receipts, never trust a model-authored pass boolean.
    computed = RESULTS.finalize(directory, severity, persist=False)
    for field in ("pass", "partial", "blockingCount", "blocking", "summary", "severityFloor", "missingAssignments"):
        if verdict.get(field) != computed[field]:
            raise ValueError(f"verdict disagrees with validated results: {field}")
    return 0 if computed["pass"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", type=Path)
    parser.add_argument("--copilot", default="copilot")
    parser.add_argument("--model", help="explicit coordinator model ID")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, help="coordinator reasoning effort (default: high)")
    parser.add_argument("--severity", choices=["critical", "important", "debt", "suggested", "question"])
    parser.add_argument("--no-custom-instructions", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--allow-tools", action="store_true", help="explicitly authorize checks/tools for headless review")
    parser.add_argument("target", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        cli_version(args.copilot)
        if not args.allow_tools:
            raise ValueError("--allow-tools is required: review executes repository checks; use only trusted code")
        repo = args.repo.resolve()
        config_path = repo / ".copilot/review-all.json"
        config = read_json(config_path) if config_path.exists() else {}
        model = args.model if args.model is not None else model_for("coordinator", config)
        reasoning_effort = args.reasoning_effort if args.reasoning_effort is not None else reasoning_for(config, "coordinator")
        run_id, started = str(uuid.uuid4()), now()
        target = " ".join(args.target[1:] if args.target[:1] == ["--"] else args.target)
        instruction_mode = "disabled" if args.no_custom_instructions else "enabled"
        prompt = (
            f"Invoke the native /review-all skill in gate mode. Review target: {json.dumps(target)}. "
            f"Requested runId: {run_id}. Record scope.requestedTarget exactly as {json.dumps(target)}. "
            f"Record instruction mode {instruction_mode}; inspect actual disabled-source settings. "
            "Use named custom subagents, not top-level --agent or the built-in code-review agent. "
            "Run the full workflow and finalize with the installed uv review-all copilot-results CLI. Do not persist lifecycle state; "
            "the wrapper does that after validation. No menus, no questions, "
            "no edits to reviewed source and no publication. Never manually fabricate a verdict."
        )
        if args.severity:
            prompt += f" Finalize with --severity {args.severity}."
        command = [args.copilot, "-C", str(repo), "-p", prompt, "--allow-all-tools", "--no-ask-user",
                   "--no-auto-update", "--model", model, "--reasoning-effort", reasoning_effort]
        if args.no_custom_instructions:
            command.append("--no-custom-instructions")
        proc = launch(command, args.timeout)
        if proc.returncode:
            raise ValueError(f"Copilot failed with exit {proc.returncode}; no valid gate completion")
        directory = repo / RUNTIME / "runs" / run_id
        code = validate_verdict(directory, run_id, started, target, args.severity)
        RESULTS.persist_state(directory)
        return code
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"copilot-gate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
