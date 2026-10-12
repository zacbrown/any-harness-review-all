"""Read-only discovery and bounded TCP/HTTP probes; no shell utilities."""

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse

from portable import execute, run


def preflight():
    return {**{tool: shutil.which(tool) is not None for tool in ("git", "gh")},
            "capabilities": {"timeouts": True, "tcp": True, "http": True, "hashing": True}}


def root(repo):
    repo = Path(repo).resolve(strict=True)
    result = execute(["git", "rev-parse", "--show-toplevel"], cwd=repo)
    if result.timed_out:
        raise TimeoutError("git repository discovery timed out")
    return Path(result.stdout.decode("utf-8").strip()) if result.returncode == 0 else repo


def tracked(repo, *args):
    result = execute(["git", "ls-files", "-z", *args], cwd=repo)
    if result.returncode:
        if (Path(repo) / ".git").exists():
            raise ValueError(result.stderr.decode("utf-8", errors="replace"))
        return []
    return result.stdout.decode("utf-8").rstrip("\0").split("\0") if result.stdout else []


def toolchain(repo):
    repo = Path(repo)
    data = dict.fromkeys(("ecosystem", "framework", "test", "lint", "typecheck", "build"), "")
    commands = {}

    def set_commands(ecosystem, framework=None, **values):
        data.update(ecosystem=ecosystem, framework=framework or ecosystem)
        for name, argv in values.items():
            data[name] = " ".join(argv)
            commands[name] = {"argv": argv}

    def exists(*names):
        return any((repo / name).is_file() for name in names)

    if exists("package.json"):
        package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
        manager = "npm"
        for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"),
                           ("bun.lock", "bun"), ("bun.lockb", "bun")):
            if exists(lock):
                manager = name
                break
        declared = package.get("packageManager", "").split("@")[0]
        if declared in {"npm", "pnpm", "yarn", "bun"}:
            manager = declared
        framework = "js"
        for name, files in (("angular", ["angular.json"]), ("next", ["next.config.js", "next.config.mjs", "next.config.ts"]),
                            ("vite", ["vite.config.js", "vite.config.ts", "vite.config.mjs"]),
                            ("nuxt", ["nuxt.config.js", "nuxt.config.ts"]), ("svelte", ["svelte.config.js"])):
            if exists(*files):
                framework = name
                break
        set_commands("js", framework)
        scripts = package.get("scripts", {})
        for name in ("test", "lint", "typecheck", "build"):
            script = scripts.get(name)
            if script:
                if not isinstance(script, str):
                    raise ValueError(f"package.json scripts.{name} must be text")
                data[name] = script
                commands[name] = {"argv": [manager, "run", name], "packageScript": script}
        if not data["typecheck"] and exists("tsconfig.json"):
            # Do not download a compiler. Resolve only the project's installed one.
            compiler = repo / "node_modules/typescript/bin/tsc"
            data["typecheck"] = "node node_modules/typescript/bin/tsc --noEmit"
            commands["typecheck"] = {"argv": ["node", str(compiler), "--noEmit"]}
    elif exists("pyproject.toml", "setup.cfg", "requirements.txt"):
        set_commands("python")
        for name, choices in (("test", [["pytest"]]), ("lint", [["ruff", "check", "."], ["flake8"]]),
                              ("typecheck", [["mypy", "."], ["pyright"]])):
            for argv in choices:
                if shutil.which(argv[0]):
                    data[name] = " ".join(argv)
                    commands[name] = {"argv": argv}
                    break
    elif exists("Cargo.toml"):
        set_commands("rust", test=["cargo", "test"], lint=["cargo", "clippy"],
                     typecheck=["cargo", "check"], build=["cargo", "build"])
    elif exists("go.mod"):
        set_commands("go", test=["go", "test", "./..."], typecheck=["go", "vet", "./..."],
                     build=["go", "build", "./..."])
        if shutil.which("golangci-lint"):
            data["lint"] = "golangci-lint run"
            commands["lint"] = {"argv": ["golangci-lint", "run"]}
    elif exists("pom.xml"):
        set_commands("java", "maven", test=["mvn", "test"], build=["mvn", "package", "-DskipTests"])
    elif exists("build.gradle", "build.gradle.kts"):
        set_commands("java", "gradle", test=["gradle", "test"], build=["gradle", "build", "-x", "test"])
    elif list(repo.glob("*.sln")) or list(repo.glob("*.csproj")):
        set_commands("dotnet", test=["dotnet", "test"], build=["dotnet", "build"])
    elif exists("Gemfile"):
        set_commands("ruby", test=["bundle", "exec", "rspec"])
        if shutil.which("rubocop"):
            data["lint"], commands["lint"] = "rubocop", {"argv": ["rubocop"]}
    elif exists("composer.json"):
        set_commands("php", test=["php", "vendor/bin/phpunit"])
    data["commands"] = commands
    return data


