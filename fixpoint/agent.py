"""The Fixpoint pipeline: triage, research, plan, reproduce (red), fix (green), report.

Model routing:
  nano  - triage and the pull request write-up (fast, cheap calls)
  ultra - root-cause planning and escalation when the fix loop stalls (deep reasoning)
  super - the tool-using agent loops that write the test and the fix
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import prompts, workspace
from .config import Settings
from .llm import LLM
from .sandbox import RunResult, Sandbox, tail
from .tools import Toolbox, make_web_search, preview

READ_TOOLS = ["list_files", "read_file", "search_code"]
WRITE_TOOLS = ["write_file", "replace_in_file"]
TRACEBACK_FILE = re.compile(r'File "([^"]+)", line \d+')
TEST_MODULE = re.compile(r"(^|/)test_[^/]*\.py$|_test\.py$")


@dataclass
class RunReport:
    status: str  # fixed | not_reproduced | not_fixed | error
    issue: str
    triage: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    research: str = ""
    sources: list[dict] = field(default_factory=list)  # web pages Tavily returned during the run
    repro_tests: list[str] = field(default_factory=list)
    diff: str = ""
    report_md: str = ""
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    test_runs: int = 0
    escalations: int = 0
    seconds: float = 0.0
    sandbox: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "fixed"


class Fixpoint:
    def __init__(
        self,
        settings: Settings,
        work: Path,
        issue: str,
        emit: Callable[[dict], None] | None = None,
        client: Any = None,
        web_search: Callable[[str], str] | None = None,
        tavily_client: Any = None,
    ):
        self.settings = settings
        self.work = Path(work)
        self.issue = issue.strip()[:20_000]
        self._emit = emit or (lambda event: None)
        self.phase = "setup"
        self.sources: list[dict] = []
        self.llm = LLM(settings, client=client, on_event=self.event_raw)
        self.sandbox = Sandbox(self.work, settings.sandbox, settings.sandbox_image, settings.test_timeout)
        if web_search is None and (settings.tavily_api_key or tavily_client):
            web_search = make_web_search(settings.tavily_api_key, self.event_raw, client=tavily_client)
        self.tools = Toolbox(self.work, self.sandbox, settings.test_command, web_search=web_search, on_test_run=self._on_test_run)
        self.test_runs = 0
        self.red_streak = 0
        self.escalations = 0
        self.last_run: RunResult | None = None

    # --- events ---------------------------------------------------------

    def event_raw(self, event: dict) -> None:
        if event.get("type") == "web_search":
            known = {s["url"] for s in self.sources}
            self.sources += [s for s in event.get("sources", []) if s.get("url") and s["url"] not in known]
        self._emit({"phase": self.phase, **event, "ts": round(time.time(), 3)})

    def event(self, kind: str, **data) -> None:
        self.event_raw({"type": kind, **data})

    def start_phase(self, name: str, detail: str) -> None:
        self.phase = name
        self.event("phase", name=name, detail=detail)

    def _on_test_run(self, result: RunResult) -> None:
        self.test_runs += 1
        self.last_run = result
        if self.phase == "fix":
            self.red_streak = 0 if result.passed else self.red_streak + 1
        self.event(
            "test_run", command=result.command, exit_code=result.exit_code, passed=result.passed,
            seconds=result.seconds, timed_out=result.timed_out, output=tail(result.output, 6000),
        )

    @property
    def _pytest(self) -> bool:
        return "pytest" in self.settings.test_command

    def _diff(self) -> str:
        try:
            return workspace.diff(self.work)
        except Exception:
            return ""

    # --- pipeline -------------------------------------------------------

    def run(self) -> RunReport:
        started = time.monotonic()
        report = RunReport(status="error", issue=self.issue, sandbox=self.sandbox.mode)
        self.event(
            "start", issue=self.issue[:4000], sandbox=self.sandbox.mode, tavily=self.tools.web_search is not None,
            models={name: tier.model for name, tier in self.settings.tiers.items()},
        )
        try:
            self._pipeline(report)
        except Exception as exc:  # report every failure to the UI instead of dying silently
            report.status = "error"
            report.error = f"{type(exc).__name__}: {exc}"
            self.event("error", message=report.error)
        report.diff = self._diff()
        report.sources = self.sources
        report.usage = self.llm.usage_summary()
        report.cost_usd = self.llm.cost_usd()
        report.test_runs = self.test_runs
        report.escalations = self.escalations
        report.seconds = round(time.monotonic() - started, 1)
        self.phase = "done"
        self.event("done", report=asdict(report))
        return report

    def _pipeline(self, report: RunReport) -> None:
        self.start_phase("triage", "Nemotron Nano reads the report and the repository layout")
        listing = self.tools.execute("list_files", {})
        report.triage = self.triage(listing)

        self.start_phase("research", "Tavily searches the web for outside knowledge")
        report.research = self.research(report.triage)

        self.start_phase("plan", "Nemotron Ultra finds the root cause and plans a test-first fix")
        report.plan = self.plan(report.triage, report.research, listing)

        self.start_phase("baseline", "Running the existing test suite before any change")
        baseline = self.tools.run_tests()
        baseline_green = baseline.passed or (self._pytest and baseline.exit_code == 5)  # 5 = no tests collected
        if not baseline_green:
            self.event("note", message="The suite already fails before any change; Fixpoint will judge the fix by the new test.")

        self.start_phase("reproduce", "Nemotron Super writes a test that fails because of the bug")
        report.repro_tests = self.reproduce(report)
        if not report.repro_tests:
            report.status = "not_reproduced"
            self.event("note", message="Could not write a failing test for this report, so no fix was attempted.")
            return

        self.start_phase("fix", "Nemotron Super edits the code until the suite is green; Ultra steps in if it stalls")
        report.status = "fixed" if self.fix(report, baseline_green) else "not_fixed"

        self.start_phase("report", "Nemotron Nano writes the pull request description")
        report.diff = self._diff()
        report.report_md = self.write_report(report)

    # --- phases ---------------------------------------------------------

    def triage(self, listing: str) -> dict:
        user = f"Bug report:\n{self.issue}\n\nRepository files:\n{listing}"
        try:
            data = self.llm.json_call("nano", prompts.TRIAGE, user, "triage")
        except ValueError:
            data = {}
        triage = {
            "summary": str(data.get("summary") or self.issue.splitlines()[0])[:300],
            "bug_type": str(data.get("bug_type") or "other"),
            "keywords": [str(k) for k in data.get("keywords") or [] if k][:5],
            "suspect_files": [str(f) for f in data.get("suspect_files") or [] if f][:4],
            "web_query": str(data.get("web_query") or "").strip(),
        }
        self.event("triage", triage=triage)
        return triage

    def research(self, triage: dict) -> str:
        query = triage.get("web_query") or triage.get("summary", "")  # fall back to searching the symptom
        if self.tools.web_search is None:
            self.event("note", message="TAVILY_API_KEY is not set, so web research is skipped.")
            return ""
        try:
            return self.tools.web_search(query[:400])
        except Exception as exc:
            self.event("note", message=f"Web search failed: {exc}")
            return ""

    def context_files(self, triage: dict) -> list[str]:
        """Pick the files the planner should see: traceback frames, triage suspects, keyword hits."""
        all_files = [self.tools.rel(p) for p in self.tools.text_files()]
        picks: list[str] = []
        for frame in TRACEBACK_FILE.findall(self.issue):
            frame = frame.replace("\\", "/")
            picks += [f for f in all_files if frame == f or frame.endswith("/" + f)]
        picks += [f for f in triage.get("suspect_files", []) if f in all_files]
        for keyword in triage.get("keywords", []):
            hits = self.tools.execute("search_code", {"pattern": re.escape(keyword)})
            picks += [line.split(":", 1)[0] for line in hits.splitlines() if line.split(":", 1)[0] in all_files]
        return list(dict.fromkeys(picks))[:6]

    def plan(self, triage: dict, research: str, listing: str) -> dict:
        files = self.context_files(triage)
        sources = "\n\n".join(self.tools.execute("read_file", {"path": f, "end_line": 300}) for f in files)
        user = (
            f"Bug report:\n{self.issue}\n\nTriage notes:\n{json.dumps(triage, indent=1)}\n\n"
            f"Repository files:\n{listing}\n\nRelevant files:\n{sources or '(none found)'}"
        )
        if research:
            user += f"\n\nWeb research (Tavily):\n{research}"
        try:
            plan = self.llm.json_call("ultra", prompts.PLAN, user, "root cause + plan")
        except ValueError:
            plan = {}
        plan.setdefault("root_cause", "unknown")
        plan.setdefault("expected_behavior", triage["summary"])
        plan.setdefault("test_plan", "Write a test that asserts the behaviour the report expects.")
        plan.setdefault("fix_plan", "Find and fix the code responsible.")
        self.event("plan", plan=plan, files=files)
        return plan

    def _brief(self, report: RunReport) -> str:
        brief = f"Bug report:\n{self.issue}\n\nPlan from the senior engineer:\n{json.dumps(report.plan, indent=1)}"
        if report.research:
            brief += f"\n\nWeb research (Tavily):\n{report.research[:4000]}"
        return brief

    def reproduce(self, report: RunReport) -> list[str]:
        self.tools.write_policy = "tests"
        self.tools.files_written.clear()

        def new_tests() -> list[str]:
            return sorted(f for f in self.tools.files_written if TEST_MODULE.search(f) and (self.work / f).is_file())

        def check(_args: dict) -> tuple[bool, str]:
            tests = new_tests()
            if not tests:
                return False, "You have not written a test file yet. Write a test that reproduces the bug, then call finish."
            result = self.tools.run_tests(" ".join(tests))
            if result.passed:
                return False, (
                    "The new test passes on the current code, so it does not reproduce the bug. "
                    "Make it assert the behaviour the report expects.\n\n" + tail(result.output, 3000)
                )
            if self._pytest and result.exit_code != 1:
                return False, (
                    f"The test run errored (exit code {result.exit_code}) instead of failing an assertion. "
                    "That usually means a syntax or import problem in the test. Fix the test.\n\n" + tail(result.output, 3000)
                )
            return True, "Reproduced: the new test fails on the current code. Handing over to the fix phase."

        user = self._brief(report) + "\n\nWrite the reproduction test now."
        ok = self._loop("super", prompts.REPRODUCE, user, READ_TOOLS + WRITE_TOOLS + ["run_tests", "web_search"], check)
        return new_tests() if ok else []

    def fix(self, report: RunReport, baseline_green: bool) -> bool:
        self.tools.write_policy = "source"
        self.red_streak = 0
        targets = " ".join(report.repro_tests)

        def check(_args: dict) -> tuple[bool, str]:
            suite = self.tools.run_tests()
            if suite.passed:
                return True, "Verified: the whole test suite passes."
            if not baseline_green and self.tools.run_tests(targets).passed:
                return True, "Verified: the reproduction tests pass (the suite already had failures before this change)."
            return False, "Not done: tests still fail.\n\n" + tail(suite.output, 4000)

        def after_step() -> str | None:
            if self.red_streak < self.settings.escalate_after or self.escalations >= 2:
                return None
            self.red_streak = 0
            return self.escalate(report)

        failing = tail(self.last_run.output, 3000) if self.last_run else ""
        user = (
            self._brief(report)
            + f"\n\nReproduction test(s), now locked: {', '.join(report.repro_tests)}"
            + f"\n\nLatest failing output:\n{failing}\n\nFix the code now."
        )
        return self._loop("super", prompts.FIX, user, READ_TOOLS + WRITE_TOOLS + ["run_tests", "web_search"], check, after_step)

    def escalate(self, report: RunReport) -> str:
        self.escalations += 1
        self.event("escalation", reason=f"{self.settings.escalate_after} failing test runs in a row; asking Nemotron Ultra")
        user = (
            f"Bug report:\n{self.issue}\n\nPlan:\n{json.dumps(report.plan, indent=1)}\n\n"
            f"Current diff:\n{self._diff()[:8000] or '(no changes yet)'}\n\n"
            f"Latest test output:\n{tail(self.last_run.output, 4000) if self.last_run else '(none)'}"
        )
        reply = self.llm.chat("ultra", [{"role": "system", "content": prompts.ESCALATE}, {"role": "user", "content": user}], purpose="escalation review")
        self.event("advice", text=reply.content)
        return f"Review from Nemotron Ultra (senior engineer):\n{reply.content}"

    def write_report(self, report: RunReport) -> str:
        outcome = "the full test suite passes" if report.ok else "the fix is incomplete: tests still fail"
        user = (
            f"Bug report:\n{self.issue}\n\nPlan:\n{json.dumps(report.plan, indent=1)}\n\n"
            f"Diff:\n{report.diff[:12000]}\n\nReproduction tests: {', '.join(report.repro_tests)}\nOutcome: {outcome}"
        )
        try:
            text = self.llm.chat("nano", [{"role": "system", "content": prompts.REPORT}, {"role": "user", "content": user}], purpose="PR description").content
        except Exception as exc:
            self.event("note", message=f"Report generation failed ({exc}); using a template.")
            text = ""
        text = re.sub(r"^```(?:markdown|md)?\s*\n|\n```\s*$", "", text.strip())
        if not text:
            text = (
                f"# Fix: {report.triage.get('summary', 'reported bug')}\n\n## Root cause\n{report.plan.get('root_cause')}\n\n"
                f"## Fix\n{report.plan.get('fix_plan')}\n\n## Tests\nAdded {', '.join(report.repro_tests)}; {outcome}."
            )
        if self.sources:
            refs = "\n".join(f"- [{s.get('title') or s['url']}]({s['url']})" for s in self.sources[:5])
            text += f"\n\n## References\n{refs}"
        self.event("report", markdown=text)
        return text

    # --- agent loop -----------------------------------------------------

    def _loop(
        self,
        tier: str,
        system: str,
        user: str,
        tool_names: list[str],
        on_finish: Callable[[dict], tuple[bool, str]],
        after_step: Callable[[], str | None] | None = None,
    ) -> bool:
        specs = self.tools.specs(tool_names + ["finish"])
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for step in range(1, self.settings.max_steps + 1):
            reply = self.llm.chat(tier, messages, tools=specs, purpose=f"{self.phase} step {step}")
            if reply.message.get("tool_calls") and not reply.message["content"]:
                reply.message["content"] = None
            messages.append(reply.message)
            if not reply.tool_calls:
                messages.append({"role": "user", "content": "Continue by calling one of the tools. Call finish when the goal is met."})
                continue
            for call in reply.tool_calls:
                try:
                    args = call.args()
                except ValueError as exc:
                    messages.append(self.llm.tool_result_message(call, f"ERROR: arguments were not valid JSON ({exc})"))
                    continue
                if call.name == "finish":
                    ok, message = on_finish(args)
                    self.event("finish_check", ok=ok, summary=str(args.get("summary", ""))[:2000], message=message[:3000])
                    messages.append(self.llm.tool_result_message(call, message))
                    if ok:
                        return True
                    continue
                self.event("tool_call", tool=call.name, args=preview(args))
                result = self.tools.execute(call.name, args)
                self.event("tool_result", tool=call.name, result=result[:4000])
                messages.append(self.llm.tool_result_message(call, result))
            if after_step and (advice := after_step()):
                messages.append({"role": "user", "content": advice})
        self.event("note", message=f"Gave up after {self.settings.max_steps} steps in the {self.phase} phase.")
        return False
