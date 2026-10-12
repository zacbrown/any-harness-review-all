# Shared Python CLI contract

Requires Python >=3.11, uv, Git, and the selected harness. Just >=1.33 is only
needed for named tasks. No Bash, WSL, Unix utilities or PowerShell launcher is
required by the tool. A project's own scripts or its harness may require a shell.

## Locate the tool, not the reviewed project

In a source checkout, TOOL is the checkout containing `pyproject.toml`,
`uv.lock`, and `justfile`. In an installed skill, TOOL is
`SKILL_ROOT/../../.review-all-tool` (resolve it to an absolute path). The
ownership-aware installer ships the complete package, resources, tasks and
deterministic tests there; it never depends on the source checkout afterward.

Provision once with `just setup` from TOOL, or without just:

```text
uv run --no-project --no-python-downloads python "<tool>/skills/review-all/cli.py" setup
```

Setup is the only operation that synchronizes dependencies. It uses the pinned
build backend and lockfile and requires an already installed Python. Bootstrap
installation of Python/uv is outside this package's scope.

Every normal invocation uses this prefix (examples below abbreviate it as
`review-all`; replace placeholders with absolute paths, not shell variables):

```text
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all --repo "<reviewed-repository>"
```

Use the tool's project and environment even if the reviewed repository contains
its own `pyproject.toml`, `.python-version`, `.venv`, or conflicting dependencies.
An incomplete installation is an error, not permission to download a runtime or
silently omit selection, checks, checkpointing or verdict validation. Reviews
never synchronize dependencies. After an upgrade, rerun setup.

`just --justfile "<tool>/justfile" discover "<reviewed-repository>"` is equivalent.
Just runs recipes in the justfile directory, so review tasks require a repository
argument. Pass absolute repository paths when invoking from elsewhere. The
historical `install-copilot-project` / `uninstall-copilot-project` default remains
the package checkout, not the caller's directory. Arguments travel through
`sys.argv`, never interpolation into Python source.

## Operations

Machine results go to stdout; diagnostics go to stderr. Operations preserve the
existing helper contracts and algorithms; a legacy `NAME.py` is invoked as the
`NAME` subcommand, not as a separate Python installation.

| Operation | Input | Output / side effects | Exit |
|---|---|---|---|
| `preflight` | none | JSON Git/gh availability and native timeout/TCP/HTTP/hash capabilities | 0; 2 without Git |
| `discover [--harness claude\|copilot]` | explicit `--repo` | fresh JSON toolchain, test pattern, root; Claude v2 rules cache verdict only for Claude | 0; 2 error |
| `detect-toolchain`, `test-pattern-probe` | explicit `--repo` | legacy fields plus executable arrays in `toolchain.commands` | 0; 2 error |
| `dev-server-probe [ports]` | comma-separated ports | `{"open":[],"closed":[]}` | 0; 2 invalid input |
| `http-probe URL [--timeout 5]` | HTTP(S) URL | status, healthy/auth-gated/unhealthy/unreachable, timing; bounded retries | 0 observation; 2 input/execution error |
| `execute SPEC.json` | explicit `--repo`; command spec below | stdout, stderr, exitCode, elapsedSeconds, timedOut, cwd, requiredShell | 0 success; 1 command failure; 124 timeout; 2 launch/input error |
| `diff --output FILE -- GIT_ARGS...` | explicit `--repo`; resolved Git arguments | writes exact binary diff bytes; JSON path/hash/length | 0; 2 error |
| `mkdir PATH...` | explicit `--repo`; relative paths | creates directories, rejects escapes | 0; 2 error |
| `select-files`, `select-agents`, `agent-order`, `dedupe` | existing JSON stdin contracts | filtered/ordered/grouped JSON; unchanged thresholds and reasons | existing helper exit codes |
| `checkpoint load\|save` | existing arguments and exact diff / findings stdin | environment-bound checkpoint load/pruning or atomic save | 0; 2 malformed |
| `code-hash`, `state-sweep` | existing positional arguments | hashes / lifecycle sweep; writes remain harness-specific | existing helper exit codes |
| `export-findings`, `gate-verdict`, `severity-tally`, `eval-scorecard` | existing input formats and flags | existing JSON/SARIF/verdict/SCORE formats | existing helper exit codes |
| `copilot-package prepare\|fresh` | request / run directory | immutable run package, scoped context and real check receipts | 0; 2 error |
| `copilot-package model-settings --role ROLE` | explicit `--repo`; role name | resolved per-stage model and reasoning settings | 0; 2 error |
| `copilot-results` | start/accept/fail/verifiers/votes/finalize/persist arguments | locked completion ledger, independent verifier inputs, reports/verdict/state | 0; finalize 1 blocked; 2 invalid |
| `copilot-gate` | explicit repository, target, permissions and model flags | fresh receipt-recomputed verdict; lifecycle persisted only after validation | 0 pass; 1 blocked/partial; 2 missing/stale/malformed/mismatched/execution error |
| `install`, `uninstall` | `--harness claude\|copilot`, `--user` or `--project PATH` | ownership manifest, resources/profiles/environment; preserve edited/unrelated files | 0; 2 collision/error |
| `test [--pattern test_*.py]` | none | all deterministic tests including documentation/eval schema checks | 0 pass; 1 failed; 2 no tests |
| `eval PREFIX --harness claude\|copilot --out DIR` | fixture prefix (`all` explicit) | opt-in model-backed RESULT/SCORE evidence | 0 pass/explicit skip; 1 failed; 3 no cases |
| `integration --out DIR` | opt-in Copilot environment variable | actual child-dispatch/context evidence; not a full gate benchmark | 0 pass/explicit skip; 1 failed |

