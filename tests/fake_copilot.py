"""Deterministic CLI-boundary fixture, never a substitute for a model smoke test."""

import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/review-all/scripts"))
from copilot_common import atomic, load_module, read_json


def main():
    if "--version" in sys.argv:
        print("Copilot 1.0.95")
        return 0
    mode = os.environ.get("REVIEW_ALL_FAKE_MODE", "pass")
    if mode == "missing":
        return 0
    repo = Path(sys.argv[sys.argv.index("-C") + 1])
    prompt = sys.argv[sys.argv.index("-p") + 1]
    run_id = re.search(r"Requested runId: ([\w-]+)", prompt)[1]
    runtime = repo / ".copilot/review-all"
    atomic(runtime / ".gitignore", b"*\n")
    atomic(runtime / "requirements.txt", b"fixture\n")
    atomic(runtime / "profile.json", {})
    target = json.JSONDecoder().raw_decode(prompt.split("Record scope.requestedTarget exactly as ", 1)[1])[0]
    atomic(runtime / "request.json", {
        "schemaVersion": 1, "runId": run_id, "target": {"kind": "worktree"},
        "scope": {"paths": [], "exclude": [], "requestedTarget": target},
        "requirements": str(runtime / "requirements.txt"), "profile": str(runtime / "profile.json"),
        "instructions": {"mode": "disabled", "settingsConfirmed": True,
                         "disabledPaths": [], "parentSources": [], "confirmedExtraDirs": []},
        "checks": [{"name": name, "status": "N/A", "reason": "deterministic CLI fixture"} for name in
                   ("Typecheck", "Lint", "Tests", "Spec Existence", "Dependencies")]})
    package, results = load_module("copilot-package"), load_module("copilot-results")
    directory = package.prepare(repo, runtime / "request.json", binary=str(Path(__file__).resolve()))
    manifest = read_json(directory / "manifest.json")
    if mode != "partial":
        for assignment in manifest["assignments"].values():
            dispatch = results.start(directory, assignment["id"])
            output = directory / "result.json"
            atomic(output, {"schemaVersion": 1, "runId": run_id, "packageHash": manifest["packageHash"],
                            "assignmentId": assignment["id"], "axis": assignment["axis"],
                            "attempt": dispatch["attempt"], "status": "complete",
                            "chunks": assignment["chunks"], "readInputs": assignment["mandatory"],
                            "missingInputs": [], "effectiveModel": None,
                            "sourceFilesRead": list(assignment["sources"]), "findings": []})
            results.accept(directory, assignment["id"], output)
    results.make_verifiers(directory)
    verdict = results.finalize(directory)
    if mode in {"stale", "fabricated", "mismatched"}:
        field, value = {"stale": ("generated_at", "2000-01-01"),
                        "fabricated": ("blockingCount", 999),
                        "mismatched": ("runId", "wrong-run")}[mode]
        atomic(directory / "gate-verdict.json", {**verdict, field: value})
    if mode == "malformed":
        atomic(directory / "gate-verdict.json", b"not JSON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
