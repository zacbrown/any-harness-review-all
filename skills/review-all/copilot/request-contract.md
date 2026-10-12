# Copilot request and cost planning

`request-example.json` is a complete staged request. `schemaVersion` must be 1.
`requirements` and `profile` are absolute paths to existing files, frozen at
prepare time. Requirements may intentionally be empty. A minimal project profile
is `{}`; it is a JSON object of discovered context, not runtime configuration.
For example:

```json
{"language":"rust","buildSystem":"cargo","rules":{"global":""}}
```

Include observed architecture, check commands and captured runtime evidence when
available. Do not invent evidence or put scoped instruction text into global
rules. Runtime settings belong in `.copilot/review-all.json`, written before
`prepare`.

## Target kinds

Replace `target` in the staged example with one of:

| Target | JSON | Compared content |
|---|---|---|
| Staged | `{"kind":"staged"}` | HEAD to index |
| Unstaged | `{"kind":"unstaged"}` | Index to worktree |
| Worktree | `{"kind":"worktree"}` | HEAD to worktree (tracked changes) |
| Range | `{"kind":"range","base":"HEAD~5","head":"HEAD"}` | Exact base to head commits |
| PR | `{"kind":"pr","number":123}` | Merge-base to PR head, using gh metadata and local objects |

A range is a two-endpoint diff. Resolve the merge base first when the user asks
for a branch comparison using merge-base semantics. All refs must resolve locally.
Deleted files use the preimage revision. Untracked files are not part of a Git
diff. `supportingFiles` is an optional list of repository-relative paths to read
from the target revision; it supplies context, not additional changed-file scope.

`scope.paths` contains repository-relative path prefixes; an empty list includes
the whole target. `scope.exclude` contains exclusion globs. Secret exclusions
always apply. Keep `selectionReason` and, for a wrapper run, `requestedTarget`
with the resolved scope. Preserve the wrapper's `runId` when supplied.

`checks` must account for exactly Typecheck, Lint, Tests, Spec Existence, and
Dependencies. Each entry either supplies `command` as an argv array with an
optional `timeoutSeconds`, or `status: "SKIP"|"N/A"` and a nonempty `reason`.
`prepare` executes commands; `plan` does not. `runtimeIdentity` may be null.

## Instruction settings

`instructions` requires `mode`, `settingsConfirmed`, `disabledPaths`,
`parentSources`, and `confirmedExtraDirs` as shown in the example. Confirm live
session settings in `/instructions` and use `copilot instruction list --json`
as an inventory only. A listing containing only home instructions does not prove
that project instructions are disabled or that all session sources were listed.
For a launch explicitly using `--no-custom-instructions`, record disabled mode.
If noninteractive settings cannot be confirmed, use
`{"mode":"unknown","settingsConfirmed":false,"disabledPaths":[],"parentSources":[],"confirmedExtraDirs":[]}`.
The resulting context is incomplete; do not assume enabled mode to obtain a pass.

## Large reviews and explicit cheaper coverage

Run `copilot-package plan --repo REPO --request REQUEST` before preparing.
It reports exact reviewer dispatch counts for the current diff/config, counts by
axis, a rough input-token estimate, verifier dispatch bounds and skipped axes.
The estimate uses repeated source/diff bytes divided by four plus 8,000 context
tokens per reviewer; it is not a tokenizer, wall-clock prediction or dollar quote.
Large instructions/check outputs and output/reasoning tokens add cost. Failed
jobs can require one retry; verifier cost depends on candidates. Rerun the plan
after scope/config changes.

Defaults remain 40 files / 200,000 diff bytes per canonical chunk. Assignment
JSON is pretty-printed for range reads; large sources and evidence use 12KB
parts. Lower limits only for actual context-budget constraints, not a 20KB
single-read display limit. Every fresh axis/chunk rereads common context and
complete source files, so smaller chunks can significantly increase cost.
Chunk numbers are global, and per-axis gaps are expected.

For an explicitly requested quick triage retaining standards, bugs/security and
test quality, a config can contain:

```json
{
  "skipAgents": ["03-dry-smells", "04-consistency-history", "05-simplification",
                 "06-security-deep-dive", "07-performance", "09-api-contract", "10-a11y-i18n"],
  "maxConcurrentAgents": 1
}
```

This omits dedicated security, performance, API and other checks; report those
omissions. It is not a full ten-axis review, and is never selected automatically.
Test quality still follows its activation rules; use `extraAgents` to explicitly
activate an installed canonical axis. Standards and DRY use the less expensive
packaged model; other reviewers use their configured models. Keep verification
enabled; lowering reviewer count does not justify skipping candidate verification.

`maxConcurrentAgents` defaults to 1 and must be a positive integer. Increase it
before prepare only to a capacity known from the current host. `start` enforces
the cap. Start one assignment immediately before its launch, or start one ready
wave with `--assignments ID1,ID2`; never reserve the entire run in advance.
Plugin starts require `--agent-namespace review-all`; manual installs omit it.
