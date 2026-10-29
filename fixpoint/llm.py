"""Client for Nemotron models on Nebius Token Factory (OpenAI-compatible API).

Routes each call to a model tier (nano / super / ultra), counts tokens per tier,
and copes with the ways reasoning models shape their output: <think> blocks,
a separate reasoning_content field, and tool calls written as text.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from .config import Settings

THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S)


def strip_think(text: str | None) -> str:
    if not text:
        return ""
    text = THINK_RE.sub("", text)
    if "</think>" in text:  # the chat template already opened the think block
        text = text.split("</think>", 1)[1]
    return text.strip()


def extract_json(text: str) -> dict:
    """Return the first JSON object in text, ignoring prose and code fences around it."""
    text = strip_think(text)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object in model output")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, as the model wrote it
    native: bool = True  # False when parsed out of the message text

    def args(self) -> dict:
        value = json.loads(self.arguments or "{}")
        if not isinstance(value, dict):
            raise ValueError("arguments must be a JSON object")
        return value


def parse_text_tool_calls(text: str, known: set[str]) -> list[ToolCall]:
    """Recover tool calls a model wrote as text (<tool_call>{...}</tool_call> or bare JSON)."""
    payloads = TOOL_CALL_RE.findall(text)
    objects: list[dict] = []
    if payloads:
        for payload in payloads:
            try:
                objects.append(extract_json(payload))
            except ValueError:
                continue
    else:
        try:
            objects.append(extract_json(text))
        except ValueError:
            return []
    calls = []
    for obj in objects:
        name = obj.get("name") or obj.get("tool")
        if name not in known:
            continue
        args = obj.get("arguments", obj.get("args", obj.get("parameters", {})))
        if not isinstance(args, str):
            args = json.dumps(args)
        calls.append(ToolCall(f"call_{uuid.uuid4().hex[:12]}", name, args, native=False))
    return calls


def _text_tool_protocol(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Describe the tools in the system prompt, for endpoints that reject the tools parameter."""
    specs = json.dumps([t["function"] for t in tools], indent=1)
    note = (
        "\n\nYou can call tools. To call one, reply with exactly:\n"
        '<tool_call>{"name": "<tool name>", "arguments": {...}}</tool_call>\n'
        f"Available tools:\n{specs}"
    )
    first, rest = messages[0], messages[1:]
    if first.get("role") == "system":
        return [{**first, "content": first["content"] + note}, *rest]
    return [{"role": "system", "content": note.strip()}, *messages]


@dataclass
class Reply:
    content: str
    reasoning: str
    tool_calls: list[ToolCall]
    message: dict  # the assistant message to append to the conversation


class LLM:
    def __init__(self, settings: Settings, client: Any = None, on_event: Callable[[dict], None] | None = None):
        if client is None:
            if not settings.nebius_api_key:
                raise RuntimeError("NEBIUS_API_KEY is not set. Create a key in the Nebius Token Factory console and put it in .env")
            from openai import OpenAI

            client = OpenAI(base_url=settings.base_url, api_key=settings.nebius_api_key, max_retries=3, timeout=300)
        self.client = client
        self.settings = settings
        self.on_event = on_event or (lambda event: None)
        self.usage = {name: {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0} for name in settings.tiers}
        self._native_tools = True
        self._lock = threading.Lock()

    def chat(
        self,
        tier: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        purpose: str = "",
        temperature: float = 0.3,
    ) -> Reply:
        model = self.settings.tiers[tier]
        kwargs: dict[str, Any] = dict(model=model.model, max_tokens=model.max_tokens, temperature=temperature)
        if tools and self._native_tools:
            kwargs.update(messages=messages, tools=tools, tool_choice="auto")
        elif tools:
            kwargs.update(messages=_text_tool_protocol(messages, tools))
        else:
            kwargs.update(messages=messages)

        started = time.monotonic()
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if tools and self._native_tools and getattr(exc, "status_code", None) == 400 and "tool" in str(exc).lower():
                self._native_tools = False
                return self.chat(tier, messages, tools, purpose, temperature)
            raise
        seconds = time.monotonic() - started

        msg = response.choices[0].message
        raw = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        if not reasoning:
            reasoning = "\n".join(m.strip() for m in THINK_RE.findall(raw))
        content = strip_think(raw)

        calls = [
            ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}")
            for tc in (getattr(msg, "tool_calls", None) or [])
        ]
        if tools and not calls:
            calls = parse_text_tool_calls(content, {t["function"]["name"] for t in tools})

        message: dict[str, Any] = {"role": "assistant", "content": content}
        native = [c for c in calls if c.native]
        if native:
            message["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in native
            ]

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        with self._lock:
            stats = self.usage[tier]
            stats["calls"] += 1
            stats["prompt_tokens"] += prompt_tokens
            stats["completion_tokens"] += completion_tokens

        self.on_event(
            {
                "type": "llm",
                "tier": tier,
                "model": model.model,
                "purpose": purpose,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "seconds": round(seconds, 2),
                "reasoning": reasoning[:2000],
                "content": content[:2000],
                "tool_calls": [c.name for c in calls],
                "usage": self.usage_summary(),
            }
        )
        return Reply(content, reasoning, calls, message)

    def json_call(self, tier: str, system: str, user: str, purpose: str) -> dict:
        """Ask for a JSON object; retry once with a reminder if the reply does not parse."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        reply = self.chat(tier, messages, purpose=purpose)
        try:
            return extract_json(reply.content)
        except ValueError:
            messages += [reply.message, {"role": "user", "content": "Reply with only the JSON object, nothing else."}]
            return extract_json(self.chat(tier, messages, purpose=f"{purpose} (retry)").content)

    @staticmethod
    def tool_result_message(call: ToolCall, content: str) -> dict:
        if call.native:
            return {"role": "tool", "tool_call_id": call.id, "content": content}
        return {"role": "user", "content": f'<tool_response name="{call.name}">\n{content}\n</tool_response>'}

    def cost_usd(self) -> float | None:
        total, priced = 0.0, False
        for name, stats in self.usage.items():
            price = self.settings.price(self.settings.tiers[name].model)
            if price is None or not stats["calls"]:
                continue
            priced = True
            total += stats["prompt_tokens"] / 1e6 * price[0] + stats["completion_tokens"] / 1e6 * price[1]
        return round(total, 5) if priced else None

    def usage_summary(self) -> dict:
        return {
            name: {**stats, "model": self.settings.tiers[name].model, "priced": self.settings.price(self.settings.tiers[name].model) is not None}
            for name, stats in self.usage.items()
        }
