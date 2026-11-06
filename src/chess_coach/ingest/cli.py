"""`chess-coach-sync <username> [<username> ...]`: load or refresh players' histories.

    uv run chess-coach-sync deitythemenace

Run it again and it fetches only what can have changed: the archive list and
the current month, which is a 0-byte 304 if no games were played since.
"""

from __future__ import annotations

import argparse
import sys

import redis

from chess_coach.chesscom import ChessComClient, ChessComError
from chess_coach.config import get_settings
from chess_coach.db import get_engine
from chess_coach.ingest.sync import SyncInProgress, sync_player
from chess_coach.logging import get_logger, setup_logging
from chess_coach.ratelimit import RedisTokenBucket


def build_client() -> ChessComClient:
    settings = get_settings()
    limiter = RedisTokenBucket(
        redis.Redis.from_url(settings.redis_url),
        rate_per_sec=settings.chesscom_rate_per_sec,
        burst=settings.chesscom_burst,
    )
    return ChessComClient(
        user_agent=settings.user_agent,
        limiter=limiter,
        base_url=settings.chesscom_base_url,
        timeout_sec=settings.chesscom_timeout_sec,
        max_retries=settings.chesscom_max_retries,
        backoff_base_sec=settings.chesscom_backoff_base_sec,
        backoff_cap_sec=settings.chesscom_backoff_cap_sec,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chess-coach-sync", description=__doc__.split("\n")[0])
    parser.add_argument("usernames", nargs="+", metavar="username")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.env != "dev")
    log = get_logger("chess_coach.ingest")

    failures = 0
    engine = get_engine()
    with build_client() as client:
        for username in args.usernames:
            try:
                sync_player(engine, client, username)
            except (ChessComError, SyncInProgress, ValueError) as exc:
                failures += 1
                log.error("sync.failed", username=username, error=f"{type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
