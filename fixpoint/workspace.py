"""Copy or clone the target repository into a scratch workspace with a git baseline for diffs."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import httpx

IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "*.pyc", ".env"
)
GIT_URL = re.compile(r"^https://(github\.com|gitlab\.com|bitbucket\.org)/[\w.-]+/[\w.-]+?(\.git)?/?$")
GITHUB_ISSUE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)/?$")


def examples_dir() -> Path:
    candidates = [
        Path(os.environ.get("FIXPOINT_EXAMPLES_DIR", "")),
        Path(__file__).resolve().parent.parent / "examples",
        Path.cwd() / "examples",
    ]
    for path in candidates:
        if str(path) not in {"", "."} and path.is_dir():
            return path
    return candidates[1]


def list_examples() -> list[dict]:
    found = []
    root = examples_dir()
    if not root.is_dir():
        return found
    for path in sorted(root.iterdir()):
        issue = path / "ISSUE.md"
        if path.is_dir() and issue.is_file():
            text = issue.read_text(encoding="utf-8")
            title = text.strip().splitlines()[0].lstrip("# ").strip()
            found.append({"name": path.name, "title": title, "issue": text})
    return found


def fetch_github_issue(url: str) -> tuple[str, str]:
    """Return (issue text, repo URL) for a public GitHub issue URL."""
    owner, repo, number = GITHUB_ISSUE.match(url.strip()).groups()
    response = httpx.get(
        f"https://api.github.com/repos/{owner}/{repo}/issues/{number}",
        headers={"Accept": "application/vnd.github+json"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    text = f"# {data['title']}\n\n{data.get('body') or ''}".strip()
    return text, f"https://github.com/{owner}/{repo}"


def git(work: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],
        cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def prepare(source: str, root: Path, allow_git_urls: bool = True) -> Path:
    """Materialise source (example name, local directory or git URL) at root/repo and commit a baseline."""
    source = source.strip()
    work = Path(root) / "repo"
    examples = {e["name"] for e in list_examples()}
    if source in examples:
        shutil.copytree(examples_dir() / source, work, ignore=IGNORE)
        (work / "ISSUE.md").unlink(missing_ok=True)  # the issue is the agent's input, not part of the repo
    elif GIT_URL.match(source):
        if not allow_git_urls:
            raise ValueError("Cloning git URLs is disabled on this server (set FIXPOINT_ALLOW_GIT_URLS=1)")
        result = subprocess.run(["git", "clone", "--depth", "1", source, str(work)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git clone failed: {result.stderr.strip()}")
    elif Path(source).is_dir():
        shutil.copytree(source, work, ignore=IGNORE)
    else:
        raise ValueError(f"Unknown repository {source!r}: use an example name, a local directory or a https git URL")

    if not (work / ".git").exists():
        git(work, "init", "-q")
    (work / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (work / ".git" / "info" / "exclude").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "-c", "user.name=fixpoint", "-c", "user.email=fixpoint@localhost", "commit", "-q", "--allow-empty", "-m", "baseline")
    return work


def diff(work: Path) -> str:
    git(work, "add", "-A")
    return git(work, "diff", "--cached", "--no-color", "HEAD")


def changed_files(work: Path) -> list[str]:
    git(work, "add", "-A")
    return [line for line in git(work, "diff", "--cached", "--name-only", "HEAD").splitlines() if line]


def remove_tree(path: Path) -> None:
    """Delete a workspace, including git's read-only object files on Windows."""

    def make_writable_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)
