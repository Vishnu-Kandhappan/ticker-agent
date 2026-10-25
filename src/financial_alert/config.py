"""Configuration loading and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class Portfolio:
    assets: list[str]
    watch_list: list[str]
    manual_updates: str
    precious_metals: list[str] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return list(dict.fromkeys([*self.assets, *self.watch_list, *self.precious_metals]))


@dataclass(frozen=True)
class Settings:
    portfolio_path: Path
    artifacts_dir: Path
    news_results_per_asset: int
    slack_webhook_url: str | None
    news_min_request_interval_seconds: float = 2.0
    news_max_retries: int = 3
    local_llm_base_url: str = "http://localhost:11434"
    local_llm_model: str = "gemma3:4b"
    local_llm_timeout_seconds: float = 90.0

    @classmethod
    def from_env(cls, portfolio_path: str | None = None) -> "Settings":
        # Prefer a .env in the current project directory; fall back to the
        # source project root when the command is launched from elsewhere.
        project_root = Path(__file__).resolve().parents[2]
        cwd_env = Path.cwd() / ".env"
        project_env = project_root / ".env"
        env_path = cwd_env if cwd_env.is_file() else project_env
        load_dotenv(env_path, override=False)

        return cls(
            portfolio_path=Path(portfolio_path or os.getenv("PORTFOLIO_PATH", "config/portfolio.json")),
            artifacts_dir=Path(os.getenv("REPORT_ARTIFACTS_DIR", "artifacts")),
            news_results_per_asset=max(1, int(os.getenv("NEWS_RESULTS_PER_ASSET", "3"))),
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
            news_min_request_interval_seconds=max(
                2.0, float(os.getenv("NEWS_MIN_REQUEST_INTERVAL_SECONDS", "2"))
            ),
            news_max_retries=max(1, int(os.getenv("NEWS_MAX_RETRIES", "3"))),
            local_llm_base_url=(
                os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434").strip().rstrip("/")
                or "http://localhost:11434"
            ),
            local_llm_model=os.getenv("LOCAL_LLM_MODEL", "gemma3:4b").strip() or "gemma3:4b",
            local_llm_timeout_seconds=max(1.0, float(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "90"))),
        )


def load_portfolio(path: Path) -> Portfolio:
    """Read the portfolio JSON and fail with actionable validation errors."""
    try:
        with path.open("r", encoding="utf-8") as portfolio_file:
            data: Any = json.load(portfolio_file)
    except FileNotFoundError as exc:
        raise ValueError(f"Portfolio file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Portfolio file contains invalid JSON: {path} ({exc})") from exc

    if not isinstance(data, dict):
        raise ValueError("Portfolio JSON must contain an object at the top level.")

    # `asset` is the current portfolio key. Accept `assets` temporarily so an
    # older portfolio file remains usable while users migrate to the new schema.
    assets = data.get("asset", data.get("assets", []))
    if not isinstance(assets, list) or any(not isinstance(asset, str) for asset in assets):
        raise ValueError("Portfolio 'asset' must be a list of strings.")

    watch_list = data.get("watch_list", [])
    if not isinstance(watch_list, list) or any(not isinstance(item, str) for item in watch_list):
        raise ValueError("Portfolio 'watch_list' must be a list of ticker strings.")

    precious_metals = data.get("precious-metals", [])
    if not isinstance(precious_metals, list) or any(not isinstance(item, str) for item in precious_metals):
        raise ValueError("Portfolio 'precious-metals' must be a list of ticker strings.")

    manual_updates = data.get("manual_updates", "No manual updates today.")
    if not isinstance(manual_updates, str):
        raise ValueError("Portfolio 'manual_updates' must be a string.")

    return Portfolio(
        assets=assets,
        watch_list=watch_list,
        precious_metals=precious_metals,
        manual_updates=manual_updates,
    )
