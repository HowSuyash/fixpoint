"""End-to-end runs of the pipeline on the scheduler example, with a scripted model."""

import json

import pytest

from fixpoint import workspace
from fixpoint.agent import Fixpoint
from fixpoint.config import load_settings
from tests.fakes import FakeClient, call, reply

ISSUE = (workspace.examples_dir() / "scheduler" / "ISSUE.md").read_text(encoding="utf-8")

TRIAGE = "<think>The traceback points at add_months.</think>" + json.dumps(
    {
        "summary": "add_months crashes when the start day does not exist in the target month",
        "bug_type": "crash",
        "keywords": ["add_months"],
        "suspect_files": ["scheduler/dates.py", "made/up.py"],
        "web_query": "",
    }
)
PLAN = json.dumps(
    {
        "root_cause": "date.replace keeps day 31, which does not exist in February.",
        "expected_behavior": "Clamp to the last day of the target month.",
        "test_plan": "tests/test_month_end.py: assert charge dates for a Jan 31 start.",
        "fix_plan": "Clamp the day with calendar.monthrange in add_months.",
        "files_to_change": ["scheduler/dates.py"],
    }
)
REPRO_TEST = """from datetime import date

from scheduler import Subscription


def test_month_end_start_is_clamped():
    sub = Subscription("acme", date(2024, 1, 31), months=3)
    assert sub.charge_dates() == [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]
"""
FIX_IMPORT = call("replace_in_file", path="scheduler/dates.py", old="from datetime import date, timedelta", new="import calendar\nfrom datetime import date, timedelta")
FIX_BODY = call(
    "replace_in_file",
    path="scheduler/dates.py",
    old="    return d.replace(year=year, month=month)",
    new="    day = min(d.day, calendar.monthrange(year, month)[1])\n    return d.replace(year=year, month=month, day=day)",
)


def reproduce_script():
    return [
        reply("", call("read_file", path="scheduler/dates.py")),
        reply("", call("write_file", path="tests/test_month_end.py", content=REPRO_TEST)),
        reply("", call("run_tests", target="tests/test_month_end.py")),
        reply("", call("finish", summary="The test fails with ValueError, as in the report.")),
    ]


@pytest.fixture
def work(tmp_path):
    return workspace.prepare("scheduler", tmp_path)


def make(work, script, **overrides):
    settings = load_settings(nebius_api_key="test", tavily_api_key="", sandbox="local", **overrides)
    events = []
    client = FakeClient(script)
    agent = Fixpoint(settings, work, ISSUE, emit=events.append, client=client)
    return agent, client, events


def test_happy_path_goes_red_then_green(work):
    script = [TRIAGE, PLAN, *reproduce_script()]
    script += [
        # The fix agent first tries to weaken the test; the lock must refuse it.
        reply("", call("write_file", path="tests/test_month_end.py", content="def test_month_end_start_is_clamped():\n    pass\n")),
        reply("Fixing add_months.", FIX_IMPORT, FIX_BODY),
        reply("", call("run_tests")),
        reply("", call("finish", summary="Clamp the day to the length of the target month.")),
        "```markdown\n# Clamp month-end dates in add_months\n\n## Root cause\n...\n```",
    ]
    agent, client, events = make(work, script)
    report = agent.run()

    assert report.status == "fixed", report.error
    assert report.repro_tests == ["tests/test_month_end.py"]
    assert "calendar.monthrange" in report.diff and "tests/test_month_end.py" in report.diff
    assert report.report_md.startswith("# Clamp month-end dates")

    tiers = [e["tier"] for e in events if e["type"] == "llm"]
    assert tiers[0] == "nano" and tiers[1] == "ultra" and tiers[-1] == "nano"
    assert set(tiers[2:-1]) == {"super"}

    locked = [e for e in events if e["type"] == "tool_result" and e["phase"] == "fix" and e["tool"] == "write_file"]
    assert "locked" in locked[0]["result"]
    assert "assert sub.charge_dates()" in (work / "tests/test_month_end.py").read_text()

    runs = [e for e in events if e["type"] == "test_run"]
    assert runs[0]["phase"] == "baseline" and runs[0]["passed"]
    assert any(r["phase"] == "reproduce" and not r["passed"] for r in runs)
    assert runs[-1]["phase"] == "fix" and runs[-1]["passed"]

    plan_event = next(e for e in events if e["type"] == "plan")
    assert plan_event["files"][0] == "scheduler/recurring.py"  # from the traceback
    assert "made/up.py" not in plan_event["files"]
    assert events[-1]["type"] == "done" and events[-1]["report"]["status"] == "fixed"
    assert report.cost_usd is not None and report.cost_usd > 0


