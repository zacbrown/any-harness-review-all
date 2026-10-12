# Examples & Common Issues

## Examples

User says: "review my changes" (nothing staged or committed ahead)
→ Empty-argument path: review the current branch vs its merge-base with the default branch, or the last commit if on the default branch with no changes.

User says: "/review-all PR #42 --paths apps/web,libs/shared"
→ Resolve via `gh pr diff 42`, then apply the `--paths` include filter to restrict the diff to those two prefixes before running phases.

User says: "review-all init"
→ Load `references/init-wizard.md` and run the config wizard instead of a review; exit after writing `.claude/review-all.json`.

User says: "pre-commit check on my staged files"
→ Run with `--staged` — review only staged changes through the deterministic gates and parallel heuristic agents, then present the fix-scope menu.

## Common Issues

- **Incomplete installation / wrong Python environment** → run `just setup` at TOOL, not the reviewed repository. Use the locked/no-sync uv prefix in `references/cli.md`; never synchronize the target's project.
- **Windows `.cmd`/`.bat` argument rejected** → quotes/newlines cannot be passed safely through that launcher; select its native executable/JS entry point instead. Do not translate arbitrary Bash programs into PowerShell.
- **Windows uninstall reports a running environment** → invoke the installed `cli.py uninstall` using system Python outside that `.venv` (see `references/cli.md`). No source checkout is needed.
- **Checkpoint invalidated after changing OS/Python** → intentional: execution-environment identity is part of the checkpoint key; do not reuse potentially different check evidence.

- **`git` missing in Phase 0.0 discovery** → `discover` exits non-zero; abort with explicit error — nothing in the skill works without git (it is the only hard requirement).
- **`PR #N` target requested but `gh` is unavailable** → reject that argument with clear message; the GitHub PR resolution path needs the `gh` CLI.
- **Agent or verifier never returns** → the Phase 2.75 completion gate re-spawns it once; if still fails, surface it under the `⚠️ PARTIAL REVIEW` banner — never drop it silently.
- **Report printed, turn ended, no menu** → premature-completion stop (the #1 Phase 4 failure mode). The mandatory menu gate requires the Phase 4 menu in the SAME turn as the report unless every section is "None found." with no appendix — re-present it.
- **Stale rules after a branch switch** → the cache key (computed by `discover`) hashes CLAUDE.md file contents, not mtimes (`git checkout` does not bump mtimes), so a branch switch changes the key → MISS → fresh extraction. Toolchain commands and tool availability are never cached at all — re-probed every run. Legacy `claudeMdHash`-era cache files auto-MISS on the schema check.
- **Resolved range is huge** (≥20 commits or ≥200 files on the empty-args default) → the large-range scope prompt offers narrower options; skip it only when an explicit argument already declared intent.
- **A re-run re-spawns every axis instead of resuming** → the checkpoint key changed. It covers HEAD, the exact diff bytes, the personas + `SKILL.md`, and `REVIEW.md`/`.claude/review-all.json`, so any edit to the working tree or to the skill invalidates all axes by design. `load`'s `ignored` array names the reason per axis (`key-mismatch`, `schema`, `unreadable`, `axis-mismatch`).
