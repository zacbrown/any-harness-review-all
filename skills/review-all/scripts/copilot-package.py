#!/usr/bin/env python3
"""Prepare versioned file-backed inputs; the Copilot coordinator owns orchestration."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import uuid

from copilot_common import RUNTIME, SCHEMA, SKILL, adapt, agent_id, atomic, body, cli_version
from copilot_common import digest, encoded, file_hash, git, load_module, model_for, now, read_json
from copilot_common import persona_text, reasoning_for, resource_hashes, run, safe_path, state_path
from copilot_instructions import collect
from portable import environment_identity, execute

SELECT = load_module("select-files")
AXES = load_module("select-agents")
ORDER = load_module("agent-order")
PART_BYTES = 12000


def tree_hashes(repo):
    names = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
    return {n: file_hash(safe_path(repo, n)) for n in sorted(set(names)) if n
            and not n.startswith((RUNTIME + "/", ".copilot/reports/"))}


def diff_args(repo, target):
    kind = target["kind"]
    if kind == "staged":
        return ["--cached"], "index"
    if kind == "unstaged":
        return [], "worktree"
    if kind == "worktree":
        return ["HEAD"], "worktree"
    if kind == "range":
        base = git(repo, "rev-parse", "--verify", target["base"] + "^{commit}").decode().strip()
        head = git(repo, "rev-parse", "--verify", target["head"] + "^{commit}").decode().strip()
        return [base, head], head
    if kind == "pr":
        pr = str(int(target["number"]))
        info = json.loads(run(["gh", "pr", "view", pr, "--json", "baseRefOid,headRefOid,title,body"], repo))
        target["metadata"] = info
        # Object availability is mandatory: never verify a PR against unrelated local HEAD.
        for sha in (info["baseRefOid"], info["headRefOid"]):
            git(repo, "cat-file", "-e", sha + "^{commit}")
        base = git(repo, "merge-base", info["baseRefOid"], info["headRefOid"]).decode().strip()
        return [base, info["headRefOid"]], info["headRefOid"]
    raise ValueError(f"unsupported resolved target kind: {kind}")


def target_records(repo, args):
    options = ["--no-ext-diff", "--no-textconv", "--find-renames"]
    data = git(repo, "diff", *options, "--name-status", "-z", *args).decode().split("\0")
    records = []
    items = iter(data[:-1])
    for status in items:
        name = next(items)
        old_name = name if status.startswith(("R", "C")) else None
        if old_name is not None:
            name = next(items)
        safe_path(repo, name)
        pathspec = [old_name, name] if old_name else [name]
        raw = git(repo, "diff", *options, "--binary", *args, "--", *pathspec)
        nums = git(repo, "diff", *options, "--numstat", *args, "--", *pathspec).split(b"\t", 2)
        binary = nums[0] == b"-"
        records.append({"path": name, "oldPath": old_name, "status": status[0], "bytes": len(raw), "_raw": raw,
                        "insertions": None if binary else int(nums[0]),
                        "deletions": None if binary else int(nums[1]),
                        "diff": raw.decode("utf-8", errors="replace")})
    return records


def select(records, scope, config):
    classes = SELECT.load_classes(SELECT.DEFAULT_CLASSES_PATH)
    includes, excludes = scope.get("paths", []), scope.get("exclude", [])
    scoped = [r for r in records if not includes or any(r["path"].startswith(p) for p in includes)]
    secret_rules = SELECT.compile_rule_list(classes["secret"])
    renamed_secrets = [r for r in scoped if r.get("oldPath") and SELECT.first_match(secret_rules, r["oldPath"])]
    renamed_secret_paths = {r["path"] for r in renamed_secrets}
    result = SELECT.select(
        [r for r in scoped if r["path"] not in renamed_secret_paths], secret_rules, excludes, includes,
        SELECT.compile_rule_list(classes["exclude"]), config.get("maxFileDiffBytes", 160000),
        SELECT.compile_class_rules(classes["classes"]),
        SELECT.compile_content_signals(classes.get("content_signals", {})))
    result["excluded"] += [{"path": r["path"], "reason": "secret",
                           "pattern": SELECT.first_match(secret_rules, r["oldPath"])} for r in renamed_secrets]
    result["counts"]["total"] += len(renamed_secrets)
    result["counts"]["excluded"]["secret"] += len(renamed_secrets)
    result["outsideScope"] = [r["path"] for r in records if r not in scoped]
    decisions = AXES.build_agents(result["reviewable"], config.get("extraAgents", []), config.get("skipAgents", []))
    for axis, decision in decisions.items():
        if decision["spawn"] and axis not in AXES.AGENT_IDS:
            raise ValueError(f"extraAgents profile {axis!r} is not installed by this adapter; "
                             "add a canonical persona and native profile before using it")
    pack_rules = [(p, AXES.compile_glob(p), pack) for p, pack in classes["rule_packs"].items()]
    return result, {"agents": decisions, "rule_packs": AXES.group_by_pack(result["reviewable"], pack_rules)}


def split_bytes(data, limit):
    if not data:
        return [b""]
    parts = []
    while data:
        end = min(limit, len(data))
        if end < len(data):
            newline = data.rfind(b"\n", 0, end)
            if newline >= 0:
                end = newline + 1
            else:
                while end and data[end] & 0xC0 == 0x80:
                    end -= 1
        if end == 0:
            raise ValueError("chunk byte limit too small for UTF-8 input")
        parts.append(data[:end])
        data = data[end:]
    return parts


def chunks(records, config):
    max_files, max_bytes = config.get("chunkMaxFiles", 40), config.get("chunkMaxBytes", 200000)
    groups, current, size = [], [], 0
    for record in records:
        raw = record["_raw"]
        pieces = split_bytes(raw, max_bytes) if max_bytes else [raw]
        for index, piece in enumerate(pieces):
            if current and ((max_files and len(current) >= max_files) or (max_bytes and size + len(piece) > max_bytes)):
                groups.append(current)
                current, size = [], 0
            current.append({"path": record["path"], "part": index + 1, "parts": len(pieces), "data": piece})
            size += len(piece)
    if current:
        groups.append(current)
    return groups


class Writer:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.artifacts = {}

    def store(self, name, data):
        data = data if isinstance(data, bytes) else encoded(data) + b"\n"
        paths = []
        for i, part in enumerate(split_bytes(data, PART_BYTES), 1):
            rel = f"inputs/{name}.{i:04d}.txt"
            atomic(self.directory / rel, part)
            self.artifacts[rel] = digest(part)
            paths.append(str(self.directory / rel))
        return paths


def source_bytes(repo, revision, name):
    if revision == "worktree":
        path = safe_path(repo, name)
        return os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
    return git(repo, "show", (":" if revision == "index" else revision + ":") + name)


def checks(repo, specs, writer, target_matches=True):
    results = []
    required = {"Typecheck", "Lint", "Tests", "Spec Existence", "Dependencies"}
    if {c["name"] for c in specs} != required or len(specs) != len(required):
        raise ValueError(f"checks must account for exactly {sorted(required)}")
    for spec in specs:
        started = now()
        if spec.get("command") and not target_matches:
            results.append({"name": spec["name"], "command": spec["command"], "status": "SKIP",
                            "reason": "live checkout differs from reviewed revision; checks would not prove this target",
                            "exitCode": None, "startedAt": started, "output": [], "cwd": str(repo)})
            continue
        if spec.get("command"):
            argv = spec["command"]
            if not isinstance(argv, list) or not all(isinstance(s, str) for s in argv):
                raise ValueError("check command must be an argv array, not shell text")
            elapsed = None
            try:
                proc = execute(argv, cwd=repo, timeout=spec.get("timeoutSeconds", 180), merge_stderr=True)
                code, output, elapsed = proc.returncode, proc.stdout, proc.elapsed
                status = "TIMEOUT" if proc.timed_out else "PASS" if code == 0 else "FAIL"
            except OSError as error:
                code, output, status = None, str(error).encode(), "SKIP"
            paths = writer.store("check-" + spec["name"].replace(" ", "-"), output)
            results.append({"name": spec["name"], "command": argv, "exitCode": code, "status": status,
                            "reason": output.decode(errors="replace") if status == "SKIP" else None,
                            "startedAt": started, "finishedAt": now(), "output": paths, "cwd": str(repo),
                            "elapsedSeconds": elapsed, "timedOut": status == "TIMEOUT"})
        else:
            if spec.get("status") not in {"SKIP", "N/A"} or not spec.get("reason"):
                raise ValueError(f"{spec['name']}: nonexecuted checks require SKIP/N/A and reason")
            results.append({**spec, "exitCode": None, "startedAt": started, "output": []})
    return results


def prepare(repo, request_path, directory=None, binary="copilot"):
    repo = Path(repo).resolve()
    request_path = Path(request_path).resolve()
    request = read_json(request_path)
    if request.get("schemaVersion") != SCHEMA:
        raise ValueError("unsupported request schemaVersion")
    version = cli_version(binary)
    root = repo / RUNTIME
    state_path(repo, RUNTIME + "/.gitignore")
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".gitignore").exists():
        atomic(root / ".gitignore", b"*\n")
    run_id = request.get("runId", str(uuid.uuid4()))
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,80}", run_id):
        raise ValueError("invalid runId")
    directory = Path(directory).resolve() if directory else root / "runs" / run_id
    if directory.exists():
        raise ValueError(f"run directory already exists: {directory}")
    directory.mkdir(parents=True)
    writer = Writer(directory)
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    baseline = tree_hashes(repo)
    config_path = repo / ".copilot/review-all.json"
    config = read_json(config_path) if config_path.exists() else {}
    reasoning_for(config)  # Validate shared and per-role settings before dispatch.
    for key in ("chunkMaxBytes", "chunkMaxFiles", "maxFileDiffBytes", "quotaDebt", "quotaSuggested", "quotaQuestion"):
        if key in config and (type(config[key]) is not int or config[key] < 0):
            raise ValueError(f"{key} must be a nonnegative integer")
    for key in ("stateFile", "historyFile", "checkpointDir", "snoozeFile"):
        if key in config:
            state_path(repo, config[key])
    if config.get("verifierVotes", 1) < 1:
        raise ValueError("verifierVotes must be >= 1")
    watched = {str(request_path): file_hash(request_path), str(config_path): file_hash(config_path)}
    context = {}
    for key in ("requirements", "profile"):
        path = Path(request[key]).resolve()
        watched[str(path)] = file_hash(path)
        context[key] = writer.store(key, path.read_bytes())
    target = dict(request["target"])
    args, revision = diff_args(repo, target)
    reviewed_head = revision if target["kind"] in {"range", "pr"} else head
    records = target_records(repo, args)
    selection, decisions = select(records, request["scope"], config)
    secret_rules = SELECT.compile_rule_list(SELECT.load_classes(SELECT.DEFAULT_CLASSES_PATH)["secret"])
    renamed_secret_paths = {r["path"] for r in records if r.get("oldPath")
                            and SELECT.first_match(secret_rules, r["oldPath"])}
    for name in request.get("supportingFiles", []):
        if name in renamed_secret_paths or SELECT.first_match(secret_rules, name):
            raise ValueError(f"supporting context matches secret exclusion: {name}")
    paths = [r["path"] for r in selection["reviewable"]]
    selected = [r for r in records if r["path"] in paths]
    instruction_files = list(dict.fromkeys(paths + request.get("supportingFiles", [])))
    instructions, instruction_hashes, errors = collect(repo, instruction_files, request["instructions"])
    instruction_errors = list(errors)
    watched.update(instruction_hashes)
    # REVIEW.md is review configuration, not an @-importing instruction format.
    # Respect the global switch and explicit per-file disable list here as well.
    review_path = repo / "REVIEW.md"
    policy = request["instructions"]
    review_enabled = policy["mode"] == "enabled" and str(review_path) not in policy.get("disabledPaths", [])
    if review_enabled:
        watched[str(review_path)] = file_hash(review_path)
    context["reviewInstructions"] = writer.store("REVIEW", review_path.read_bytes()
        if review_enabled and review_path.exists() else b"REVIEW.md absent or intentionally disabled.\n")
    context["changedFiles"] = writer.store("changed-files", selection)
    context["config"] = writer.store("config", config)
    context["dismissals"] = writer.store("dismissals", [])
    all_dismissals = []
    history_path = state_path(repo, config.get("historyFile", RUNTIME + "/history.jsonl"))
    watched[str(history_path)] = file_hash(history_path)
    context["history"] = writer.store("history", history_path.read_bytes() if history_path.exists() else b"")
    lifecycle_path = state_path(repo, config.get("stateFile", RUNTIME + "/state.json"))
    watched[str(lifecycle_path)] = file_hash(lifecycle_path)
    if lifecycle_path.exists():
        state = read_json(lifecycle_path)
        if state.get("harness") != "copilot":
            errors.append("state lacks harness=copilot; refusing Claude or unidentified lifecycle state")
        else:
            # Human decisions travel; machine decisions cannot be reused without
            # a complete, proven runtime identity and input fingerprint.
            entries = [{"root_cause_key": k, **v} for k, v in state["findings"].items()
                       if v.get("status") == "wontfix" or
                       (v.get("status") == "snoozed" and v.get("snoozed_until", "") > now())]
            all_dismissals = entries
            context["dismissals"] = writer.store("dismissals", sorted(entries,
                key=lambda x: x.get("last_seen_at", ""), reverse=True)[:30])
    untracked = [n for n in git(repo, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\0")
                 if n and not n.startswith((RUNTIME + "/", ".copilot/reports/"))]
    target_matches = revision == "worktree" or (not untracked and not git(
        repo, "diff", "--no-ext-diff", "--no-textconv", "--name-only",
        *([] if revision == "index" else [revision])))
    results = checks(repo, request["checks"], writer, target_matches)
    context["checks"] = writer.store("checks", results)
    context["checkOutput"] = [p for row in results for p in row["output"]]
    if tree_hashes(repo) != baseline:
        raise ValueError("reviewed inputs changed while checks ran; prepare a fresh run")
    context["protocol"] = writer.store("protocol", (SKILL / "copilot/dispatch.md").read_bytes())
    contract = (SKILL / "copilot/result-contract.md").read_text(encoding="utf-8").split("## Verifier findings", 1)[0].encode()
    context["resultContract"] = writer.store("result-contract", contract)
    shared = adapt(body((SKILL / "agents/_shared.md").read_text(encoding="utf-8")))
    for tier, key, default in [("debt", "quotaDebt", 5), ("suggested", "quotaSuggested", 3), ("question", "quotaQuestion", 2)]:
        shared = shared.replace("${quota." + tier + "}", str(config.get(key, default)))
    shared = re.sub(r"\$\{codegraphTools\.\w+\}", "unavailable (use native search)", shared)
    context["sharedRules"] = writer.store("shared", shared.encode())
    sources = {}
    for record in selected:
        name = record["path"]
        source_revision = revision
        if record["status"] == "D":
            source_revision = ("index" if target["kind"] == "unstaged" else
                               args[0] if target["kind"] in {"range", "pr"} else head)
            data = git(repo, "show", (":" if source_revision == "index" else source_revision + ":") + name)
        else:
            data = source_bytes(repo, revision, name)
        snapshot = safe_path(directory / "sources", name)
        atomic(snapshot, data)
        writer.artifacts[str(snapshot.relative_to(directory))] = digest(data)
        sources[name] = {"originalPath": str(repo / name), "snapshotPath": str(snapshot), "revision": source_revision,
                         "deleted": record["status"] == "D",
                         "parts": writer.store("source-" + digest(name.encode())[:16], data),
                         "sha256": digest(data)}
    for name in request.get("supportingFiles", []):
        data = source_bytes(repo, revision, name)
        snapshot = safe_path(directory / "sources", name)
        atomic(snapshot, data)
        writer.artifacts[str(snapshot.relative_to(directory))] = digest(data)
        sources[name] = {"originalPath": str(safe_path(repo, name)), "snapshotPath": str(snapshot),
                         "revision": revision, "deleted": False,
                         "parts": writer.store("source-" + digest(name.encode())[:16], data), "sha256": digest(data)}
    exact_diff = b"".join(r["_raw"] for r in selected)
    context["exactDiff"] = writer.store("reviewed-diff", exact_diff)
    canonical = chunks(selected, config)
    orders = ORDER.build_orders(paths, 10)
    assignments = {}
    common = [p for key, values in context.items() if key != "exactDiff" for p in values]
    for axis, decision in decisions["agents"].items():
        if not decision["spawn"]:
            continue
        persona = persona_text(axis)
        persona_paths = writer.store("persona-" + axis, persona.encode())
        for chunk_index, chunk in enumerate(canonical, 1):
            members = [part for part in chunk if part["path"] in decision["files"]]
            if not members:
                continue
            chunk_id = f"chunk-{chunk_index:04d}"
            assignment_id = axis + "--" + chunk_id
            if len({m["path"] for m in members}) >= 3:
                order = orders[str(int(axis[:2]))]
                members.sort(key=lambda m: order.index(m["path"]))
            names = list(dict.fromkeys(m["path"] for m in members))
            mandatory = common + persona_paths
            scoped_names = names + request.get("supportingFiles", [])
            scoped_instructions = [{**item, "files": [f for f in item["files"] if f in scoped_names]}
                                   for item in instructions if set(item["files"]) & set(scoped_names)]
            mandatory += writer.store(assignment_id + "-instructions", scoped_instructions)
            if config.get("languageRules", True):
                for pack, pack_files in decisions["rule_packs"].items():
                    if set(pack_files) & set(scoped_names):
                        mandatory += writer.store(assignment_id + "-rules-" + pack, (SKILL / "rules" / pack).read_bytes())
            for part in members:
                mandatory += writer.store(assignment_id + "-diff-" + str(len(mandatory)), part["data"])
            for name in names + [p for p in request.get("supportingFiles", []) if p not in names]:
                mandatory += sources[name]["parts"]
            if axis == "04-consistency-history":
                history = git(repo, "log", "-10", "--oneline",
                              revision if revision not in {"worktree", "index"} else "HEAD", "--", *names)
                mandatory += writer.store(assignment_id + "-history", history)
            assignments[assignment_id] = {
                "schemaVersion": SCHEMA, "id": assignment_id, "kind": "reviewer", "axis": axis,
                "agent": agent_id(axis), "model": model_for(axis, config),
                "reasoningEffort": reasoning_for(config, axis),
                "chunks": [chunk_id], "files": names,
                "diffParts": [{k: v for k, v in m.items() if k != "data"} for m in members],
                "mandatory": list(dict.fromkeys(mandatory)), "sources": {n: sources[n] for n in names + request.get("supportingFiles", [])},
                "contextErrors": errors, "timeoutSeconds": config.get("agentTimeoutSeconds", 600),
            }
    manifest = {"schemaVersion": SCHEMA, "harness": "copilot", "runId": run_id, "createdAt": now(),
                "repo": str(repo), "head": reviewed_head, "localHead": head,
                "target": target, "diffArgs": args, "sourceRevision": revision,
                "indexHash": digest(git(repo, "ls-files", "--stage", "-z")),
                "checksMatchTarget": target_matches,
                "diffSha256": digest(exact_diff), "scope": request["scope"], "selection": selection,
                "decisions": decisions, "config": config, "instructions": instructions,
                "instructionPolicy": policy, "instructionFiles": instruction_files,
                "instructionErrors": instruction_errors, "contextErrors": errors,
                "sources": sources, "context": context, "checks": results, "dismissals": all_dismissals,
                "watched": watched, "treeHashes": baseline, "resources": resource_hashes(),
                "runtime": {"cliVersion": version, "identity": request.get("runtimeIdentity"),
                            "environment": environment_identity(),
                            "reuseEnabled": False,
                            "reuseReason": "effective per-subagent model/provider identity cannot be independently established"},
                "canonicalChunks": [[{k: v for k, v in p.items() if k != "data"} for p in c] for c in canonical],
                "assignments": assignments, "artifacts": writer.artifacts}
    manifest["inputFingerprint"] = digest(encoded({
        k: manifest[k] for k in ("head", "localHead", "indexHash", "target", "diffSha256", "scope", "config",
                                "instructions", "watched", "treeHashes", "resources", "runtime", "checks")}))
    manifest["packageHash"] = package_hash(manifest)
    for assignment in assignments.values():
        assignment.update({"runId": run_id, "packageHash": manifest["packageHash"]})
        atomic(directory / "assignments" / (assignment["id"] + ".json"), assignment)
    atomic(directory / "manifest.json", manifest)
    return directory


def fresh(directory):
    directory = Path(directory).resolve()
    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != SCHEMA or manifest.get("harness") != "copilot":
        raise ValueError("invalid package manifest")
    if package_hash(manifest) != manifest["packageHash"]:
        raise ValueError("package manifest integrity mismatch")
    if manifest["runtime"].get("environment") != environment_identity():
        raise ValueError("execution environment changed; prepare a fresh run")
    repo = Path(manifest["repo"])
    if git(repo, "rev-parse", "HEAD").decode().strip() != manifest["localHead"] or tree_hashes(repo) != manifest["treeHashes"]:
        raise ValueError("reviewed source/index inputs changed")
    if digest(git(repo, "ls-files", "--stage", "-z")) != manifest["indexHash"]:
        raise ValueError("reviewed index/diff changed")
    # Worktree content alone does not detect a different staged diff.
    records = target_records(repo, manifest["diffArgs"])
    selection, _ = select(records, manifest["scope"], manifest["config"])
    names = {r["path"] for r in selection["reviewable"]}
    if digest(b"".join(r["_raw"] for r in records if r["path"] in names)) != manifest["diffSha256"]:
        raise ValueError("reviewed diff changed")
    for path, expected in manifest["watched"].items():
        if file_hash(path) != expected:
            raise ValueError(f"review input changed or disappeared: {path}")
    instructions, _, errors = collect(repo, manifest["instructionFiles"], manifest["instructionPolicy"])
    if instructions != manifest["instructions"] or errors != manifest["instructionErrors"]:
        raise ValueError("instruction sources/settings changed")
    if resource_hashes() != manifest["resources"]:
        raise ValueError("skill resources changed")
    for relative, expected in manifest["artifacts"].items():
        if file_hash(safe_path(directory, relative)) != expected:
            raise ValueError(f"mandatory package input changed or missing: {relative}")
    for assignment in manifest["assignments"].values():
        if read_json(directory / "assignments" / (assignment["id"] + ".json")) != assignment:
            raise ValueError("assignment changed")
    return manifest


def package_hash(manifest):
    if not isinstance(manifest.get("assignments"), dict) or not all(
            isinstance(a, dict) for a in manifest["assignments"].values()):
        raise ValueError("invalid manifest assignments")
    payload = {k: v for k, v in manifest.items() if k != "packageHash"}
    payload["assignments"] = {
        k: {field: value for field, value in assignment.items() if field not in {"runId", "packageHash"}}
        for k, assignment in manifest["assignments"].items()}
    return digest(encoded(payload))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--repo", default=".")
    prep.add_argument("--request", required=True)
    prep.add_argument("--copilot", default="copilot")
    check = sub.add_parser("fresh")
    check.add_argument("directory")
    settings = sub.add_parser("model-settings", help="resolve follow-up/coordinator model settings")
    settings.add_argument("--repo", default=".", type=Path)
    settings.add_argument("--role", required=True, choices=AXES.AGENT_IDS + ["coordinator", "verifier", "judge", "deep-dive"])
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            print(prepare(args.repo, args.request, binary=args.copilot))
        elif args.action == "model-settings":
            path = args.repo.resolve() / ".copilot/review-all.json"
            config = read_json(path) if path.exists() else {}
            print(json.dumps({"role": args.role, "model": model_for(args.role, config),
                              "reasoning_effort": reasoning_for(config, args.role)}))
        else:
            manifest = fresh(args.directory)
            print(json.dumps({"runId": manifest["runId"], "fresh": True}))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"copilot-package: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
