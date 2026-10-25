"""Settings for the ticker review pipeline, loaded from .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _path(value: str, root: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


@dataclass(frozen=True)
class ReviewSettings:
    project_root: Path
    portfolio_path: Path
    artifacts_dir: Path
    agents_dir: Path
    skill_path: Path
    max_news_items: int = 2
    crawler_sleep_ms: int = 1500
    safe_search: bool = True
    crawler_timeout_ms: int = 20000
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "gemma3:4b"
    llm_timeout_seconds: float = 90.0
    slack_webhook_url: str | None = None

    @classmethod
    def from_env(cls, root: Path | None = None) -> "ReviewSettings":
        root = root or PROJECT_ROOT
        cwd_env = Path.cwd() / ".env"
        load_dotenv(cwd_env if cwd_env.is_file() else root / ".env", override=False)

        portfolio = os.getenv("PORTFOLIO_PATH", "config/portfolio.json")
        portfolio_path = _path(portfolio, root)
        if not portfolio_path.is_file() and (root / "portfolio.json").is_file():
            portfolio_path = root / "portfolio.json"  # pre-config/ layout

        try:
            llm_timeout = max(1.0, float(os.getenv("LOCAL_LLM_TIMEOUT_SECONDS", "90")))
        except ValueError:
            llm_timeout = 90.0

        return cls(
            project_root=root,
            portfolio_path=portfolio_path,
            artifacts_dir=_path(os.getenv("REPORT_ARTIFACTS_DIR", "artifacts"), root),
            agents_dir=root / "agents",
            skill_path=root / "skills" / "ticker-strategy-analyzer" / "skill-description.md",
            max_news_items=_int("MAX_NEWS_ITEMS", 2, 1),
            crawler_sleep_ms=_int("CRAWLER_SLEEP_MS", 1500, 0),
            safe_search=_bool(os.getenv("SAFE_SEARCH"), True),
            crawler_timeout_ms=_int("CRAWLER_TIMEOUT_MS", 20000, 1000),
            llm_base_url=(os.getenv("LOCAL_LLM_BASE_URL") or "http://localhost:11434").strip().rstrip("/"),
            llm_model=(os.getenv("LOCAL_LLM_MODEL") or "gemma3:4b").strip(),
            llm_timeout_seconds=llm_timeout,
            slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL") or None,
        )

    def prompt(self, name: str) -> str:
        """Return the text of agents/<name>.prompt."""
        path = self.agents_dir / f"{name}.prompt"
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"Prompt file not found: {path}") from exc

    def skill(self) -> str:
        try:
            return self.skill_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ValueError(f"Skill file not found: {self.skill_path}") from exc
