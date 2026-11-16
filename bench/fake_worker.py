"""A stand-in analysis worker, so the benchmark harness can be tested before M2.

    uv run python -m bench.fake_worker --worker-id w1 --until-empty

It does everything the real worker's job loop does except run Stockfish:
claims `analyze-game` jobs with `FOR UPDATE SKIP LOCKED`, replays the PGN,
looks every position up through the real `EvalCache` (batch first, then a
re-check just before "computing" each miss), sleeps `--ms-per-eval` in place
of a search, writes the result with `put`, writes `moves`, and marks the game
analysed. Its numbers mean nothing about Stockfish; they exercise the harness,
the queue pattern and the cache under real concurrency.

Payload format: `{"game_id": <int>}`, matching the harness's default
`--job-payload`.
"""

from __future__ import annotations

import argparse
import io
import sys
import time

import chess
import chess.pgn
from sqlalchemy import Engine, text

from chess_coach.cache import build_eval_cache
from chess_coach.config import get_settings
from chess_coach.db import get_engine
from chess_coach.positions import EvalCache, Evaluation, fen_hash, normalize_fen

ENGINE_VERSION = "fake-1"
DEPTH = 1

_CLAIM = text(
    """
    UPDATE jobs SET status = 'running', locked_by = :worker, locked_at = now(),
                    attempts = attempts + 1
    WHERE id = (
        SELECT id FROM jobs
        WHERE status = 'pending' AND type = :type AND run_after <= now()
        ORDER BY priority, run_after, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, payload
    """
)


def fake_evaluation(board: chess.Board, key: bytes, ms: int) -> Evaluation:
    """Deterministic in the key, like a cleared-hash search should be."""
    if board.is_checkmate():
        return Evaluation(eval_cp=None, mate_in=0, best_move=None, compute_ms=ms)
    move = next(iter(board.legal_moves), None)
    return Evaluation(
        eval_cp=int.from_bytes(key[:2], "big") % 400 - 200,
        mate_in=None,
        best_move=move.uci() if move else None,
        nodes=1,
        compute_ms=ms,
    )


def analyse_game(engine: Engine, cache: EvalCache, game_id: int, ms_per_eval: int) -> None:
    with engine.connect() as conn:
        pgn, color = conn.execute(
            text("SELECT pgn, color FROM games WHERE id = :id"), {"id": game_id}
        ).one()
    game = chess.pgn.read_game(io.StringIO(pgn))
    board = game.board()
    plies: list[tuple[chess.Board, chess.Move]] = []
    for move in game.mainline_moves():
        plies.append((board.copy(stack=False), move))
        board.push(move)
    boards = [b for b, _ in plies] + [board.copy(stack=False)]
    keys = [fen_hash(b) for b in boards]

    # Batch prefetch where the cache supports it. NullCache (POSITION_CACHE=off,
    # the benchmark's "no cache" baseline) only has the EvalCache protocol.
    get_many = getattr(cache, "get_many", None)
    known = dict(get_many(keys, ENGINE_VERSION, DEPTH)) if get_many else {}
    for b, key in zip(boards, keys, strict=True):
        if key in known:
            continue
        hit = cache.get(key, ENGINE_VERSION, DEPTH)  # another worker may have just done it
        if hit is None:
            time.sleep(ms_per_eval / 1000)
            hit = fake_evaluation(b, key, ms_per_eval)
            cache.put(key, normalize_fen(b), ENGINE_VERSION, DEPTH, hit)
        known[key] = hit

    player_parity = 0 if color == "white" else 1
    rows = [
        {
            "game_id": game_id,
            "ply": i + 1,
            "fen_hash": keys[i],
            "san": b.san(move),
            "uci": move.uci(),
            "is_player_move": i % 2 == player_parity,
        }
        for i, (b, move) in enumerate(plies)
    ]
    with engine.begin() as conn:
        if rows:
            conn.execute(
                text(
                    "INSERT INTO moves (game_id, ply, fen_hash, san, uci, is_player_move) "
                    "VALUES (:game_id, :ply, :fen_hash, :san, :uci, :is_player_move) "
                    "ON CONFLICT (game_id, ply) DO NOTHING"
                ),
                rows,
            )
        conn.execute(
            text(
                "UPDATE games SET analysis_status = 'complete', analyzed_at = clock_timestamp() "
                "WHERE id = :id"
            ),
            {"id": game_id},
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.fake_worker")
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--until-empty", action="store_true")
    parser.add_argument("--ms-per-eval", type=int, default=5)
    parser.add_argument("--job-type", default="analyze-game")
    args = parser.parse_args(argv)

    engine = get_engine()
    cache = build_eval_cache(get_settings(), engine)
    while True:
        with engine.begin() as conn:
            job = conn.execute(_CLAIM, {"worker": args.worker_id, "type": args.job_type}).first()
        if job is None:
            if args.until_empty:
                return 0
            time.sleep(0.2)
            continue
        analyse_game(engine, cache, int(job.payload["game_id"]), args.ms_per_eval)
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE jobs SET status = 'done', finished_at = now() WHERE id = :id"),
                {"id": job.id},
            )


if __name__ == "__main__":
    sys.exit(main())
