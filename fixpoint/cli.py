"""Command line: `fixpoint run`, `fixpoint serve`, `fixpoint check`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

from . import workspace
from .config import load_settings

TIER_COLOR = {"nano": "\033[32m", "super": "\033[36m", "ultra": "\033[35m"}
RESET, DIM, BOLD, RED, GREEN, YELLOW = "\033[0m", "\033[2m", "\033[1m", "\033[31m", "\033[32m", "\033[33m"


def _short(args: str) -> str:
    try:
        data = json.loads(args)
    except ValueError:
        return args[:80]
    for key in ("path", "target", "pattern", "query"):
        if data.get(key):
            return str(data[key])[:100]
    return "(whole suite)" if "target" in data or not data else ""


def print_event(e: dict) -> None:
    kind = e["type"]
    if kind == "phase":
        print(f"\n{BOLD}== {e['name'].upper()}{RESET}  {DIM}{e['detail']}{RESET}")
    elif kind == "llm":
        color = TIER_COLOR.get(e["tier"], "")
        print(f"  {color}[{e['tier']}]{RESET} {e['purpose']}  {DIM}{e['prompt_tokens']} in / {e['completion_tokens']} out, {e['seconds']}s{RESET}")
    elif kind == "triage":
        t = e["triage"]
        print(f"  summary: {t['summary']}\n  suspects: {', '.join(t['suspect_files']) or '-'}")
    elif kind == "web_search":
        print(f"  {YELLOW}tavily{RESET} {e['query']}")
        for source in e["sources"][:3]:
            print(f"    {DIM}{source['url']}{RESET}")
    elif kind == "plan":
        print(f"  root cause: {e['plan'].get('root_cause')}\n  fix plan: {e['plan'].get('fix_plan')}")
    elif kind == "tool_call":
        print(f"    > {e['tool']} {DIM}{_short(e['args'])}{RESET}")
    elif kind == "test_run":
        mark = f"{GREEN}PASS{RESET}" if e["passed"] else f"{RED}FAIL{RESET}"
        print(f"    {mark} {DIM}{e['command']} ({e['seconds']}s){RESET}")
    elif kind == "finish_check":
        print(f"    {GREEN if e['ok'] else YELLOW}finish: {e['message'].splitlines()[0]}{RESET}")
    elif kind == "escalation":
        print(f"  {TIER_COLOR['ultra']}escalating: {e['reason']}{RESET}")
    elif kind == "advice":
        print("    " + e["text"].replace("\n", "\n    "))
    elif kind == "note":
        print(f"  {DIM}{e['message']}{RESET}")
    elif kind == "error":
        print(f"  {RED}error: {e['message']}{RESET}")


def cmd_run(args: argparse.Namespace) -> int:
    from .agent import Fixpoint

    overrides = {"allow_git_urls": True}
    if args.test_cmd:
        overrides["test_command"] = args.test_cmd
    if args.sandbox:
        overrides["sandbox"] = args.sandbox
    settings = load_settings(**overrides)
    if not settings.nebius_api_key:
        print(f"{RED}NEBIUS_API_KEY is not set.{RESET} Copy .env.example to .env and add your key.", file=sys.stderr)
        return 2

    repo, issue = args.repo or "", args.issue or ""
    if args.issue_file:
        issue = Path(args.issue_file).read_text(encoding="utf-8")
    if workspace.GITHUB_ISSUE.match(issue.strip()):
        issue, inferred = workspace.fetch_github_issue(issue)
        repo = repo or inferred
    if not issue:
        example = next((e for e in workspace.list_examples() if e["name"] == repo), None)
        issue = example["issue"] if example else ""
    if not repo or not issue:
        print("Give a repository (--repo) and a bug report (--issue, --issue-file or a GitHub issue URL).", file=sys.stderr)
        return 2

    root = Path(tempfile.mkdtemp(prefix="fixpoint-"))
    work = workspace.prepare(repo, root, allow_git_urls=True)
    print(f"{BOLD}Fixpoint{RESET} on {repo}  {DIM}(workspace {work}){RESET}")
    report = Fixpoint(settings, work, issue, emit=print_event).run()

    color = GREEN if report.ok else RED
    print(f"\n{BOLD}{color}{report.status.upper()}{RESET} in {report.seconds}s, {report.test_runs} test runs, {report.escalations} escalations")
    for tier, stats in report.usage.items():
        print(f"  {tier:<6} {stats['calls']:>3} calls  {stats['prompt_tokens']:>8} in  {stats['completion_tokens']:>7} out  {DIM}{stats['model']}{RESET}")
    if report.cost_usd is not None:
        print(f"  cost: ${report.cost_usd:.4f} {DIM}(models with a known price){RESET}")
    if report.diff:
        print(f"\n{report.diff}")
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "fix.patch").write_text(report.diff, encoding="utf-8")
        (out / "PR.md").write_text(report.report_md, encoding="utf-8")
        (out / "report.json").write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        print(f"Wrote {out / 'fix.patch'}, {out / 'PR.md'} and {out / 'report.json'}")
    if args.keep:
        print(f"Workspace kept at {work}")
    else:
        workspace.remove_tree(root)
    return 0 if report.ok else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("fixpoint.server:app", host=args.host, port=args.port)
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    """Verify the API key, the configured Nemotron model IDs, Tavily and the sandbox."""
    from openai import OpenAI

    from .sandbox import docker_ready

    settings = load_settings()
    if not settings.nebius_api_key:
        print(f"{RED}NEBIUS_API_KEY is not set.{RESET} Copy .env.example to .env and add your key.")
        return 1
    client = OpenAI(base_url=settings.base_url, api_key=settings.nebius_api_key)
    try:
        available = {m.id for m in client.models.list()}
    except Exception as exc:
        print(f"{RED}Could not list models at {settings.base_url}: {exc}{RESET}")
        return 1
    nemotron = sorted(m for m in available if "nemotron" in m.lower())
    print(f"Token Factory: {len(available)} models, Nemotron: {', '.join(nemotron) or 'none'}")
    ok = True
    for tier in settings.tiers.values():
        if tier.model not in available:
            ok = False
            print(f"  {RED}{tier.name:<6} {tier.model} not found{RESET}  set FIXPOINT_{tier.name.upper()}_MODEL to one of the IDs above")
            continue
        started = time.monotonic()
        try:
            response = client.chat.completions.create(
                model=tier.model, messages=[{"role": "user", "content": "Reply with the single word: ready"}], max_tokens=512
            )
            text = (response.choices[0].message.content or "").strip().splitlines()[-1:] or [""]
            print(f"  {GREEN}{tier.name:<6}{RESET} {tier.model}  {time.monotonic() - started:.1f}s  {DIM}{text[0][:60]}{RESET}")
        except Exception as exc:
            ok = False
            print(f"  {RED}{tier.name:<6} {tier.model} failed: {exc}{RESET}")
    if settings.tavily_api_key:
        try:
            from tavily import TavilyClient

            hits = TavilyClient(api_key=settings.tavily_api_key).search(query="Nebius Token Factory", max_results=1)
            print(f"  {GREEN}tavily{RESET} ok ({len(hits.get('results', []))} result)")
        except Exception as exc:
            ok = False
            print(f"  {RED}tavily failed: {exc}{RESET}")
    else:
        print(f"  {YELLOW}tavily{RESET} not configured (optional): web research is skipped")
    mode = settings.sandbox
    if mode == "auto":
        mode = "docker" if docker_ready(settings.sandbox_image) else "local"
    print(f"  sandbox: {mode}" + ("" if mode == "docker" else f"  {DIM}(build the Docker sandbox with: docker build -f sandbox.Dockerfile -t {settings.sandbox_image} .){RESET}"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        os.system("")  # enable ANSI colours in the Windows console
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="fixpoint", description="Test-first bug fixing with NVIDIA Nemotron on Nebius Token Factory.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="fix a bug in a repository")
    run.add_argument("--repo", help="example name (billing, scheduler, ratelimit), local directory or https git URL")
    run.add_argument("--issue", help="bug report text, or a GitHub issue URL")
    run.add_argument("--issue-file", help="file containing the bug report")
    run.add_argument("--test-cmd", help="test command (default: python -m pytest -q)")
    run.add_argument("--sandbox", choices=["auto", "docker", "local"])
    run.add_argument("--out", help="directory to write fix.patch, PR.md and report.json")
    run.add_argument("--keep", action="store_true", help="keep the scratch workspace")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="start the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    check = sub.add_parser("check", help="verify API keys, model IDs and the sandbox")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
