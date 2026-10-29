"""Runtime settings, read from environment variables (and a local .env file)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"

# USD per 1M tokens (input, output) as listed for Nebius Token Factory, Oct 2026.
# None means the price is unknown: tokens are still counted, cost is not.
DEFAULT_PRICES: dict[str, tuple[float, float] | None] = {
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": None,
    "nvidia/nemotron-3-super-120b-a12b": (0.30, 0.90),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1.00, 3.00),
}


@dataclass(frozen=True)
class Tier:
    """One model tier. Nano does the quick calls, Super runs the agent loops, Ultra reasons."""

    name: str
    model: str
    max_tokens: int


@dataclass(frozen=True)
class Settings:
    nebius_api_key: str
    base_url: str
    tavily_api_key: str
    tiers: dict[str, Tier]
    test_command: str
    sandbox: str
    sandbox_image: str
    test_timeout: int
    max_steps: int
    escalate_after: int
    workdir: Path
    allow_git_urls: bool
    max_concurrent_runs: int
    public_demo: bool
    runs_per_ip_per_day: int
    max_runs_per_day: int

    def price(self, model: str) -> tuple[float, float] | None:
        return DEFAULT_PRICES.get(model)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _flag(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_settings(**overrides) -> Settings:
    tiers = {
        "nano": Tier("nano", _env("FIXPOINT_NANO_MODEL", "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"), 4096),
        "super": Tier("super", _env("FIXPOINT_SUPER_MODEL", "nvidia/nemotron-3-super-120b-a12b"), 8192),
        "ultra": Tier("ultra", _env("FIXPOINT_ULTRA_MODEL", "nvidia/Nemotron-3-Ultra-550b-a55b"), 16384),
    }
    # A Vercel deployment is always public: default to demo mode with run limits (env vars still override).
    on_vercel = os.environ.get("VERCEL") == "1"
    values = dict(
        nebius_api_key=_env("NEBIUS_API_KEY"),
        base_url=_env("NEBIUS_BASE_URL", DEFAULT_BASE_URL),
        tavily_api_key=_env("TAVILY_API_KEY"),
        tiers=tiers,
        test_command=_env("FIXPOINT_TEST_COMMAND", "python -m pytest -q -p no:cacheprovider"),
        sandbox=_env("FIXPOINT_SANDBOX", "local" if on_vercel else "auto"),
        sandbox_image=_env("FIXPOINT_SANDBOX_IMAGE", "fixpoint-sandbox"),
        test_timeout=int(_env("FIXPOINT_TEST_TIMEOUT", "120")),
        max_steps=int(_env("FIXPOINT_MAX_STEPS", "24")),
        escalate_after=int(_env("FIXPOINT_ESCALATE_AFTER", "3")),
        workdir=Path(_env("FIXPOINT_WORKDIR", str(Path(tempfile.gettempdir()) / "fixpoint"))),
        allow_git_urls=_flag("FIXPOINT_ALLOW_GIT_URLS", False),
        max_concurrent_runs=int(_env("FIXPOINT_MAX_CONCURRENT_RUNS", "2")),
        # Public demo: bundled examples with their own bug reports only, so visitors cannot steer the agent.
        public_demo=_flag("FIXPOINT_PUBLIC_DEMO", on_vercel),
        runs_per_ip_per_day=int(_env("FIXPOINT_RUNS_PER_IP_PER_DAY", "5" if on_vercel else "0")),  # 0 = unlimited
        max_runs_per_day=int(_env("FIXPOINT_MAX_RUNS_PER_DAY", "100" if on_vercel else "0")),  # 0 = unlimited
    )
    values.update(overrides)
    return Settings(**values)
