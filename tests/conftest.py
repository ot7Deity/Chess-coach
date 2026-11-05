from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_stockfish() -> Path | None:
    """Find a usable Stockfish, or None so engine tests can skip cleanly.

    Checked in order: STOCKFISH_PATH, the vendored binary, then PATH. Engine
    tests must never be a hard requirement for running the unit suite, or a
    fresh clone cannot run tests until an 80 MB download finishes.
    """
    env = os.environ.get("STOCKFISH_PATH")
    if env:
        candidate = Path(env)
        if not candidate.is_absolute():
            candidate = REPO_ROOT / candidate
        if candidate.exists():
            return candidate

    for name in ("stockfish.exe", "stockfish"):
        candidate = REPO_ROOT / "vendor" / "stockfish" / name
        if candidate.exists():
            return candidate

    found = shutil.which("stockfish")
    return Path(found) if found else None


@pytest.fixture(scope="session")
def stockfish_path() -> Path:
    path = _resolve_stockfish()
    if path is None:
        pytest.skip("no Stockfish binary (run: uv run python tools/fetch_stockfish.py)")
    return path
