"""Redis must not answer for a Postgres cluster other than the one it copied.

A fresh cluster (initdb) gives `positions` the same relfilenode every time
(16460 measured on two fresh postgres:17 clusters), so the namespace carries
the cluster's `system_identifier`. It has to be read per batch: a long-running
worker that rides out a Postgres re-creation otherwise keeps the old one.

The cluster ID is faked by rewriting the statement on its way to Postgres, so
the real `_namespace` code runs - mocking `_namespace` itself would test the mock.
"""

from __future__ import annotations

import chess
import pytest
from sqlalchemy import Engine, event

from chess_coach.positions import Evaluation, fen_hash, normalize_fen

pc = pytest.importorskip("chess_coach.cache.position_cache")

pytestmark = [pytest.mark.postgres, pytest.mark.redis]

ENGINE, DEPTH = "sf_18", 16
_REAL = "SELECT system_identifier FROM pg_control_system()"


@pytest.fixture
def fake_cluster_id(clean_engine: Engine):
    """Set `.value` to make every following query report that cluster ID."""

    class Fake:
        value = 111

    def rewrite(conn, cursor, statement, params, context, executemany):
        if _REAL in statement:
            statement = statement.replace(_REAL, f"SELECT {int(Fake.value)}::bigint")
        return statement, params

    event.listen(clean_engine, "before_cursor_execute", rewrite, retval=True)
    yield Fake
    event.remove(clean_engine, "before_cursor_execute", rewrite)


def test_a_cluster_change_under_a_running_cache_turns_redis_hits_into_misses(
    clean_engine, redis_client, fake_cluster_id
):
    if not hasattr(pc.PositionCache, "redis_name"):
        pytest.skip("no cluster-scoped namespace on this branch (before #48)")
    board = chess.Board()
    board.push_san("c4")
    key, fen = fen_hash(board), normalize_fen(board)
    cache = pc.PositionCache(clean_engine, redis_client, key_prefix=redis_client.key_prefix)

    cache.put(key, fen, ENGINE, DEPTH, Evaluation(eval_cp=10, mate_in=None, best_move="e7e5"))
    assert cache.get(key, ENGINE, DEPTH) is not None
    assert cache.stats.redis_hits == 1

    # The same process, the same PositionCache, a "new" cluster.
    fake_cluster_id.value = 222
    cache.get(key, ENGINE, DEPTH)
    # The row is still in this test's Postgres, so the answer comes from there -
    # but Redis must not have been the one to give it.
    assert cache.stats.redis_hits == 1, "Redis answered with the previous cluster's entry"
    assert cache.stats.postgres_hits == 1
