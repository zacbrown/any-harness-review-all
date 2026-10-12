#!/usr/bin/env python3
"""Opt-in real Copilot child-dispatch handoff test (uses AI credits).

REVIEW_ALL_COPILOT_INTEGRATION=1 python3 tests/copilot_integration.py --out /tmp/evidence
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/review-all/scripts"
sys.path.insert(0, str(SCRIPTS))
from copilot_common import REASONING_EFFORTS, atomic, cli_version, load_module, model_for, read_json, reasoning_for  # noqa: E402
from portable import run

INSTALL = load_module("copilot-install")
PACKAGE = load_module("copilot-package")
RESULTS = load_module("copilot-results")


def git(repo, *args):
    run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def write(repo, path, content):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def events(path):
    parsed = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            parsed.append(json.loads(line))
        except ValueError:
            continue  # CLI startup notices are not structured events.
    return parsed


def child_calls(stream):
    calls = []
    for event in stream:
        if event.get("type") == "assistant.message":
            calls.extend(t for t in event.get("data", {}).get("toolRequests", []) if t.get("name") == "task")
    return calls


def runtime_model_errors(stream, expected_models, token_roles=None):
    # Correlate runtime agent IDs with result-envelope roles, then check every
    # exposed identity, including messages emitted before the final result.
    # effectiveModel inside child-authored JSON is not runtime provenance.
    roles = {}
    for event in stream:
        data = event.get("data", {})
        if event.get("agentId") and event.get("type") == "assistant.message" and data.get("content"):
            try:
                result = json.loads(data["content"])
            except ValueError:
                continue
            if isinstance(result, dict):
                role = (token_roles or {}).get(result.get("dispatchToken"), result.get("axis"))
                if role in expected_models:
                    roles[event["agentId"]] = role
    errors = []
    for event in stream:
        axis = roles.get(event.get("agentId"))
        model = event.get("data", {}).get("model")
        if axis and model and model != expected_models[axis]:
            errors.append(f"{axis} effective model {model!r} differs from requested {expected_models[axis]!r} "
                          f"(agent {event['agentId']})")
    return errors


def source_read_errors(stream, ledger, plan):
    errors = []
    for assignment in plan["assignments"].values():
        job = ledger["jobs"].get(assignment["id"], {})
        if job.get("status") != "complete":
            continue
        agent_ids = set()
        for event in stream:
            if event.get("agentId") and event.get("type") == "assistant.message":
                try:
                    result = json.loads(event["data"].get("content", ""))
                except ValueError:
                    continue
                if result == job.get("rawResult", job["result"]):
                    agent_ids.add(event["agentId"])
        reads = set()

        def paths(value):
            if isinstance(value, dict):
                if isinstance(value.get("path"), str):
                    reads.add(value["path"])
                for child in value.values():
                    paths(child)
            elif isinstance(value, list):
                for child in value:
                    paths(child)
        for event in stream:
            if event.get("agentId") in agent_ids and event.get("type") == "assistant.message":
                for tool in event["data"].get("toolRequests", []):
                    if tool.get("name") in {"view", "multi_tool_use.parallel"}:
                        paths(tool.get("arguments"))
        for name, source in assignment["sources"].items():
            original_paths = set(source["parts"]) | {source["originalPath"], source["snapshotPath"]}
            if not reads & original_paths:
                errors.append(f"no actual verifier source-read tool call for {name}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="persistent evidence directory")
    parser.add_argument("--copilot", default="copilot")
    parser.add_argument("--model", default=model_for("coordinator", {}), help="parent coordinator model")
    parser.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default=reasoning_for({}))
    parser.add_argument("--reviewer-model", default=model_for("01-standards", {}))
    parser.add_argument("--reviewer-reasoning-effort", choices=REASONING_EFFORTS,
                        default=reasoning_for({}, "01-standards"))
    parser.add_argument("--verifier-model", default=model_for("verifier", {}))
    parser.add_argument("--verifier-reasoning-effort", choices=REASONING_EFFORTS,
                        default=reasoning_for({}, "verifier"))
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if os.environ.get("REVIEW_ALL_COPILOT_INTEGRATION") != "1":
        print("SKIP: set REVIEW_ALL_COPILOT_INTEGRATION=1 to spend AI credits on real subagents")
        return 0
    version = cli_version(args.copilot)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # Preserve the fixture and evidence for inspection; never confuse a skipped
    # or failed model run with passing deterministic tests.
    repo = Path(tempfile.mkdtemp(prefix="fixture-", dir=out))
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "review-all integration")
    git(repo, "config", "user.email", "integration@example.invalid")
    config = {"models": {"01-standards": args.reviewer_model}, "verifierModel": args.verifier_model,
              "reasoningEfforts": {"01-standards": args.reviewer_reasoning_effort,
                                   "verifier": args.verifier_reasoning_effort}}
    INSTALL.install(repo / ".github", config)
    INSTALL.provision(repo / ".github")
    write(repo, "AGENTS.md", "Repository contract: every public CONFIG dictionary must contain audit_tag. "
          "Report a newly missing audit_tag as IMPORTANT with static proof.\n")
    write(repo, ".github/instructions/scoped.instructions.md",
          '---\napplyTo: "billing/**/*.py"\n---\n'
          "Billing CONFIG dictionaries must include currency_unit. This rule applies ONLY to billing/. "
          "Report a newly missing currency_unit as IMPORTANT with static proof.\n")
    before = "CONFIG = {'audit_tag': 'present', 'currency_unit': 'cents', 'retention_days': 47, 'factor': 13}\n"
    write(repo, "billing/config.py", before)
    write(repo, "public/config.py", "CONFIG = {'audit_tag': 'present'}\n")
    write(repo, "verify_original.py", "ORIGINAL_MARKER = 'must-be-reread'\n")
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture baseline")
    write(repo, "billing/config.py", "CONFIG = {'retention_days': 7, 'factor': 9}\n")
    write(repo, "public/config.py", "CONFIG = {'audit_tag': 'updated'}\n")
    skip = [a for a in PACKAGE.AXES.AGENT_IDS if a != "01-standards"]
    write(repo, ".copilot/review-all.json", json.dumps({**config, "skipAgents": skip}))
    runtime = repo / ".copilot/review-all"
    runtime.mkdir(parents=True, exist_ok=True)
    write(repo, ".copilot/review-all/.gitignore", "*\n")
    write(repo, ".copilot/review-all/profile.json", '{"language":"python","purpose":"configuration contracts"}')
    checks = [{"name": n, "status": "N/A", "reason": "fixture"} for n in
              ("Typecheck", "Lint", "Tests", "Spec Existence", "Dependencies")]
    checks[2] = {"name": "Tests", "command": [sys.executable, "-c",
        "from billing.config import CONFIG; print('CHECK REQUIREMENT: factor must equal 13'); assert CONFIG['factor'] == 13"]}
    request = {"schemaVersion": 1, "runId": "context-handoff", "target": {"kind": "worktree"},
               "scope": {"paths": [], "exclude": []}, "requirements": str(runtime / "requirements.txt"),
               "profile": str(runtime / "profile.json"), "checks": checks,
               "supportingFiles": ["verify_original.py"],
               "instructions": {"mode": "enabled", "settingsConfirmed": True, "disabledPaths": [],
                                "parentSources": [], "confirmedExtraDirs": []}}
    atomic(runtime / "request.json", request)
    directory = runtime / "runs/context-handoff"
    first_id = "01-standards--chunk-0001"
    tool = repo / ".github/.review-all-tool"
    cli = f'uv run --project "{tool}" --locked --no-sync --no-python-downloads review-all --repo "{repo}"'
    prompt = (
        "Test the installed native review-all custom SUBAGENTS, not your own review ability. "
        "Requirement supplied ONLY in this parent conversation: billing CONFIG retention_days must be exactly 47; "
        "report a changed literal violating it as IMPORTANT. "
        f"First record that requirement explicitly in {runtime / 'requirements.txt'}; "
        "do not put it in repository instructions or source. "
        f'Run {cli} copilot-package prepare --request "{runtime / "request.json"}". '
        "Do not use a top-level --agent process. Use the task tool with the explicitly named custom agent. "
        f'Run {cli} copilot-results start "{directory}" --assignment {first_id}. '
        f"Dispatch agent_type review-all-standards with model {args.reviewer_model}, "
        f"reasoning_effort {args.reviewer_reasoning_effort}, and the returned prompt. "
        "Write the child's exact JSON response, unmodified, "
        f"to {directory / 'reviewer-result.json'}. "
        f'Then invoke {cli} copilot-results accept "{directory}" '
        f"--assignment {first_id} --result {directory / 'reviewer-result.json'}. "
        "If malformed, use start and retry once with the exact same inputs and save the raw retry. "
        f'Invoke {cli} copilot-results verifiers "{directory}". '
        "Read the resulting verification.json, start every verifier assignment with copilot-results.py start, "
        f"and dispatch agent_type review-all-verifier with model {args.verifier_model}, "
        f"reasoning_effort {args.verifier_reasoning_effort}, and that start prompt in a fresh context. "
        "Do not pass another child's exploration or judgments. Save each exact child JSON to verifier-result.json "
        "in the run directory and accept it through copilot-results.py (retry once if required). "
        "Stop after the reviewer and verifier results have been accepted. Do not finalize or persist lifecycle state. "
        "Do not edit reviewed source. No menus, publication or other subagents. "
        "Output a short completion summary only after the child results have been persisted."
    )
    command = [args.copilot, "-C", str(repo), "-p", prompt, "--model", args.model, "--allow-all-tools",
               "--reasoning-effort", args.reasoning_effort,
               "--no-ask-user", "--disable-builtin-mcps", "--no-auto-update", "--output-format", "json",
               "--add-dir", str(SCRIPTS), "--add-dir", str(out)]
    transcript = out / "transcript.jsonl"
    with transcript.open("w", encoding="utf-8") as stdout, (out / "stderr.txt").open("w", encoding="utf-8") as stderr:
        try:
            proc = run(command, cwd=repo, stdout=stdout, stderr=stderr, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            atomic(out / "result.json", {"passed": False, "cliVersion": version, "error": "CLI timeout", "repo": str(repo)})
            print(f"FAIL: CLI timeout; evidence in {out}", file=sys.stderr)
            return 1
    errors = []
    if proc.returncode:
        errors.append(f"CLI exited {proc.returncode}")
    stream = events(transcript)
    expected_models = {"01-standards": args.reviewer_model, "verifier": args.verifier_model}
    jobs = RESULTS.state(directory)["jobs"]
    token_roles = {job["dispatchToken"]: ("verifier" if job_id.startswith("verifier--") else
                                         job_id.split("--", 1)[0])
                   for job_id, job in jobs.items() if job.get("dispatchToken")}
    errors.extend(runtime_model_errors(stream, expected_models, token_roles))
    # A separate parent turn prevents the production skill's completion/menu
    # behavior from swallowing this deliberately broken, out-of-band probe.
    try:
        broken = read_json(directory / "assignments" / (first_id + ".json"))
        broken["id"] = "missing-context-probe"
        broken["mandatory"].append(str(runtime / "intentionally-missing.txt"))
        atomic(runtime / "missing-assignment.json", broken)
        missing_prompt = (
            "This is one isolated custom-subagent protocol probe, NOT a full review workflow. "
            f"Use task exactly once with agent_type review-all-standards, model {args.reviewer_model}, "
            f"reasoning_effort {args.reviewer_reasoning_effort}, mode sync. "
            f"Child prompt: Read {runtime / 'missing-assignment.json'} and EVERY mandatory input. Attempt 1. "
            "Return ONLY the result-contract JSON object. A mandatory file is intentionally missing: "
            "report status incomplete and missingInputs, not no findings. Do not create missing files. "
            "Do not edit any files or run a skill, review workflow, or checks. "
            "After the child returns, end your turn. The test driver captures its response directly."
        )
        missing_command = command.copy()
        missing_command[missing_command.index("-p") + 1] = missing_prompt
        with (out / "missing-transcript.jsonl").open("w", encoding="utf-8") as stdout, (out / "missing-stderr.txt").open("w", encoding="utf-8") as stderr:
            missing_proc = run(missing_command, cwd=repo, stdout=stdout, stderr=stderr, timeout=args.timeout)
        if missing_proc.returncode:
            errors.append(f"missing-context parent exited {missing_proc.returncode}")
        missing_stream = events(out / "missing-transcript.jsonl")
        errors.extend(runtime_model_errors(missing_stream, expected_models))
        stream += missing_stream
        for event in missing_stream:
            if event.get("agentId") and event.get("type") == "assistant.message" and event.get("data", {}).get("content"):
                raw = json.loads(event["data"]["content"])
                if isinstance(raw, dict) and raw.get("assignmentId") == "missing-context-probe":
                    atomic(directory / "missing-result.json", raw)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        errors.append(f"missing-context probe: {error}")
    calls = child_calls(stream)
    for call in calls:
        arguments = call.get("arguments", {})
        expected = {"review-all-standards": (args.reviewer_model, args.reviewer_reasoning_effort),
                    "review-all-verifier": (args.verifier_model, args.verifier_reasoning_effort)}
        agent = arguments.get("agent_type", "")
        if agent.startswith("review-all-") and (
                agent not in expected or (arguments.get("model"), arguments.get("reasoning_effort")) != expected[agent]):
            errors.append("custom subagent dispatch did not use its role's requested model and reasoning effort")
    call_text = json.dumps(calls)
    for name in ("review-all-standards", "review-all-verifier"):
        if name not in call_text:
            errors.append(f"no actual task-tool dispatch of {name} recorded")
    try:
        ledger = RESULTS.state(directory)
        reviewer = ledger["jobs"][first_id]
        if reviewer["status"] != "complete":
            errors.append("reviewer failed result validation")
        findings = reviewer["result"]["findings"]
        for token in ("audit_tag", "currency_unit", "retention_days", "factor"):
            if not any(token in json.dumps(f) for f in findings):
                errors.append(f"missing context-dependent finding: {token}")
        if any(f["file"] == "public/config.py" and "currency_unit" in json.dumps(f) for f in findings):
            errors.append("path-specific rule incorrectly applied to public/")
        plan = read_json(directory / "verification.json")
        if not plan["assignments"]:
            errors.append("no independent verifier")
        for job in plan["assignments"].values():
            result = ledger["jobs"][job["id"]]
            if result["status"] != "complete":
                errors.append("verifier failed original-source/coverage validation")
        child_results = []
        identities = []
        for event in stream:
            data = event.get("data", {})
            if event.get("agentId") and event.get("type") == "assistant.message":
                identities.append({"agentId": event["agentId"], "model": data.get("model"), "provider": data.get("providerId")})
                if not data.get("content"):
                    continue
                try:
                    child_results.append(json.loads(data["content"]))
                except ValueError:
                    continue
        for job in ledger["jobs"].values():
            if job["status"] == "complete" and job.get("rawResult", job["result"]) not in child_results:
                errors.append("coordinator altered a raw child result before acceptance")
        errors.extend(source_read_errors(stream, ledger, plan))
        missing = read_json(directory / "missing-result.json")
        if missing["status"] != "incomplete" or not missing["missingInputs"]:
            errors.append("missing mandatory context did not produce incomplete")
        PACKAGE.fresh(directory)
    except (OSError, ValueError, KeyError, TypeError) as error:
        errors.append(str(error))
    outcome = {"passed": not errors, "cliVersion": version, "model": args.model,
               "reasoningEffort": args.reasoning_effort, "reviewConfig": config, "errors": errors,
               "repo": str(repo), "run": str(directory), "taskCalls": len(calls),
               "runtimeIdentities": identities if "identities" in locals() else [],
               "tests": ["root instructions", "path-specific instructions", "parent-only requirement",
                         "failed check evidence", "independent verifier original source", "missing mandatory context"]}
    atomic(out / "result.json", outcome)
    print(json.dumps(outcome, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
