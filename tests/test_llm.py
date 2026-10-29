import json

import pytest

from fixpoint.config import load_settings
from fixpoint.llm import LLM, extract_json, parse_text_tool_calls, strip_think
from tests.fakes import FakeClient, call, reply


@pytest.fixture
def settings(tmp_path):
    return load_settings(nebius_api_key="test", tavily_api_key="", sandbox="local", workdir=tmp_path)


def test_strip_think_removes_reasoning():
    assert strip_think("<think>hmm\nmaybe</think>\nAnswer") == "Answer"
    assert strip_think("reasoning without opening tag</think>Answer") == "Answer"
    assert strip_think(None) == ""


def test_extract_json_ignores_fences_and_prose():
    text = 'Sure! Here it is:\n```json\n{"a": 1, "b": {"c": [1, 2]}}\n```\nanything else?'
    assert extract_json(text) == {"a": 1, "b": {"c": [1, 2]}}
    assert extract_json("<think>{not json}</think> {\"ok\": true}") == {"ok": True}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_parse_text_tool_calls():
    text = 'Let me look.\n<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>'
    calls = parse_text_tool_calls(text, {"read_file"})
    assert [(c.name, c.args(), c.native) for c in calls] == [("read_file", {"path": "a.py"}, False)]
    assert parse_text_tool_calls('{"name": "rm_rf", "arguments": {}}', {"read_file"}) == []


def test_usage_and_cost_are_tracked_per_tier(settings):
    llm = LLM(settings, client=FakeClient(["hi", "there"]))
    llm.chat("super", [{"role": "user", "content": "x"}])
    llm.chat("ultra", [{"role": "user", "content": "x"}])
    assert llm.usage["super"] == {"calls": 1, "prompt_tokens": 100, "completion_tokens": 20}
    assert llm.usage["nano"]["calls"] == 0
    expected = 100 / 1e6 * 0.30 + 20 / 1e6 * 0.90 + 100 / 1e6 * 1.0 + 20 / 1e6 * 3.0
    assert llm.cost_usd() == pytest.approx(expected, abs=1e-5)


def test_native_tool_calls_become_assistant_message(settings):
    llm = LLM(settings, client=FakeClient([reply("", call("read_file", path="a.py"))]))
    tools = [{"type": "function", "function": {"name": "read_file", "parameters": {}}}]
    out = llm.chat("super", [{"role": "user", "content": "x"}], tools=tools)
    assert out.tool_calls[0].native
    assert out.message["tool_calls"][0]["function"]["name"] == "read_file"
    assert LLM.tool_result_message(out.tool_calls[0], "ok")["role"] == "tool"


def test_falls_back_to_text_tools_when_endpoint_rejects_them(settings):
    class BadRequest(Exception):
        status_code = 400

    client = FakeClient([BadRequest("tools are not supported for this model"), '<tool_call>{"name": "finish", "arguments": {"summary": "done"}}</tool_call>'])
    llm = LLM(settings, client=client)
    tools = [{"type": "function", "function": {"name": "finish", "parameters": {}}}]
    out = llm.chat("super", [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}], tools=tools)
    assert [c.name for c in out.tool_calls] == ["finish"]
    assert not out.tool_calls[0].native
    retry = client.requests[1]
    assert "tools" not in retry and "<tool_call>" in retry["messages"][0]["content"]
    assert LLM.tool_result_message(out.tool_calls[0], "ok")["role"] == "user"


def test_json_call_retries_once(settings):
    llm = LLM(settings, client=FakeClient(["I think the answer is...", json.dumps({"x": 1})]))
    assert llm.json_call("nano", "sys", "user", "test") == {"x": 1}
