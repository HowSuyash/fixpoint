"""Run test commands against a workspace: in a locked-down Docker container when available."""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT = 12_000
SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.I)


@dataclass
class RunResult:
    command: str
    exit_code: int
    output: str
    seconds: float
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


def tail(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def docker_ready(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _quote(path: str) -> str:
    return subprocess.list2cmdline([path]) if os.name == "nt" else "'" + path.replace("'", "'\\''") + "'"


@functools.lru_cache(maxsize=1)
def _packages_dir() -> str | None:
    """The directory this process imports pytest from.

    Serverless hosts (Vercel) put the installed packages on sys.path at startup, and a child interpreter does not
    inherit that, so local test runs pass this directory on in PYTHONPATH.
    """
    spec = importlib.util.find_spec("pytest")
    if spec is None or not spec.origin:
        return None
    return str(Path(spec.origin).parent.parent)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


class Sandbox:
    """mode is "docker", "local" or "auto" (Docker if the sandbox image exists, else local)."""

    def __init__(self, root: Path, mode: str = "auto", image: str = "fixpoint-sandbox", timeout: int = 120):
        if mode == "auto":
            mode = "docker" if docker_ready(image) else "local"
        if mode not in {"docker", "local"}:
            raise ValueError(f"unknown sandbox mode {mode!r}")
        self.root = Path(root).resolve()
        self.mode = mode
        self.image = image
        self.timeout = timeout

    def run(self, command: str) -> RunResult:
        if self.mode == "docker":
            name = f"fixpoint-{uuid.uuid4().hex[:12]}"
            args = [
                "docker", "run", "--rm", "--name", name,
                "--network", "none", "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
                "-e", "PYTHONDONTWRITEBYTECODE=1",
                "--mount", f"type=bind,source={self.root},target=/work", "-w", "/work",
            ]
            if hasattr(os, "getuid"):
                args += ["--user", f"{os.getuid()}:{os.getgid()}"]
            args += [self.image, "sh", "-c", command]
            return self._exec(args, command, shell=False, on_timeout=lambda: subprocess.run(["docker", "kill", name], capture_output=True))

        # Local mode: use this interpreter (so pytest is importable) and hide secrets from the code under test.
        local = command
        if command == "python" or command.startswith("python "):
            local = _quote(sys.executable) + command[len("python"):]
        return self._exec(local, command, shell=True, python_path=_packages_dir())

    def _exec(self, args, label: str, shell: bool, on_timeout=None, python_path: str | None = None) -> RunResult:
        env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if python_path:
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [python_path, env.get("PYTHONPATH")]))
        extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        started = time.monotonic()
        proc = subprocess.Popen(
            args, shell=shell, cwd=self.root, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", **extra,
        )
        try:
            out, _ = proc.communicate(timeout=self.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            if on_timeout:
                on_timeout()
            _kill_tree(proc)
            out, _ = proc.communicate()
            out = (out or "") + f"\n[fixpoint] command timed out after {self.timeout}s"
            timed_out = True
        code = -1 if timed_out else proc.returncode
        return RunResult(label, code, tail(out or ""), round(time.monotonic() - started, 2), timed_out)
