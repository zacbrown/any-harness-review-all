set script-interpreter := ["uv", "run", "--project", ".", "--locked", "--no-sync", "--no-python-downloads", "python"]
set positional-arguments
export REVIEW_ALL_TOOL := justfile_directory()
export UV_PROJECT_ENVIRONMENT := justfile_directory() / ".venv"

[script]
help:
    from review_all.cli import main
    raise SystemExit(main(["help"]))

# The only task allowed to synchronize dependencies. Requires an installed Python.
[script("uv", "run", "--no-project", "--no-python-downloads", "python")]
setup:
    import os, runpy
    from pathlib import Path
    module = runpy.run_path(str(Path(os.environ["REVIEW_ALL_TOOL"]) / "skills/review-all/cli.py"))
    raise SystemExit(module["setup"]())

[script]
install:
    from review_all.cli import main
    raise SystemExit(main(["install", "--user"]))

[script]
uninstall:
    from review_all.cli import main
    raise SystemExit(main(["uninstall", "--user"]))

[script]
install-copilot:
    from review_all.cli import main
    raise SystemExit(main(["install", "--harness", "copilot", "--user"]))

[script]
uninstall-copilot:
    from review_all.cli import main
    raise SystemExit(main(["uninstall", "--harness", "copilot", "--user"]))

# Project recipes retain the historical default: the package checkout.
[script]
install-copilot-project project=".":
    import sys
    from review_all.cli import main
    raise SystemExit(main(["install", "--harness", "copilot", "--project", *sys.argv[1:]]))

[script]
uninstall-copilot-project project=".":
    import sys
    from review_all.cli import main
    raise SystemExit(main(["uninstall", "--harness", "copilot", "--project", *sys.argv[1:]]))

[script]
review-self: install
    from review_all.cli import main
    raise SystemExit(main(["review-self"]))

# Unlike installation, review targets are always explicit.
[script]
discover repo *args:
    import sys
    from review_all.cli import main
    raise SystemExit(main(["--repo", sys.argv[1], "discover", *sys.argv[2:]]))

[script]
test *args:
    import sys
    from review_all.cli import main
    raise SystemExit(main(["test", *sys.argv[1:]]))

[script]
eval *args:
    import sys
    from review_all.cli import main
    raise SystemExit(main(["eval", *sys.argv[1:]]))

[script]
gate repo *args:
    import sys
    from review_all.cli import main
    raise SystemExit(main(["--repo", sys.argv[1], "copilot-gate", *sys.argv[2:]]))

[script]
cli +args:
    import sys
    from review_all.cli import main
    raise SystemExit(main(sys.argv[1:]))
