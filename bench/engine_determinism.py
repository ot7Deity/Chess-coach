"""Is a fixed-depth Stockfish evaluation a pure function of the position?

    uv run python -m bench.engine_determinism --depth 14

Evaluates the same positions three ways: twice with the hash table cleared
before every search ("cold"), and once after searching an unrelated position
first ("warm", as a worker's engine always is). Prints how many scores and
best moves differ. This is the measurement behind the contract rule that the
producer evaluates from a cleared hash; the position cache keys on
(position, engine, depth) and is only correct if that rule holds.
"""

from __future__ import annotations

import argparse
import random
import sys

import chess
import chess.engine

from chess_coach.config import get_settings


def sample_positions(count: int, seed: int = 1) -> list[chess.Board]:
    rng = random.Random(seed)
    board = chess.Board()
    out = []
    while len(out) < count:
        if board.is_game_over():
            board = chess.Board()
        board.push(rng.choice(sorted(board.legal_moves, key=lambda m: m.uci())))
        if board.ply() % 4 == 0:
            out.append(board.copy(stack=False))
    return out


def evaluate(path: str, boards: list[chess.Board], depth: int, warm: bool) -> list[tuple]:
    rng = random.Random(5)
    engine = chess.engine.SimpleEngine.popen_uci(path)
    try:
        engine.configure({"Threads": 1, "Hash": 16})
        out = []
        for board in boards:
            if warm:
                other = chess.Board()
                for _ in range(12):
                    other.push(rng.choice(sorted(other.legal_moves, key=lambda m: m.uci())))
                engine.analyse(other, chess.engine.Limit(depth=depth))
            else:
                engine.configure({"Clear Hash": None})
            info = engine.analyse(board, chess.engine.Limit(depth=depth))
            out.append((info["score"].relative.score(mate_score=100000), info["pv"][0].uci()))
        return out
    finally:
        engine.quit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.engine_determinism")
    parser.add_argument("--depth", type=int, default=14)
    parser.add_argument("--positions", type=int, default=10)
    args = parser.parse_args(argv)

    path = str(get_settings().stockfish_path)
    boards = sample_positions(args.positions)
    cold_a = evaluate(path, boards, args.depth, warm=False)
    cold_b = evaluate(path, boards, args.depth, warm=False)
    warm = evaluate(path, boards, args.depth, warm=True)

    def differ(a, b, i):
        return sum(x[i] != y[i] for x, y in zip(a, b, strict=True))

    deltas = [abs(x[0] - y[0]) for x, y in zip(cold_a, warm, strict=True) if x[0] != y[0]]
    print(f"depth {args.depth}, {len(boards)} positions")
    for label, other in (("cold vs cold", cold_b), ("cold vs warm", warm)):
        scores, moves = differ(cold_a, other, 0), differ(cold_a, other, 1)
        print(f"{label}: scores differ {scores}, best moves differ {moves}")
    if deltas:
        print(f"cold vs warm score deltas: {min(deltas)}-{max(deltas)} cp")
    return 0


if __name__ == "__main__":
    sys.exit(main())
