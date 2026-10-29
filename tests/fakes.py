"""A scripted stand-in for the Token Factory client, so the pipeline can be tested offline."""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace as NS

_ids = itertools.count(1)


def call(name: str, **args) -> NS:
    return NS(id=f"call_{next(_ids)}", type="function", function=NS(name=name, arguments=json.dumps(args)))


def reply(content: str = "", *calls: NS) -> tuple[str, list[NS]]:
    return content, list(calls)


class FakeClient:
    """Mimics OpenAI().chat.completions.create; pops one scripted reply per request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.script:
            raise AssertionError(f"script exhausted; last request: {kwargs['messages'][-1]}")
        item = self.script.pop(0)
        if callable(item):
            item = item(kwargs)
        if isinstance(item, Exception):
            raise item
        content, calls = item if isinstance(item, tuple) else (item, [])
        message = NS(content=content, tool_calls=calls or None, reasoning_content=None)
        return NS(choices=[NS(message=message)], usage=NS(prompt_tokens=100, completion_tokens=20))

    def models_used(self) -> list[str]:
        return [r["model"] for r in self.requests]