def test_a_test_that_passes_is_not_accepted_as_a_reproduction(work):
    passing = REPRO_TEST.replace("date(2024, 1, 31), months=3", "date(2024, 1, 10), months=1").replace(
        "[date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31)]", "[date(2024, 1, 10)]"
    )
    script = [TRIAGE, PLAN]
    script += [
        reply("", call("write_file", path="tests/test_month_end.py", content=passing)),
        reply("", call("finish", summary="done")),
    ]
    agent, client, events = make(work, script, max_steps=2)
    report = agent.run()
    assert report.status == "not_reproduced"
    check = next(e for e in events if e["type"] == "finish_check")
    assert not check["ok"] and "does not reproduce" in check["message"]


def test_stalled_fix_escalates_to_ultra(work):
    script = [TRIAGE, PLAN, *reproduce_script()]
    script += [
        reply("", call("run_tests")),
        reply("", call("run_tests")),
        "Clamp the day: use calendar.monthrange(year, month)[1].",  # Ultra's review
        reply("", FIX_IMPORT, FIX_BODY),
        reply("", call("finish", summary="done")),
        "# Fix\n",
    ]
    agent, client, events = make(work, script, escalate_after=2)
    report = agent.run()
    assert report.status == "fixed", report.error
    assert report.escalations == 1
    assert [e for e in events if e["type"] == "advice"][0]["text"].startswith("Clamp the day")
    fix_prompt = client.requests[-2]["messages"]
    assert any("Review from Nemotron Ultra" in str(m.get("content")) for m in fix_prompt)


class FakeTavily:
    def __init__(self):
        self.queries = []

    def search(self, query, **_kwargs):
        self.queries.append(query)
        return {
            "answer": "calendar.monthrange(year, month)[1] is the number of days in the month.",
            "results": [{"title": "calendar - Python docs", "url": "https://docs.python.org/3/library/calendar.html", "content": "monthrange(year, month)"}],
        }


def test_tavily_research_feeds_the_plan_and_the_pr(work):
    script = [TRIAGE, PLAN, *reproduce_script(), reply("", FIX_IMPORT, FIX_BODY), reply("", call("finish", summary="done")), "# Fix\n\nBody."]
    settings = load_settings(nebius_api_key="test", tavily_api_key="", sandbox="local")
    tavily, client = FakeTavily(), FakeClient(script)
    report = Fixpoint(settings, work, ISSUE, client=client, tavily_client=tavily).run()

    assert report.status == "fixed", report.error
    assert tavily.queries == [json.loads(TRIAGE.split("</think>")[1])["summary"]]  # empty web_query -> search the symptom
    plan_prompt = client.requests[1]["messages"][1]["content"]
    assert "Web research (Tavily)" in plan_prompt and "monthrange" in plan_prompt
    assert report.sources == [{"title": "calendar - Python docs", "url": "https://docs.python.org/3/library/calendar.html"}]
    assert report.report_md.endswith("## References\n- [calendar - Python docs](https://docs.python.org/3/library/calendar.html)")


def test_errors_are_reported_not_raised(work):
    agent, client, events = make(work, [TRIAGE])  # the script runs out at the planning step
    report = agent.run()
    assert report.status == "error" and "script exhausted" in report.error
    assert events[-1]["type"] == "done"
