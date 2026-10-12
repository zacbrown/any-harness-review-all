#!/bin/sh
# Temporary compatibility shim; supported entry point: uv / review-all.
TOOL="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec uv run --project "$TOOL" --locked --no-sync --no-python-downloads review-all test --pattern test_discovery.py
