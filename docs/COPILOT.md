# Native GitHub Copilot CLI support

The Copilot adapter runs `/review-all` with ten namespaced custom reviewer
profiles, an independent `review-all-verifier`, and a read-only follow-up
`review-all-deep-dive` profile. It does not replace the
methodology with Copilot's built-in code-review agent. Both harnesses share the
Python CLI, native process runner and ownership-aware installation.

## Install and remove

Prerequisites: **Copilot CLI >=1.0.95**, authenticated; Python >=3.11; uv; Git.
Just >=1.33 is optional for named tasks.
See the [shared CLI contract](../skills/review-all/references/cli.md) for
direct uv commands, tested versions, shell/capability handling and platform
limitations. Windows 11 and live default-model gate evidence remain pending;
the historical macOS smoke below is not evidence of those results.

### Marketplace plugin

After this repository's packaging changes and the
[marketplace registration](#marketplace-registration) are published:

```text
copilot plugin marketplace add zacbrown/zbrown-copilot-marketplace
copilot plugin marketplace update zbrown-copilot-marketplace
copilot plugin install review-all@zbrown-copilot-marketplace
copilot plugin list
copilot skill list --json
```

For direct installation after the packaging changes are published, without a
marketplace, use `copilot plugin install zacbrown/any-harness-review-all`.
For local development, `copilot plugin install /absolute/path/to/any-harness-review-all`
installs the current checkout. Alternatively, preview without installing via
`copilot --plugin-dir /absolute/path/to/any-harness-review-all`.

The plugin contains **one `review-all` skill and twelve custom agent profiles**.
The manifest at `.github/plugin/plugin.json` explicitly selects
`.github/plugin/skills/` and `.github/plugin/agents/`, not the Claude skill at
`skills/review-all/SKILL.md`. The generated native entry point resolves shared
resources from `skills/review-all/` in the same plugin, with the adapted workflow
at `.github/plugin/workflow.md`. The existing `.claude-plugin/` package remains
independent.

**Runtime setup is a separate, explicit step.** Plugin installation does not
provision Python dependencies, run hooks, or copy profiles into your user/project
directories. In `copilot skill list --json`, find `review-all` with
`"source": "plugin"`; its `path` ends in `.github/plugin/skills/review-all`.
Resolve four parent directories from that path to get `<tool>` (the installed
plugin root, not the reviewed repository or original development checkout).
Run:

```text
uv run --no-project --no-python-downloads python "<tool>/skills/review-all/cli.py" setup
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<reviewed-repository>" discover --harness copilot
```

Use absolute paths and an installed Python >=3.11. If Python selection needs
to be explicit, add `--python /absolute/path/to/python` to the setup invocation.
Setup creates `<tool>/.venv`; normal reviews use `--no-sync` and never
bootstrap a runtime. Restart Copilot, confirm the twelve `review-all-*`
profiles in `/agent`, and use `/review-all`. The packaged model defaults and
availability requirements below still apply.

To update or remove:

```text
copilot plugin update review-all
copilot plugin uninstall review-all
```

After an update, resolve the installed skill path again, rerun setup, and
restart Copilot. Do not edit files in the plugin cache; updates replace them.
Removal is managed by Copilot and does not remove review state or configuration
in reviewed repositories. Do not use `just uninstall-copilot` to remove a plugin.

**Migrating from the manual installer:** first use `just uninstall-copilot`
and/or `just uninstall-copilot-project /path/to/repo` for the installations you
previously created. These commands preserve local edits; inspect any retained
files rather than deleting them blindly. User/project skills and agents take
precedence over plugin components and can silently shadow the plugin. If you
need customized profile model defaults, keep using the manual installer with
`--config` instead of installing both variants. Plugin profiles use the
repository's packaged model configuration.

### Marketplace registration

Add this object to the `plugins` array in
[`zacbrown/zbrown-copilot-marketplace/.github/plugin/marketplace.json`](https://github.com/zacbrown/zbrown-copilot-marketplace/blob/main/.github/plugin/marketplace.json)
after the generated files are published in this repository:

```json
{
  "name": "review-all",
  "description": "Native Copilot multi-axis code review with twelve custom agent profiles, deterministic checks, and independent verification. Requires Python >=3.11, uv, Git, and explicit runtime setup.",
  "source": {
    "source": "github",
    "repo": "zacbrown/any-harness-review-all"
  },
  "repository": "https://github.com/zacbrown/any-harness-review-all"
}
```

**Omit `source.path`: the plugin root is this repository's root**, not the
manifest directory. The Python project, lockfile, canonical resources and
generated components must all be present. The unpinned entry follows the
source repository's default branch, matching the marketplace's existing
entries. For a reproducible release, add `source.ref` or a full 40-character
`source.sha`; a catalog `version` alone does not pin the source.

The Copilot plugin version follows `pyproject.toml` (independent of the
upstream Claude plugin version). To prepare a release:

```text
just setup
just package-copilot
just package-copilot --check
just test --pattern test_copilot_plugin.py
```

Commit `.github/plugin/` alongside any changed source templates, personas,
model defaults or version. These are generated distribution files, not a second
maintained methodology; CI's deterministic suite rejects missing, stale or
unexpected generated output. Plugin consumers do not run a generation step.
The MIT license and original upstream attribution remain in `LICENSE`.

See GitHub's [plugin reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference)
for manifest paths, source pinning and component precedence. This uses the
supported legacy Copilot format for custom component paths, not the fixed-path
Agent Plugins format.

### Manual installation

Run `just setup` in the source checkout, then:

```text
# From this package checkout:
just install-copilot                   # ~/.copilot/skills/review-all and ~/.copilot/agents
just install-copilot-project /absolute/path/to/reviewed/repository

just uninstall-copilot
just uninstall-copilot-project /absolute/path/to/reviewed/repository
```

The project recipes default to the package checkout when no path is supplied.
Quote paths containing spaces.

`COPILOT_HOME` overrides `~/.copilot`. Project installation uses
`.github/skills/review-all/` and `.github/agents/`. Restart Copilot after
installation or changes to instructions/profiles. Resources resolve from the
installed skill's absolute path, even while reviewing another checkout.

Both the plugin generator and installer assemble personas and the shared traceability gate from the
canonical `skills/review-all/agents/` files. Full shared rules, resolved quotas,
language rules, phase references and scripts travel with the skill. There is
no independently maintained copy of the review methodology. Generated profiles
are bounded by the documented 30,000-character profile limit.

Ownership is recorded in `review-all-install.json` within `.github` or
`COPILOT_HOME`. Installs preflight every destination before writing: collisions
and locally edited owned files cause an error. Uninstall deletes only unchanged
owned files, preserves edited files and unrelated agents/skills, and removes
only empty directories. Neither command uses `rsync --delete` on shared agent
directories. Do not delete the ownership manifest before uninstalling.

## Invocation and configuration

```text
/review-all
/review-all --staged
/review-all vs main --paths src,tests --exclude src/generated
/review-all PR #123
/review-all init
```

Canonical target resolution, scope prompts, exclusions, all ten axes,
conditional activation, per-agent ordering, canonical chunk membership,
deduplication, severity/confidence rules and interactive follow-ups still apply.
Target-revision source snapshots keep staged and historical reviews distinct
from a dirty working tree. PR review requires `gh` and local Git objects for the
PR's base/head; missing objects are an explicit preparation error, not a review
of unrelated local code.

Use **`.copilot/review-all.json`**, never the Claude configuration:

```json
{
  "models": {
    "01-standards": "gpt-6-luna",
    "03-dry-smells": "gpt-6-luna",
    "coordinator": "gpt-6.1-sol",
    "judge": "claude-opus-5.5",
    "deep-dive": "gpt-6-astra"
  },
  "verifierModel": "claude-opus-5.5",
  "reasoningEfforts": {
    "01-standards": "medium",
    "03-dry-smells": "medium"
  },
  "verifierVotes": 1,
  "maxConcurrentAgents": 2,
  "chunkMaxFiles": 40,
  "chunkMaxBytes": 200000,
  "skipAgents": [],
  "gateSeverityFloor": "critical"
}
```

Review-affecting canonical keys retain their meanings (see
`skills/review-all/references/config-keys.md`), with these adapter differences:

| Setting | Copilot behavior |
|---|---|
| State/history/checkpoints | `.copilot/review-all/`; never imports Claude state |
| Reports | `.copilot/reports/` by default |
| `models` | Explicit Copilot model ID per canonical axis or `coordinator`, `verifier`, `judge`, `deep-dive` role |
| `verifierModel` | Explicit verifier model ID; takes precedence over `models.verifier` |
| `reasoningEffort` | Optional shared override for all roles, including roles with a different packaged default |
| `reasoningEfforts` | Map of role IDs to efforts; overrides the shared setting for those roles |
| `maxConcurrentAgents` | Positive integer, default 1; enforced across running ledger jobs, also bounded by known host capacity |
| Gate verdict | Authoritative verdict is per-run `runs/RUN_ID/gate-verdict.json`; legacy singleton `gateVerdictFile` is not a CI oracle |
| Cache/checkpoint reuse | Cross-run reuse disabled; current-run completion receipts remain persistent |
| `extraAgents` | Unknown/uninstalled custom axes fail explicitly rather than silently losing coverage |

`copilot/models.json` is the single source of packaged defaults:

| Stage / role | Model ID | Effort |
|---|---|---|
| Coordinator, reporting, ordinary follow-ups, fixes/test generation | `gpt-6.1-sol` | `high` |
| 01 standards, 03 DRY/smells | `gpt-6-luna` | `medium` |
| 02 bugs/security, 04 consistency/history, 05 simplification, 06 security deep dive, 07 performance, 08 test quality, 09 API contract, 10 a11y/i18n | `gpt-6.1-sol` | `high` |
| Verifier and all verifier votes | `claude-opus-5.5` | `high` |
| Independent eval rubric judge | `claude-opus-5.5` | `high` |
| Explicit difficult-finding investigation (`deep-dive`) | `gpt-6-astra` | `high` |

Discovery, filtering, hashing, check execution, deduplication and gate calculation
remain deterministic code. Astra is an explicit follow-up, not automatic escalation
of the 06 security reviewer. `verifierVotes` remains 1 by default.

Effort precedence is: `reasoningEfforts[role]`, then explicit `reasoningEffort`,
then the packaged role default, then the packaged shared default (`high`). Thus
an existing `reasoningEffort: high` configuration still raises Luna to high;
omit it to use the mixed preset. Explicit model overrides retain their precedence.

Generated profiles declare the resolved `model`, `reasoningEffort`, and
`modelPolicy: required`. Assignment packages bind model/effort to the input hash;
native task dispatches pass both `model` and `reasoning_effort` explicitly.
For manual installations, reinstall profiles with the same configuration after
changing model overrides. Marketplace profiles have fixed packaged defaults;
switch to a manual installation for customized profiles.
An unavailable model/effort must cause an explicit substitution and rebuilt
package (or a headless error), never an unrecorded fallback. Do not use an Auto
parent session for this fixed-model workflow. Record runtime-exposed effective
identity when available; model self-identification is not provenance.

The headless wrapper uses `models.coordinator` and the coordinator's resolved
effort unless `--model` / `--reasoning-effort` override them. An interactive
parent keeps its own settings; start it with Sol/high for the recommended preset.

The eval runner's `--model` / `--reasoning-effort` affect only the coordinator.
`--judge-model` / `--judge-reasoning-effort` independently override the judge.
`REVIEW_ALL_COPILOT_CONFIG_JSON` supplies fixture/profile settings and the default
coordinator/judge settings; CLI flags win only for their named role. Each case
run records requested launch settings in `model-settings.json` alongside logs.
The integration runner has separate `--reviewer-model`,
`--reviewer-reasoning-effort`, `--verifier-model`, and
`--verifier-reasoning-effort` options; its original model/effort flags now affect
only the parent. It defaults to the mixed Sol/Luna/Opus configuration.

This preset is a candidate for evaluation, not a measured quality improvement.
Compare missed defects, false positives, completion failures, latency and credits
against all-Sol/high. For that baseline, set all ten axis IDs plus `coordinator`
and `deep-dive` in `models` to `gpt-6.1-sol`, set `verifierModel` to the
same ID, set `reasoningEffort: high`, and remove `reasoningEfforts` overrides.
Leave `models.judge` unchanged and pass `--judge-model claude-opus-5.5` and
`--judge-reasoning-effort high` to both eval runs so the rubric judge stays fixed
across experimental arms when comparing review models.

Model IDs and dispatch controls are documented in the
[Copilot CLI reference](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference).
Access remains subject to plan and organization policy. The existing CLI 1.0.95
smoke evidence below predates this preset and does not prove these models or
required-model dispatch on that version; validate on the installed CLI.

Profile defaults can also be generated with a chosen configuration:

```text
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<repo>" install --harness copilot   --project /path/to/repo --config /path/to/models.json
```

## Explicit context and instruction boundaries

The coordinator records conversational requirements in a file, resolves the
target and scope, discovers the project, and authorizes checks. The preparation
helper collects filesystem/process evidence; it cannot infer what the user
previously said. See the [request contract](../skills/review-all/copilot/request-contract.md)
for all target kinds, a five-commit range example, a minimal profile, instruction
settings confirmation, and explicit cheaper-axis configuration. The
[staged request example](../skills/review-all/copilot/request-example.json) is a
complete starting point. Finish config before preparing; adding a previously
absent `.copilot/review-all.json` also invalidates a run.

Preview cost before checks or dispatch with `copilot-package plan --repo REPO
--request REQUEST` through the installed tool's uv CLI. It reports reviewer
assignment counts by axis, approximate input tokens, verifier dispatch bounds
and skipped axes. Include this in large-range prompts. The estimate is a
planning heuristic, not a dollar quote; output/reasoning tokens, checks, retries
and verification add variable cost. For ad-hoc inspection use `<tool>/.venv/bin/python`
(Windows: `<tool>/.venv/Scripts/python.exe`), not a system Python 3.9.

Every package has a schema version, unique run ID, exact filtered diff hash,
target revisions, source hashes, selection decisions and chunk assignments,
instruction sources/scopes, resource hashes, configuration, requirements,
check commands/exit codes/output/times/cwd, and available runtime identity.
Artifacts live under ignored `.copilot/review-all/`. The helper places a `*`
ignore file there; do not force-add or upload those potentially sensitive files.

Each dispatch includes essential task instructions and an exact assignment
path. Every mandatory input is file-backed in numbered parts of at most 12KB.
Large files and long lines are split without dropping bytes. File chunk
membership is computed in canonical Git order before persona-specific ordering.
Assignment JSON is pretty-printed; use small `view_range` windows through EOF
if a full `view` truncates. Keep default chunk limits for display truncation:
smaller chunks repeat common context and source, increasing dispatch/token cost.
Chunk IDs are global canonical indices. An axis with only `chunk-0003` has no
matching files in chunks 1 and 2; it has not lost coverage.
Providing every part does not mean it fits every model context: lower chunk
limits or report partial if a child cannot read everything. Never silently cut
the review scope. Check output is preserved, not reduced to a claimed PASS.

All twelve profiles declare `include-custom-instructions: true`. The adapter
uses native loading plus explicit scoped handoff for:

- Root/nested AGENTS.md, CLAUDE.md, `.claude/CLAUDE.md`, GEMINI.md and
  `.github/copilot-instructions.md`.
- `.github/instructions/**/*.instructions.md` and user modular instructions
  with `applyTo`, preserving directory scope and case-sensitive matching.
- User `copilot-instructions.md`, relative bounded imports in supported
  formats, and explicitly enabled parent-only instruction sources.

The helper supports documented `*`, `**`, `?`, brace alternatives, and
comma-separated glob patterns in a scalar `applyTo` string. Unsupported glob or
frontmatter forms are reported as unavailable context, not widened to all
files. Relative imports remain within repository/custom-directory boundaries.
REVIEW.md remains verbatim, with no import expansion, and retains its review
focus/severity precedence but cannot override proof or completion requirements.

`copilot instruction list --json` is useful inventory, **not authoritative
live-session enablement**. In particular, its plugin listing uses global
settings and cannot prove every managed/repository/session setting. The
coordinator must confirm the live policy and record disabled source paths.
`--no-custom-instructions` disables explicit instruction transfer too, including
REVIEW.md. Disabled files are not silently reintroduced as requirements.
Unknown settings and unreadable enabled files mean incomplete context.

## Dispatch and result transport

Start immediately before dispatch, one assignment or one ready wave at a time.
`copilot-results start RUN --assignments ID1,ID2` performs one freshness check,
reserves the wave atomically, and emits one dispatch object per line. It refuses
waves exceeding `maxConcurrentAgents` including already-running jobs. Configure
known host capacity before prepare; there is no portable capacity probe, and an
observed eight-agent session is not a general default.

Plugin starts must add `--agent-namespace review-all`, so the returned name is
dispatchable, e.g. `review-all:review-all-standards` or
`review-all:review-all-verifier`. Manual installations omit this option. Use the
returned name and settings verbatim.

Reviewers return the [compact result contract](../skills/review-all/copilot/result-contract.md):
a per-attempt dispatch token, status, explicit `allMandatoryRead` attestation,
missing inputs, effective model, source files reread, and findings. The helper
fills deterministic run/chunk/path metadata and stores the full validated result.
Legacy full envelopes remain supported. The assignment itself must be read;
its path is optional in legacy `readInputs`, while every `mandatory` component
path is required. Neither form proves a model honestly read or applied the inputs.

Use `copilot-results accept RUN --assignment ID --stdin` when the host can pipe
an existing JSON output stream; otherwise use create/edit to save the compact
object and `--result FILE`. Do not embed arbitrary agent JSON in shell code.
Read-only agents still have no write tools, and the coordinator need not re-emit
the mandatory path list or read back expanded result artifacts.

On a stopped/stale run, cancel live agents in the host, then use
`copilot-results abandon RUN --reason "config changed"`. It closes unfinished
jobs without checking freshness, preserves completed evidence and forbids
restarting/finalizing that run. Finish configuration and prepare a new run.

Parent-only instruction directories do not automatically follow child
dispatches. The coordinator records relevant, enabled sources as `parentSources`
with exact source, boundary and applicable file set. Additional directories
from `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` must be explicitly accounted for.
Nested instructions are never promoted to repository-wide rules.

## Permissions, checks and verification

Reviewers and verifier have only native **read/search** tools. The history
reviewer additionally has **execute** for read-only Git history. Tool aliases do
not restrict shell arguments: apply host permission rules for Git log/show/blame
if you need enforcement, rather than treating the profile's prose as a sandbox.
No profile receives edit or child-delegation tools by default. The coordinator
runs approved checks; checks execute repository code and require trust.

A complete empty findings array is accepted. Missing inputs/chunks, malformed
results, dispatch errors, timeout and missing verifier judgments are not
interpreted as "no findings." The coordinator gets exactly one retry per
assignment, with the validation error; persistent failure produces partial
coverage. Hash checks reject changed inputs before results are accepted.

Verifier batches contain deduplicated candidates, corroboration metadata,
relevant original source snapshots/diffs, scoped instructions, user requirements
and check evidence. They contain neither exploratory reasoning nor other
verifiers' judgments. Every vote uses a fresh context, rereads source, and must
return one judgment per candidate. Source quotations are checked against the
snapshot. Canonical thresholds remain >=75 keep, 50-74 appendix, <50 drop;
unobserved runtime/data/rendering claims remain unverified and cannot block.

The coordinator still maintains human lifecycle decisions and history in the
Copilot namespace. **Cross-run model-result reuse is deliberately disabled**:
the helper cannot independently prove effective model/provider identity for
every child in an arbitrary interactive runtime. That includes prior machine
rejections, which must not silently suppress fresh candidates. Source, diff,
scope, instructions, requirements, checks, personas, rules, config and harness
identity are fingerprinted, but a fingerprint alone does not establish model
identity. Current-run completion is persisted; cancelled running assignments
must be marked failed and retried, not treated as completed.

## Difficult-finding follow-up

After the user selects a difficult finding for investigation, resolve its settings:

```text
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<repo>" copilot-package model-settings --role deep-dive
```

Dispatch `review-all-deep-dive` through the native task tool with the returned
model and `reasoning_effort`, plus the finding, target/scope, applicable scoped
instructions, requirements, shared rules, source snapshots/diff and check evidence.
It returns read-only root-cause analysis and fix options. Its output never updates
a verdict directly; any changed findings/fixes require a fresh review package.
Ordinary explanations and test suggestions stay with the Sol coordinator.

## Reliable headless gates

Install the skill first. Then:

```text
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<repo>" copilot-gate --allow-tools --model gpt-6.1-sol --reasoning-effort high --severity important -- --staged
```

For project installation, TOOL is `/path/to/repo/.github/.review-all-tool`.
For user installation, TOOL is `~/.copilot/.review-all-tool` (expand the home
directory to an absolute path).
`--allow-tools` explicitly authorizes the coordinator's tools/checks on trusted
code; it does not grant all filesystem paths or all URLs. Review your host's
permission policy. Pass `--no-custom-instructions` to disable native and
explicit instruction loading together.

The wrapper starts a fresh Copilot run, requires a verdict with its unique run
ID and exact requested target, validates timestamp/package/source identity,
and recomputes the verdict from validated reviewer/verifier receipts. It
rejects missing, stale, mismatched and manually fabricated verdicts.

| Exit | Meaning |
|---|---|
| 0 | Complete passing review |
| 1 | Blocking verified finding, or partial coverage |
| 2 | Malformed/missing/stale verdict, changed inputs, unsupported CLI, or execution error |

This preserves `gate-verdict.py`'s contract without assuming a tool command's
exit status becomes Copilot's process exit status. Headless mode has no scope
menus, post-review menus, fixes or publication. If scope is not explicitly
narrowed, the coordinator reviews the complete resolved target.

## Tests and compatibility evidence

```text
just test

# Real task-tool child dispatches, not merely copilot --agent:
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<repo>" integration    --out /absolute/path/to/evidence

# Existing synthetic cases + independent Copilot rubric grader:
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<repo>" run-evals-copilot 01    --model gpt-6.1-sol --reasoning-effort high --out /absolute/path/to/eval-evidence
```

Both model-backed runners are opt-in and retain evidence. Set
`REVIEW_ALL_COPILOT_INTEGRATION=1` or `REVIEW_ALL_COPILOT_EVALS=1` respectively
in the host environment before invoking them. The integration
fixture depends on root and path-specific instructions, a requirement provided
only in the parent prompt, a real failed check, verifier source rereads, and
missing-context rejection. It inspects actual task-tool calls. The eval runner
keeps the existing fixture/rubric/RESULT/SCORE interfaces and preserves the Claude
runner. Claude-specific cache/state-reuse fixtures are explicitly ungraded
errors, not silently passed. Full model-backed evals are not part of CI.

The live context-handoff integration test **passed on 2026-10-11** with CLI
**1.0.95**, model **gpt-5.4-mini**, and runtime-reported provider **copilot**.
It recorded four actual task-tool dispatches (reviewer, verifier plus its
schema/read-coverage retry, and missing-context probe). All six fixture
assertions passed. Earlier failed attempts exposed a too-strict read-receipt
check and ambiguous reviewer/verifier output examples; the adapter now accepts
additional native reads and supplies role-specific result contracts.

This verifies the context handoff, not a full ten-axis quality benchmark or
an end-to-end live headless gate. The full model-backed rubric eval suite was
not run. That recorded run used the previous model defaults; it does not verify
the mixed Sol/Luna/Opus preset or the Astra follow-up. Cross-run result reuse remains disabled.

Minimum CLI version inspected and exercised: **1.0.95**, macOS, Python 3.9.6,
Bash 5.3.20. `include-custom-instructions` was introduced in **1.0.86**;
older versions are rejected, and 1.0.86-1.0.94 are not claimed as tested.
Live-run results are recorded in each runner's `result.json`/transcript; a
successful deterministic suite alone is not evidence of native integration.

Sources checked during implementation:

- [Copilot CLI changelog, 1.0.86](https://github.com/github/copilot-cli/blob/main/changelog.md)
- [Custom-agent configuration and tool aliases](https://docs.github.com/en/copilot/reference/custom-agents-configuration)
- [CLI instruction locations, imports and scope](https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions)
- [Instruction inventory caveats](https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference#using-copilot-instruction)

## Remaining limits

Context packages increase disk/token cost; required read receipts cannot prove
that a model applied every instruction or honestly inspected every byte.
Native instruction discovery and explicit transfer have different boundaries.
Model availability and concurrency vary by runtime; queued work is not lost
coverage. Default concurrency is one when capacity is unknown. Checks execute
the live checkout; for historical/staged targets whose live tree differs, the
helper records SKIP with a provenance-mismatch reason rather than executing
checks that cannot prove the target revision. Historical supporting context must be supplied
from the target revision rather than assumed from the live tree.

Runtime observations, CodeGraph/MCP, browser checks and scheduling depend on
available host tools. The default profiles do not grant arbitrary MCP tools;
unavailable structural tooling falls back to native search. No model-backed
run establishes universal instruction compliance or correctness, and the
integration smoke test is not a full ten-axis quality benchmark.
