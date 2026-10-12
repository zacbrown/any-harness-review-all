## Installation layout

This entry point is generated for the Copilot plugin. Resolve this SKILL.md's
absolute directory as `ENTRY_ROOT`. Resolve `TOOL` as `ENTRY_ROOT/../../../..`
(the plugin root containing `pyproject.toml`, `uv.lock`, and `justfile`), and
`SKILL_ROOT` as `TOOL/skills/review-all`.

All relative resource links in this entry point and the canonical references
resolve from `SKILL_ROOT`, not `ENTRY_ROOT`, with one exception:
`references/workflow.md` means `TOOL/.github/plugin/workflow.md`. Read that
generated workflow, `SKILL_ROOT/agents/_shared.md`, and
`SKILL_ROOT/references/cli.md` before starting. This layout overrides the
custom installer's `.review-all-tool` paths in the canonical workflow.
Never execute the Claude coordinator at `SKILL_ROOT/SKILL.md`.

Marketplace installation supplies instructions and source, not a Python
environment. Before the first review, and after every plugin update, require
explicit setup of the installed plugin's tool environment:

```text
uv run --no-project --no-python-downloads python "<tool>/skills/review-all/cli.py" setup
```

Replace `<tool>` with the absolute TOOL path above. If setup is missing or
stale, stop and tell the user to run it; do not synchronize dependencies as
part of a review. Setup uses an already installed Python >=3.11 and uv.
Do not run `install` or `install-copilot` inside the plugin: Copilot already
loads its skill and twelve profiles, and manual copies would shadow updates.

Invoke normal helpers through TOOL's locked/no-sync uv CLI with the reviewed
repository passed explicitly. Never use the reviewed project's environment.
The plugin ships fixed profile defaults; for custom model profiles use the
documented ownership-aware installer instead of editing the plugin cache.
