"""Repository tools the agents call, with per-phase write permissions.

During reproduction the agent may only touch test files; during the fix, test
files are locked. That ordering is what keeps the agent honest: it cannot make
the suite green by editing the test that proves the bug.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .sandbox import RunResult, Sandbox

TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")
SELECTOR = re.compile(r"^[\w./:\[\]\- ]+$")
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".mypy_cache", ".tox", "dist", "build"}
MAX_FILE_BYTES = 300_000
MAX_READ_LINES = 400

SPECS = {
    "list_files": (
        "List files in the repository (directories end with /).",
        {"path": {"type": "string", "description": "Directory relative to the repo root. Default: ."}},
        [],
    ),
    "read_file": (
        "Read a text file with line numbers. Long files are returned in chunks.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "start_line": {"type": "integer", "description": "First line to show (1-based). Default 1."},
            "end_line": {"type": "integer", "description": "Last line to show. Default: start_line + 400."},
        },
        ["path"],
    ),
    "search_code": (
        "Search file contents with a regular expression. Returns file:line: text matches.",
        {
            "pattern": {"type": "string", "description": "Python regular expression (or plain text)."},
            "path": {"type": "string", "description": "Directory or file to search. Default: ."},
        },
        ["pattern"],
    ),
    "write_file": (
        "Create or overwrite a file with the given content.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "content": {"type": "string", "description": "The complete new file content."},
        },
        ["path", "content"],
    ),
    "replace_in_file": (
        "Replace one exact occurrence of `old` with `new` in a file. `old` must match the file exactly "
        "(including indentation) and appear exactly once; include surrounding lines to make it unique.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "old": {"type": "string", "description": "Exact text to replace."},
            "new": {"type": "string", "description": "Replacement text."},
        },
        ["path", "old", "new"],
    ),
    "run_tests": (
        "Run the test suite in the sandbox and return the exit code and output.",
        {"target": {"type": "string", "description": "Optional test file or node id, e.g. tests/test_x.py::test_y. Empty = whole suite."}},
        [],
    ),
    "web_search": (
        "Search the web (Tavily) for documentation, library behaviour or an error message. Returns a short answer and sources.",
        {"query": {"type": "string", "description": "Search query."}},
        ["query"],
    ),
    "finish": (
        "Call when the goal of this phase is met. Fixpoint re-runs the tests to verify before accepting.",
        {"summary": {"type": "string", "description": "What you did and why it works."}},
        ["summary"],
    ),
}


def spec(name: str) -> dict:
    description, properties, required = SPECS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


class ToolError(Exception):
    pass


def is_test_file(rel: str) -> bool:
    return bool(TEST_FILE.search(rel.replace("\\", "/")))


@dataclass
class Toolbox:
    root: Path
    sandbox: Sandbox
    test_command: str
    write_policy: str = "none"  # "tests", "source" or "none"
    web_search: Callable[[str], str] | None = None
    on_test_run: Callable[[RunResult], None] | None = None
    files_written: set[str] = field(default_factory=set)

    def __post_init__(self):
        self.root = Path(self.root).resolve()

    # --- plumbing -------------------------------------------------------

    def specs(self, names: list[str]) -> list[dict]:
        return [spec(name) for name in names]

    def execute(self, name: str, args: dict) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return f"ERROR: unknown tool {name!r}"
        try:
            return handler(**args)
        except ToolError as exc:
            return f"ERROR: {exc}"
        except TypeError as exc:
            return f"ERROR: bad arguments for {name}: {exc}"
        except (OSError, UnicodeError, re.error) as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"

    def resolve(self, path: str) -> tuple[Path, str]:
        target = (self.root / (path or ".")).resolve()
        if target != self.root and self.root not in target.parents:
            raise ToolError(f"{path} is outside the repository")
        rel = target.relative_to(self.root).as_posix() if target != self.root else "."
        if ".git" in Path(rel).parts:
            raise ToolError("the .git directory is off limits")
        return target, rel

    def text_files(self, start: Path | None = None) -> list[Path]:
        start = start or self.root
        if start.is_file():
            return [start]
        found = []
        for dirpath, dirnames, filenames in os.walk(start):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for filename in sorted(filenames):
                path = Path(dirpath) / filename
                try:
                    if path.stat().st_size > MAX_FILE_BYTES or b"\0" in path.read_bytes()[:2048]:
                        continue
                except OSError:
                    continue
                found.append(path)
        return found

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _read(self, target: Path) -> tuple[str, bool]:
        raw = target.read_bytes().decode("utf-8", errors="replace")
        return raw.replace("\r\n", "\n"), "\r\n" in raw

    def _write(self, target: Path, rel: str, text: str, crlf: bool = False) -> None:
        if self.write_policy == "none":
            raise ToolError("Files are read-only in this phase.")
        if self.write_policy == "tests" and not is_test_file(rel):
            raise ToolError(
                f"{rel} is not a test file. In this phase you may only create or edit tests "
                "(under tests/ or named test_*.py). Write the reproduction test first; the fix comes next."
            )
        if self.write_policy == "source" and is_test_file(rel):
            raise ToolError(f"{rel} is a test file and tests are locked while fixing. Change the code under test instead.")
        target.parent.mkdir(parents=True, exist_ok=True)
        if crlf:
            text = text.replace("\n", "\r\n")
        target.write_bytes(text.encode("utf-8"))
        self.files_written.add(rel)

    # --- tools ----------------------------------------------------------

    def _tool_list_files(self, path: str = ".") -> str:
        target, _ = self.resolve(path)
        if not target.is_dir():
            raise ToolError(f"{path} is not a directory")
        lines = []
        for dirpath, dirnames, filenames in os.walk(target):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            base = Path(dirpath)
            depth = len(base.relative_to(target).parts)
            if depth >= 4:
                dirnames[:] = []
            for d in dirnames:
                lines.append(self.rel(base / d) + "/")
            for f in sorted(filenames):
                lines.append(self.rel(base / f))
            if len(lines) > 300:
                lines = lines[:300] + ["... (truncated)"]
                break
        return "\n".join(sorted(lines)) or "(empty)"

    def _tool_read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        target, rel = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} does not exist")
        lines = self._read(target)[0].split("\n")
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:>5} | {lines[n - 1]}" for n in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines; read again with start_line={end + 1})" if end < len(lines) else ""
        return f"{rel} (lines {start}-{end} of {len(lines)})\n{body}{more}"

    def _tool_search_code(self, pattern: str, path: str = ".") -> str:
        target, _ = self.resolve(path)
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        hits = []
        for file in self.text_files(target):
            text = self._read(file)[0]
            for n, line in enumerate(text.split("\n"), 1):
                if regex.search(line):
                    hits.append(f"{self.rel(file)}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 50:
                        return "\n".join(hits) + "\n... (more matches truncated)"
        return "\n".join(hits) or "no matches"

    def _tool_write_file(self, path: str, content: str) -> str:
        target, rel = self.resolve(path)
        crlf = target.is_file() and self._read(target)[1]
        self._write(target, rel, content, crlf)
        return f"wrote {rel} ({content.count(chr(10)) + 1} lines)"

    def _tool_replace_in_file(self, path: str, old: str, new: str) -> str:
        target, rel = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"{path} does not exist")
        text, crlf = self._read(target)
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        count = text.count(old) if old else 0
        if count != 1:
            hint = "not found; read the file again and copy the text exactly" if count == 0 else f"found {count} times; include more context"
            raise ToolError(f"`old` {hint}")
        self._write(target, rel, text.replace(old, new, 1), crlf)
        return f"edited {rel}"

    def _tool_run_tests(self, target: str = "") -> str:
        result = self.run_tests(target)
        verdict = "PASSED" if result.passed else "FAILED"
        return f"exit code {result.exit_code} ({verdict}) in {result.seconds}s\n{result.output}"

    def _tool_web_search(self, query: str) -> str:
        if self.web_search is None:
            return "Web search is not configured (no TAVILY_API_KEY). Rely on the code and your own knowledge."
        return self.web_search(query)

    def run_tests(self, target: str = "") -> RunResult:
        target = (target or "").strip()
        if target and not SELECTOR.match(target):
            raise ToolError("target may only contain file paths and test node ids")
        command = f"{self.test_command} {target}".strip()
        result = self.sandbox.run(command)
        if self.on_test_run:
            self.on_test_run(result)
        return result


def make_web_search(api_key: str, on_event: Callable[[dict], None], client=None) -> Callable[[str], str]:
    """Tavily search, condensed into a short block of text the model can cite."""
    if client is None:
        from tavily import TavilyClient

        client = TavilyClient(api_key=api_key)

    def search(query: str) -> str:
        result = client.search(query=query, search_depth="advanced", max_results=5, include_answer=True)
        # Some page titles arrive with U+FFFD where a dash was; show a plain dash instead.
        sources = [{"title": (r.get("title") or "").replace("�", "-"), "url": r.get("url", "")} for r in result.get("results", [])]
        on_event({"type": "web_search", "query": query, "answer": result.get("answer") or "", "sources": sources})
        lines = []
        if result.get("answer"):
            lines.append(f"Answer: {result['answer']}")
        for r in result.get("results", [])[:5]:
            lines.append(f"- {r.get('title', '')} ({r.get('url', '')})\n  {(r.get('content') or '')[:600]}")
        return "\n".join(lines) or "no results"

    return search


def preview(args: dict, limit: int = 4000) -> str:
    text = json.dumps(args, indent=1, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"
