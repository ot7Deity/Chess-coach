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
    # 1 is not just a default. With more than one search thread Stockfish is
    # non-deterministic - measured: the same position at depth 16 came back
    # -186 and -213 on two runs with Threads=2 - and a non-deterministic value
    # cannot be cached under (position, engine, depth). Scale with workers.
    # Enforced: the only accepted value is 1.
    engine_threads: int = Field(default=1, ge=1, le=1)
    # Part of the engine version the cache is keyed on ('sf_18-h128'): the
    # hash size changes results slightly, so changing it starts a new cache.
    engine_hash_mb: int = Field(default=128, ge=1)
    # Per position. A search that runs past this is treated as a hung engine:
    # the process is killed and restarted, and the job is retried.
    engine_timeout_sec: float = Field(default=30.0, gt=0)

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
    # Redis answers in well under a millisecond; anything near this is an outage.
    position_cache_redis_timeout_sec: float = Field(default=0.25, gt=0)
    # After a Redis failure, go Postgres-only for this long before retrying.
    position_cache_redis_retry_sec: float = Field(default=30.0, ge=0)
    # Prometheus endpoint. 0 disables it. Several workers on one host each take
    # metrics_port + their worker index. Default 9100 avoids clash with API port 8000.
    metrics_port: int = Field(default=9100, ge=0, le=65535)
    metrics_addr: str = "127.0.0.1"

    # --- Workers ------------------------------------------------------------
    worker_poll_interval_sec: float = Field(default=1.0, gt=0)
    job_max_attempts: int = Field(default=5, ge=1)
    # A running job whose worker has not heartbeated for this long is presumed
    # dead and handed back to the queue. Workers heartbeat every third of it,
    # so a live worker on a long game is never reaped.
    job_lock_timeout_sec: float = Field(default=300, gt=0)
    # Retry backoff after a failed attempt: base * 2^(attempt-1), capped, with
    # equal jitter. Pushed into jobs.run_after.
    job_retry_base_sec: float = Field(default=30.0, gt=0)
    job_retry_cap_sec: float = Field(default=1800.0, gt=0)

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