Use global `--input FILE` **before** the operation to pass existing stdin data
without shell pipelines. Resolve HEAD with Git, capture the resolved filtered
diff with `diff`, then pass that file to `checkpoint load`; never decode and
re-encode a diff. Hashes always cover actual bytes, including LF/CRLF differences.
No Git line-ending setting is changed.

```json
{"argv": ["npm", "run", "test"], "timeoutSeconds": 180}
```

Run package scripts through the package manager; never run the `scripts.test`
text as an executable. A deliberately configured shell program instead uses
`{"shell":"bash","program":"...","timeoutSeconds":180}`. Missing required shells
are errors. Arbitrary Bash programs are never translated into PowerShell.
Windows native `.exe`, `.cmd`/`.bat`, `.ps1`, and explicit Python script paths
are handled centrally. Batch arguments with quotes/newlines are rejected with
an explicit error: use a native executable for those values. PowerShell scripts
require installed PowerShell and remain subject to its execution policy.

Process execution drains large output to temporary files and kills the process
group on Unix or the preassigned Job Object on Windows, including descendants.
This also cleans up background descendants on normal completion; a check is not
a supported way to start a persistent dev server. On Unix, deliberately escaping
the process group is not supported. Failed/timed-out commands
cannot become PASS. TCP liveness alone is never build evidence: retain the
fresh-log/source-mtime/success-marker proof from Phase 1.

Model evals require `REVIEW_ALL_CLAUDE_EVALS=1` or `REVIEW_ALL_COPILOT_EVALS=1`.
Integration requires `REVIEW_ALL_COPILOT_INTEGRATION=1`. Set environment values
through the host/CI environment, not a shell-specific command prefix. Claude
evals require the skill installed for that harness. Copilot cache/state-reuse
fixtures remain explicit unsupported errors, not model failures or green skips.

## Validation scope

Locally exercised: macOS arm64, Python 3.13.2, uv 0.6.9, just 1.58.0.
The CI matrix runs the same suite and direct uv / just entry points on Ubuntu,
macOS and Windows Server, including a controlled Windows PATH without Git Bash.
The Windows driver accepts Git initially found in `Git/bin` or `Git/cmd` and
uses the verified `Git/cmd/git.exe` directory in its restricted PATH.
CI configuration alone is not evidence of passing Windows execution.

**Windows 11 x64 smoke evidence is pending.** On an actual Windows 11 machine,
record OS/tool versions, setup/install/discovery/check/gate results, special-path
and descendant-cleanup results, and uninstall preservation. Record live Copilot
subagent dispatch and a complete gate separately with the configured per-stage
defaults in `copilot/models.json` (see `docs/COPILOT.md`). Do not treat deterministic stubs, hosted Windows
Server, or the older macOS context-handoff smoke as this evidence.
ARM64 Windows and network-share repositories remain unclaimed.
Windows will not delete the Python executable running the uninstaller. To remove
an installation without retaining a development checkout, launch its same CLI
with an already installed system Python outside the environment being removed:

```text
uv run --no-project --no-python-downloads --python "<system-python>" python "<tool>/skills/review-all/cli.py" uninstall --harness copilot --user
```

Substitute `--harness claude` or `--project "<repo>"` as appropriate. Uninstall
alone permits system Python so it can also recover an incomplete installation;
normal reviews still require the locked, provisioned tool environment.
