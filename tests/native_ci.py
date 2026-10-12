"""Exercise native entry points with a Windows PATH that cannot find Git Bash."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="review-all-native-path-") as temp:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        if os.name == "nt":
            bin_dir = Path(temp)
            for name in ("uv", "just"):
                path = shutil.which(name)
                if not path:
                    raise RuntimeError(f"{name} missing")
                shutil.copy2(path, bin_dir / (name + ".exe"))
            git = Path(shutil.which("git")).parent
            if git.name.lower() != "cmd":
                raise RuntimeError(f"expected native Git cmd directory, not Git Bash: {git}")
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0"
            env["PATH"] = os.pathsep.join([str(bin_dir), str(git), str(Path(sys.executable).parent), str(powershell)])
            for name in ("bash", "sh", "timeout", "lsof", "ss", "curl", "rsync", "jq", "sed", "awk", "grep"):
                if shutil.which(name, path=env["PATH"]):
                    raise RuntimeError(f"Unix utility leaked into native PATH: {name}")
        subprocess.run(["just", "setup"], cwd=root, env=env, check=True, timeout=300)
        subprocess.run(["just", "test"], cwd=root, env=env, check=True, timeout=900)
        subprocess.run(["uv", "run", "--project", str(root), "--locked", "--no-sync", "--no-python-downloads",
                        "review-all", "--repo", str(root), "discover", "--harness", "copilot"],
                       cwd=root, env=env, check=True, timeout=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
