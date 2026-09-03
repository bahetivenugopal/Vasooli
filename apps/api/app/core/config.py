"""Application configuration, loaded from the repo-root `.env`.

Every setting has a safe default so the app boots without credentials — the
health check must work on a fresh clone. Anything requiring a real key fails at
the point of use, with a clear message, rather than at import time.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/app/core/config.py -> parents[2] = apps/api, parents[4] = repo root
API_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    """Runtime configuration. Field names map to UPPER_SNAKE env vars."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---------------------------------------------------------------
    app_name: str = "Vasooli API"
    version: str = "0.1.0"
    environment: str = "development"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # Comma-separated in .env; use `cors_origin_list` to consume.
    cors_origins: str = "http://localhost:3000"

    # --- Razorpay (TEST MODE ONLY) -----------------------------------------
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # --- Anthropic ---------------------------------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # --- Database ----------------------------------------------------------
    database_url: str = "sqlite:///./vasooli.db"

    # --- Batch demo --------------------------------------------------------
    demo_seed: int = 42

    @field_validator("razorpay_key_id")
    @classmethod
    def _reject_live_keys(cls, v: str) -> str:
        """Test-mode keys only, always.

        A live key in this project is a defect, not a configuration choice — so
        refuse to start rather than let one reach a real API call.
        """
        if v.startswith("rzp_live_"):
            raise ValueError(
                "A live Razorpay key was supplied. Vasooli is test-mode only: "
                "use a key starting with 'rzp_test_'. Rotate the live key now — "
                "it has been exposed to this process."
            )
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_database_url(self) -> str:
        """Anchor a relative SQLite path to `apps/api/`.

        Without this the database file lands wherever the process happens to be
        started from, which quietly produces two different databases.
        """
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            raw = self.database_url[len(prefix) :]
            if not raw.startswith("/") and ":" not in raw[:3]:
                return f"{prefix}{(API_DIR / raw).resolve()}"
        return self.database_url


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


settings = get_settings()
