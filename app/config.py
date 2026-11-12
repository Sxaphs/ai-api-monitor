from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    rapidapi_key: str | None
    scheduler_enabled: bool
    log_level: str
    request_timeout: float = 30.0
    retry_attempts: int = 3
    retry_backoff: float = 1.0
    rapidapi_host: str = "twitter-api45.p.rapidapi.com"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"
    llm_api_key: str | None = None
    llm_max_chars: int = 60000

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = f"sqlite:///{(BASE_DIR / 'data' / 'monitor.db').as_posix()}"
        return cls(
            database_url=os.getenv("DATABASE_URL", default_db),
            rapidapi_key=os.getenv("RAPIDAPI_KEY") or None,
            scheduler_enabled=_as_bool(os.getenv("SCHEDULER_ENABLED"), True),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "deepseek-chat"),
            llm_api_key=os.getenv("LLM_API_KEY") or None,
            llm_max_chars=int(os.getenv("LLM_MAX_CHARS", "60000")),
        )


settings = Settings.from_env()

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
)
