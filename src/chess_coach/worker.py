"""`chess-coach-worker`: one worker process - analysis (one Stockfish) or sync.

    uv run chess-coach-worker                       # analysis, until stopped
    uv run chess-coach-worker --until-empty         # drain the queue, then exit
    uv run chess-coach-worker --worker-id w3 --max-jobs 50
    uv run chess-coach-worker --kind sync           # sync-player jobs (Chess.com)
    python -m chess_coach.worker ...

Scale by starting more of these, not by giving one more threads: each owns
one single-threaded engine, which is what keeps evaluations reproducible.
Ctrl+C (or SIGTERM) finishes the current game, then exits; a second one exits
immediately, and the unfinished job is recovered by the reaper.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading

from chess_coach.analysis.jobs import enqueue_pending
from chess_coach.analysis.runner import Worker
from chess_coach.config import get_settings
from chess_coach.db import get_engine
from chess_coach.engine import StockfishEngine
from chess_coach.logging import get_logger, setup_logging


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="chess-coach-worker", description=__doc__.split("\n")[0])
    parser.add_argument(
        "--kind",
        choices=("analysis", "sync"),
        default="analysis",
        help="analysis: analyze-game jobs with Stockfish; sync: sync-player jobs",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="name recorded in jobs.locked_by (default: host:pid); at most 64 characters",
    )
    parser.add_argument(
        "--max-jobs", type=int, default=None, help="exit after this many jobs (failures count)"
    )
    parser.add_argument(
        "--until-empty",
        action="store_true",
        help="exit once no analysis job is pending or running",
    )
    parser.add_argument(
        "--enqueue-pending",
        action="store_true",
        help="first enqueue every pending game that has no job yet",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, json_output=settings.env != "dev")
    log = get_logger("chess_coach.worker")
    worker_id = args.worker_id or default_worker_id()

    db = get_engine()
    if args.enqueue_pending:
        enqueue_pending(db)

    stop = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        if stop.is_set():
            raise KeyboardInterrupt
        log.info("worker.stopping", signal=signum, detail="finishing the current job")
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    if args.kind == "sync":
        return _run_sync(args, db, worker_id, stop, log)

    try:
        engine = StockfishEngine(
            settings.stockfish_path,
            depth=settings.engine_depth,
            threads=settings.engine_threads,
            hash_mb=settings.engine_hash_mb,
            timeout_sec=settings.engine_timeout_sec,
        )
    except FileNotFoundError:
        log.error(
            "worker.no_engine",
            path=str(settings.stockfish_path),
            detail="run: uv run python tools/fetch_stockfish.py",
        )
        return 2

    with engine:
        worker = Worker(db, engine, worker_id=worker_id, settings=settings)
        try:
            worker.run(max_jobs=args.max_jobs, until_empty=args.until_empty, stop=stop)
        except KeyboardInterrupt:
            log.warning("worker.interrupted", worker_id=worker_id)
            return 130
    return 0


def _run_sync(args, db, worker_id: str, stop: threading.Event, log) -> int:
    # Imported here: an analysis worker needs neither Redis nor the client.
    from chess_coach.ingest.cli import build_client
    from chess_coach.ingest.jobs import SyncWorker

    with build_client() as client:
        worker = SyncWorker(db, client, worker_id=worker_id, settings=get_settings())
        try:
            worker.run(max_jobs=args.max_jobs, until_empty=args.until_empty, stop=stop)
        except KeyboardInterrupt:
            log.warning("worker.interrupted", worker_id=worker_id)
            return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