def test_pattern(repo):
    candidates = [n for n in tracked(repo) if re.search(
        r"(^|/)tests?/|\.(spec|test)\.(ts|tsx|js|jsx|py|rs|go|java|kt|rb)$", n)][:50]
    if not candidates:
        return {"pattern": "unknown", "suffix": "", "framework": "unknown"}
    separate = sum(bool(re.search(r"(^|/)tests?/", n)) for n in candidates)
    suffixes = Counter(m.group() for n in candidates if (m := re.search(r"\.(spec|test)\.[a-z]+$", n)))
    suffix = max(suffixes, key=lambda s: (suffixes[s], s)) if suffixes else ""
    repo = Path(repo)
    framework = "unknown"
    if (repo / "package.json").exists():
        text = (repo / "package.json").read_text(encoding="utf-8")
        for name, pattern in (("jest", '"jest"'), ("vitest", '"vitest"|"@vitest/'), ("mocha", '"mocha"'),
                              ("karma", '"karma"|jasmine'), ("playwright", '"playwright"')):
            if re.search(pattern, text):
                framework = name
                break
    else:
        for name, files in (("pytest", ("pyproject.toml", "setup.cfg")), ("cargo-test", ("Cargo.toml",)),
                            ("go-test", ("go.mod",))):
            if any((repo / f).exists() for f in files):
                framework = name
                break
    return {"pattern": "separate-tree" if separate > len(candidates) - separate else "co-located",
            "suffix": suffix, "framework": framework}


def discover(repo, harness="claude"):
    available = preflight()
    if not available["git"]:
        raise FileNotFoundError("git is required")
    repo = root(repo)
    result = {"schemaVersion": 2, "repo": str(repo), "available": available,
              "toolchain": toolchain(repo), "testPattern": test_pattern(repo),
              "codegraphIndex": (repo / ".codegraph").is_dir()}
    if harness == "copilot":
        return result  # Copilot collects scoped instructions separately, always fresh.
    names = set(tracked(repo, "--cached", "--others", "--exclude-standard", "--", "CLAUDE.md", "*/CLAUDE.md"))
    if (repo / "CLAUDE.local.md").is_file():
        names.add("CLAUDE.local.md")
    manifest = b"v2\n"
    for name in sorted(names, key=lambda n: n.encode("utf-8")):
        path = repo / name
        if path.is_file():
            manifest += f"{name} {hashlib.sha256(path.read_bytes()).hexdigest()}\n".encode("utf-8")
    key = hashlib.sha256(manifest).hexdigest()
    cache = repo / ".claude/cache/review-all-profile.json"
    profile, reason = None, "no-cache"
    if cache.is_file():
        try:
            profile = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reason = "unreadable"
        else:
            if not isinstance(profile, dict) or profile.get("schemaVersion") != 2:
                reason = "schema"
            elif profile.get("cacheKey") != key:
                reason = "key-mismatch"
            elif time.time() - cache.stat().st_mtime > 7 * 86400:
                reason = "expired"
            else:
                reason = None
    result.update(cacheKey=key, cache={"status": "MISS" if reason else "HIT", "reason": reason},
                  cachedProfile=profile if reason is None else None)
    return result


def dev_server(ports):
    opened, closed = [], []
    for port in ports:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("ports must be integers from 1 to 65535")
        for host in ("127.0.0.1", "::1"):
            try:
                with socket.create_connection((host, port), timeout=0.3):
                    opened.append(port)
                    break
            except OSError:
                continue
        else:
            closed.append(port)
    return {"open": opened, "closed": closed}


def _http_once(url, timeout, insecure=False):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
    if not url.startswith(("http://", "https://")):
        raise ValueError("HTTP probe requires an http:// or https:// URL")
    started = time.monotonic()
    try:
        with opener.open(url, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return {"url": url, "status": None, "health": "unreachable", "error": str(error),
                "elapsedSeconds": time.monotonic() - started}
    return {"url": url, "status": status,
            "health": "healthy" if 200 <= status < 400 else "auth-gated" if status in {401, 403} else "unhealthy",
            "elapsedSeconds": time.monotonic() - started}


def _http_retries(url, timeout):
    for attempt in range(3):
        result = _http_once(url, timeout)
        if result["status"] == 400 and url.startswith("http://") and urllib.parse.urlparse(url).hostname in {
                "localhost", "127.0.0.1", "::1"}:
            result = _http_once("https://" + url[7:], timeout, insecure=True)
        if result["health"] in {"healthy", "auth-gated"}:
            return result
        if attempt < 2:
            time.sleep(2)
    return result


def http_probe(url, timeout=5):
    if not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("HTTP timeout must be finite and positive")
    # An OS socket timeout alone is not a wall-clock bound on slow headers.
    budget = 6 * timeout + 5
    try:
        result = run([sys.executable, __file__, url, str(timeout)], cwd=Path.cwd(),
                     capture_output=True, timeout=budget, check=True)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"url": url, "status": None, "health": "unreachable", "error": "HTTP probe timed out",
                "elapsedSeconds": budget}


if __name__ == "__main__":
    print(json.dumps(_http_retries(sys.argv[1], float(sys.argv[2]))))
