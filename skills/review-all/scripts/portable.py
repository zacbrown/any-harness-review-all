"""Shared native process, encoding and locking mechanics."""

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def environment_identity():
    return {"system": platform.system(), "machine": platform.machine(),
            "python": platform.python_version()}


def command_line(argv, env, cwd):
    if not argv or not all(isinstance(a, (str, os.PathLike)) for a in argv):
        raise ValueError("command must be a nonempty executable-plus-arguments array")
    argv = [os.fspath(a) for a in argv]
    if any("\0" in a for a in argv):
        raise ValueError("NUL is not permitted in command arguments")
    name = argv[0]
    if os.path.dirname(name):
        executable = str((Path(cwd or Path.cwd()) / name).resolve())
    else:
        executable = shutil.which(name, path=env.get("PATH"))
    if not executable:
        raise FileNotFoundError(f"executable not found: {name}")
    argv[0] = executable
    if Path(executable).suffix.lower() == ".py":
        return [sys.executable, *argv]
    if os.name != "nt":
        return argv
    suffix = Path(executable).suffix.lower()
    if suffix == ".ps1":
        powershell = shutil.which("pwsh", path=env.get("PATH")) or shutil.which("powershell", path=env.get("PATH"))
        if not powershell:
            raise FileNotFoundError("PowerShell is required by the configured .ps1 command")
        return [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", *argv]
    if suffix in {".cmd", ".bat"}:
        # Expansion happens once, with delayed expansion disabled. Keep values
        # out of command source; reject quotes/newlines which break cmd's quoting.
        if any('"' in a or "\r" in a or "\n" in a for a in argv):
            raise ValueError("cmd/bat arguments cannot contain quotes or newlines; use a native executable")
        for i, arg in enumerate(argv):
            env[f"REVIEW_ALL_ARG_{i}"] = re.sub(r"(\\+)$", lambda m: m[0] * 2, arg)
        command = " ".join(f'"%REVIEW_ALL_ARG_{i}%"' for i in range(len(argv)))
        comspec = str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe")
        return f'"{comspec}" /d /v:off /s /c "{command}"'
    return argv


class WindowsJob:
    """Assign a suspended process before it can spawn uncontained descendants."""

    def __init__(self, process):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [w.HANDLE]
            resume.restype = ctypes.c_long
            if resume(int(process._handle)) != 0:
                raise OSError("could not resume contained Windows process")
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


@dataclass
class Execution:
    args: list
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed: float
    timed_out: bool

    def as_json(self):
        return {"command": self.args, "exitCode": self.returncode,
                "stdout": self.stdout.decode("utf-8", errors="replace"),
                "stderr": self.stderr.decode("utf-8", errors="replace"),
                "elapsedSeconds": self.elapsed, "timedOut": self.timed_out}


def execute(argv, *, cwd, timeout=180, input=None, env=None):
    if not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a finite positive number")
    environment = dict(os.environ if env is None else env)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUTF8", "1")
    command = command_line(argv, environment, cwd)
    started = time.monotonic()
    job = None
    timed_out = False
    # Disk-backed output cannot fill a pipe or keep communicate waiting after a
    # parent exits with inherited handles still open in a descendant.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.PIPE,
                                stdout=stdout, stderr=stderr, start_new_session=os.name != "nt",
                                creationflags=0x4 if os.name == "nt" else 0)
        try:
            if os.name == "nt":
                job = WindowsJob(proc)
            try:
                proc.communicate(input=input, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
        finally:
            if os.name == "nt":
                if job:
                    job.close()
                elif proc.poll() is None:
                    proc.kill()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait(timeout=10)
            if proc.stdin:
                proc.stdin.close()
        stdout.seek(0)
        stderr.seek(0)
        return Execution(list(map(os.fspath, argv)), 124 if timed_out else proc.returncode,
                         stdout.read(), stderr.read(), time.monotonic() - started, timed_out)


def run(argv, *, cwd=None, timeout=180, capture_output=False, stdout=None, stderr=None,
        input=None, text=False, encoding="utf-8", check=False, env=None):
    """subprocess.run-compatible boundary, with bounded descendant cleanup."""
    result = execute(argv, cwd=cwd or Path.cwd(), timeout=timeout,
                     input=input.encode(encoding) if isinstance(input, str) else input, env=env)
    if result.timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=result.stdout, stderr=result.stderr)
    output, errors = result.stdout, result.stderr
    if capture_output:
        if text:
            output, errors = output.decode(encoding), errors.decode(encoding)
    else:
        import sys
        for data, stream, default in ((output, stdout, sys.stdout), (errors, stderr, sys.stderr)):
            if stream == subprocess.DEVNULL:
                continue
            target = stream if stream is not None else default
            if hasattr(target, "buffer"):
                target.buffer.write(data)
                target.flush()
            else:
                if isinstance(target, io.TextIOBase):
                    target.write(data.decode(encoding, errors="replace"))
                else:
                    target.write(data)
        output = errors = None
    completed = subprocess.CompletedProcess(argv, result.returncode, output, errors)
    if check:
        completed.check_returncode()
    return completed


@contextmanager
def file_lock(path, timeout=30):
    path = Path(path)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                import errno
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring {path}") from error
                time.sleep(0.02)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
