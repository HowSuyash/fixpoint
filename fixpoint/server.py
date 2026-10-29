"""Web app: start a run, then stream its events to the browser over Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__, workspace
from .agent import Fixpoint
from .config import load_settings
from .sandbox import docker_ready

STATIC = Path(__file__).parent / "static"
MAX_ISSUE_CHARS = 20_000
MAX_KEPT_RUNS = 50

settings = load_settings()
app = FastAPI(title="Fixpoint", version=__version__)
_slots = threading.BoundedSemaphore(settings.max_concurrent_runs)


@dataclass
class Run:
    id: str
    repo: str
    issue: str
    events: list[dict] = field(default_factory=list)
    done: bool = False
    report: dict | None = None

    def add(self, event: dict) -> None:
        self.events.append(event)  # list.append is atomic; readers only slice


runs: dict[str, Run] = {}
_started: dict[str, list[float]] = {}  # client IP (and "*" for everyone) -> run start times
_started_lock = threading.Lock()
DAY = 86_400


class RunRequest(BaseModel):
    repo: str = ""
    issue: str = ""


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _take_quota(ip: str) -> None:
    """Enforce the per-visitor and overall daily run limits that protect the demo's API credits."""
    now = time.time()
    with _started_lock:
        everyone = [t for t in _started.get("*", []) if now - t < DAY]
        mine = [t for t in _started.get(ip, []) if now - t < DAY]
        if settings.max_runs_per_day and len(everyone) >= settings.max_runs_per_day:
            raise HTTPException(429, "Today's demo budget is used up. Try again tomorrow, or run Fixpoint locally (see the README).")
        if settings.runs_per_ip_per_day and len(mine) >= settings.runs_per_ip_per_day:
            raise HTTPException(429, f"The demo allows {settings.runs_per_ip_per_day} runs per visitor per day. Run Fixpoint locally for more (see the README).")
        _started["*"] = everyone + [now]
        _started[ip] = mine + [now]


def _resolve(repo: str, issue: str) -> tuple[str, str]:
    repo, issue = repo.strip(), issue.strip()
    if workspace.GITHUB_ISSUE.match(issue):
        if not settings.allow_git_urls:
            raise ValueError("GitHub issue URLs are disabled on this server (set FIXPOINT_ALLOW_GIT_URLS=1)")
        issue, inferred = workspace.fetch_github_issue(issue)
        repo = repo or inferred
    if not issue:
        example = next((e for e in workspace.list_examples() if e["name"] == repo), None)
        issue = example["issue"] if example else ""
    if not repo or not issue:
        raise ValueError("Choose a repository and describe the bug.")
    return repo, issue


def _execute(run: Run) -> None:
    settings.workdir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=settings.workdir))
    try:
        repo, issue = _resolve(run.repo, run.issue)
        work = workspace.prepare(repo, root, settings.allow_git_urls)
        run.report = asdict(Fixpoint(settings, work, issue, emit=run.add).run())
    except Exception as exc:
        run.add({"type": "error", "phase": "setup", "message": f"{type(exc).__name__}: {exc}"})
        run.add({"type": "done", "phase": "done", "report": {"status": "error", "error": str(exc)}})
    finally:
        run.done = True
        _slots.release()
        try:
            workspace.remove_tree(root)
        except OSError:
            pass


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/info")
def info() -> dict:
    sandbox = settings.sandbox
    if sandbox == "auto":
        sandbox = "docker" if docker_ready(settings.sandbox_image) else "local"
    return {
        "version": __version__,
        "models": {name: tier.model for name, tier in settings.tiers.items()},
        "nebius_key": bool(settings.nebius_api_key),
        "tavily": bool(settings.tavily_api_key),
        "sandbox": sandbox,
        "allow_git_urls": settings.allow_git_urls and not settings.public_demo,
        "public_demo": settings.public_demo,
        "examples": workspace.list_examples(),
    }


@app.post("/api/runs")
def create_run(body: RunRequest, request: Request) -> dict:
    if not settings.nebius_api_key:
        raise HTTPException(503, "The server has no NEBIUS_API_KEY configured.")
    if len(body.issue) > MAX_ISSUE_CHARS:
        raise HTTPException(413, "The bug report is too long.")
    issue = body.issue
    if settings.public_demo:
        if body.repo not in {e["name"] for e in workspace.list_examples()}:
            raise HTTPException(400, "The public demo runs the bundled examples only. Run Fixpoint locally for your own repositories.")
        issue = ""  # always use the example's own bug report, so visitors cannot steer the agent
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "All agent slots are busy. Try again in a minute.")
    try:
        _take_quota(_client_ip(request))
    except HTTPException:
        _slots.release()
        raise
    run = Run(id=uuid.uuid4().hex[:12], repo=body.repo, issue=issue)
    runs[run.id] = run
    for old in list(runs)[:-MAX_KEPT_RUNS]:
        if runs[old].done:
            runs.pop(old, None)
    threading.Thread(target=_execute, args=(run,), daemon=True).start()
    return {"id": run.id}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run")
    return {"id": run.id, "done": run.done, "events": len(run.events), "report": run.report}


@app.get("/api/runs/{run_id}/events")
async def stream_events(run_id: str, request: Request) -> StreamingResponse:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "Unknown run")
    last = request.headers.get("last-event-id", "")
    start = int(last) + 1 if last.isdigit() else 0

    async def events():
        index = start
        while True:
            batch = run.events[index:]
            for event in batch:
                yield f"id: {index}\ndata: {json.dumps(event, default=str)}\n\n"
                index += 1
            if run.done and index >= len(run.events):
                yield "event: end\ndata: {}\n\n"
                return
            if await request.is_disconnected():
                return
            if not batch:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.3)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
