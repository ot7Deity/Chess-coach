"""Process configuration, read once from the environment.

Every process (API, ingest worker, analysis worker) loads the same settings
object so there is exactly one place where a knob is named and defaulted.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chess_coach import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database / cache ---------------------------------------------------
    database_url: str = "postgresql+psycopg://chess:chess@localhost:5432/chess_coach"
    redis_url: str = "redis://localhost:6379/0"

    # --- Chess.com client ---------------------------------------------------
    chesscom_contact: str = "you@example.com"
    chesscom_base_url: str = "https://api.chess.com/pub"
    chesscom_rate_per_sec: float = 1.0
    chesscom_burst: int = 3
    chesscom_timeout_sec: float = 30.0
    # Retries after the first attempt, for 429s, 5xx and transport errors.
    chesscom_max_retries: int = Field(default=5, ge=0)
    chesscom_backoff_base_sec: float = Field(default=1.0, gt=0)
    chesscom_backoff_cap_sec: float = Field(default=60.0, gt=0)

    # --- Engine -------------------------------------------------------------
    stockfish_path: Path = Path("vendor/stockfish/stockfish.exe")
    engine_depth: int = Field(default=16, ge=1, le=30)
    engine_threads: int = Field(default=1, ge=1)
    engine_hash_mb: int = Field(default=128, ge=1)
    engine_timeout_sec: float = 30.0

    # --- Position cache / metrics -------------------------------------------
    # Which layers the analysis worker's EvalCache uses: 'full' is Redis in
    # front of Postgres, 'postgres' skips Redis, 'off' is NullCache (the
    # "without cache" side of the engine-seconds-saved benchmark).
    position_cache: str = Field(default="full", pattern="^(full|postgres|off)$")
    # Bump the version segment to orphan every Redis entry at once (Postgres is
    # unaffected; Redis refills from it).
    position_cache_key_prefix: str = "chess_coach:pos:v1:"
    # Sliding expiry, refreshed on every hit. 0 disables expiry.
    position_cache_ttl_sec: int = Field(default=7 * 24 * 3600, ge=0)

    # --- Workers ------------------------------------------------------------
    worker_poll_interval_sec: float = 1.0
    job_max_attempts: int = Field(default=5, ge=1)
    job_lock_timeout_sec: int = Field(default=300, ge=1)

    # --- Misc ---------------------------------------------------------------
    log_level: str = "INFO"
    env: str = "dev"

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def user_agent(self) -> str:
        """Chess.com asks clients to identify themselves with contact info.

        Sending a contactable User-Agent is the difference between being
        throttled politely and being blocked without explanation.
        """
        return f"ChessCoach/{__version__} ({self.chesscom_contact})"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor. Tests clear the cache via `get_settings.cache_clear()`."""
    return Settings()
