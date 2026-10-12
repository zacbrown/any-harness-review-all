# Phase 1.5 — Runtime Probe (optional)

Loaded by `/review-all` Phase 1.5. Adds runtime/visual verification on top of static analysis gates. Optional and self-skipping — never blocks the review.

## Why this exists

Static review cannot catch a route that 500s, a template rendering blank, or a layout breaking visually. Anthropic's long-running-harness research identifies browser/visual verification as the highest-leverage way to close that gap.

## When to run

Run Phase 1.5 only if **all** of:
1. Phase 0 detected an open dev-server port matching the framework's typical port.
2. Diff contains UI-relevant files: `.html`, `.tsx`, `.jsx`, `.vue`, `.svelte`, `.css`, `.scss`, `.tmpl`, framework template extensions.
3. `toolchain.available.capabilities.http` is true (from Phase 0.0).

If `runtimeProbe == "force"`, condition (2) is bypassed — Step 1 runs whenever conditions (1) and (3) hold. HTTP support is built into the Python CLI; no curl or GNU timeout is needed.

If any required condition fails → skip Phase 1.5 entirely. Do NOT prompt the user.

## Step 1 — Health probe (always, when running)

For each detected dev-server port, invoke the shared HTTP probe. It retries up to 3 times with 2s backoff to absorb warm-up (e.g. `ng serve` binds the port before its HTTP stack answers):

```text
uv run --project "<tool>" --locked --no-sync --no-python-downloads review-all http-probe "http://localhost:<port>/" --timeout 5
```

Use the final JSON `status`, `health`, `url` and timing for recording.

Record `runtime.health[<port>]` in the Project Profile:
- `2xx`/`3xx` → `healthy`
- `401`/`403` → `auth-gated` — do NOT emit a finding (auth-protected dev routes are normal; treat as healthy for probe purposes)
- other `4xx`/`5xx` → `unhealthy(<code>)` — emit one 🟠 IMPORTANT finding (`confidence: VERIFIED`) naming port and code
- timeout / connection refused → `unreachable` — do NOT emit a finding (port-open with no HTTP responder is common for non-HTTP dev servers)
- on `400` to local `http://` → the helper retries once against `https://localhost:<port>/` accepting self-signed certificates for loopback only; record successful scheme

If `runtimeRoutes` is set in config, probe each listed route instead of `/`. Each route is requested as `http://localhost:<port><route>`. Lets users override unauthenticated-`/` assumptions for apps mounted at non-root paths.

`unhealthy` does not block subsequent steps; reported alongside other gate results.

## Step 2 — Visual diff (only if Playwright/Puppeteer available)

Detection: read `package.json` `devDependencies`/`dependencies` for `playwright`, `@playwright/test`, `puppeteer`, or `puppeteer-core`. If none → skip Step 2.

If available:

1. Determine routes to probe. Start with `/`. If the diff touches files under a discoverable routes directory (`src/routes`, `src/pages`, `src/app/**/page.{tsx,jsx}`, Angular route configs), map changed files to URL paths and include them.
2. Use CLI `mkdir` to create `.claude/review-all/shots/<platform>/<HEAD-sha>` (and `pending-baseline/<platform>` if step 4 will fire). Keep platform-specific baselines: operating systems may render fonts differently. Phase 0.9 only creates the top-level directory.
3. For each route, headless-screenshot to `.claude/review-all/shots/<platform>/<HEAD-sha>/<route-slug>.png` via a one-shot Node script the orchestrator writes to a temp file. Execute it through CLI `execute`, using the repository cwd and installed Playwright/Puppeteer, without installing or downloading browser dependencies.
4. Detect a pixel-diff lib in `package.json` (`pixelmatch`, `odiff-bin`, `looks-same`, `resemblejs`, or Playwright's built-in `toHaveScreenshot`). If none present → skip the diff (still write the new shot); log `Runtime probe: visual diff SKIPPED (no pixel-diff lib)`. Do NOT byte-compare PNGs — headless renders differ byte-wise across runs (metadata, compression) even when pixels match, producing constant false positives.
5. If a pixel-diff lib is present AND `.claude/review-all/shots/baseline/<platform>/<route-slug>.png` exists → run the diff. Differences > `visualDiffThresholdPct`% of pixels (default `1.0`) → emit one 🔵 SUGGESTED finding (`confidence: VERIFIED`) per route, attaching both image paths.
6. If no baseline → write new shots as `pending-baseline/` so the user can promote them in Phase 4. Do NOT emit a finding.

## Failure modes

Phase 1.5 records its own errors. Any failure (HTTP execution failure, dev-server died mid-run, Playwright crash, write-permission denied) → log one line in the report's gate section: `Runtime probe: SKIPPED (<reason>)`. Never conceal the error or turn it into a PASS; continue Phase 2.

## Configuration

Configurable via `.claude/review-all.json` keys `runtimeProbe`, `runtimeRoutes`, `visualDiffThresholdPct` — see `references/config-keys.md` for defaults and semantics.

## Output integration

- Step 1 findings: standard Phase 1 gate row labelled `Runtime`.
- Step 2 findings: enter the normal finding pipeline at Phase 2.5 with `confidence: VERIFIED` (skip verification).
