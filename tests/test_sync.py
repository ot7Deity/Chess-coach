"""Sync against a real Postgres and an in-memory Chess.com.

The M1 "done when" is the first two tests: adding a username loads the full
history once, and running it again fetches only the current month.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

from chess_coach.chesscom import ChessComUnavailable, PlayerNotFound
from chess_coach.ingest.sync import SyncInProgress, is_final, sync_player
from chess_coach.models import Archive, Game, Player
from tests.chesscom_fake import FakeChessCom, make_game

pytestmark = pytest.mark.postgres

ME = "deitythemenace"
NOV_15 = dt.datetime(2026, 11, 15, 12, tzinfo=dt.UTC)


def _history(fake: FakeChessCom) -> None:
    fake.add_player(ME)
    for i, month in enumerate(["2026-08", "2026-09", "2026-10", "2026-11"]):
        for j in range(3):
            fake.add_game(ME, month, make_game(f"{month}-{j}", white=ME, black=f"opp{i}{j}"))


def _count(engine: Engine, model) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(model))


@pytest.fixture
def fake() -> FakeChessCom:
    f = FakeChessCom()
    _history(f)
    return f


def test_first_sync_loads_the_full_history(clean_engine: Engine, fake: FakeChessCom):
    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    assert result.games_new == 12
    assert _count(clean_engine, Game) == 12
    with Session(clean_engine) as s:
        archives = {a.month: a.is_complete for a in s.scalars(select(Archive))}
        player = s.scalar(select(Player))
    assert archives == {"2026-08": True, "2026-09": True, "2026-10": True, "2026-11": False}
    assert player.sync_status == "complete"
    assert player.last_synced_at == NOV_15
    assert player.chesscom_player_id == 1


def test_resync_fetches_only_the_current_month(clean_engine: Engine, fake: FakeChessCom):
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    fake.log.clear()

    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15 + dt.timedelta(hours=1))

    # No profile, no finished months: the archive list and November, which is
    # unchanged and so a 304.
    assert fake.log == [f"player/{ME}/games/archives", f"player/{ME}/games/2026/11"]
    assert result.months_skipped == 3
    assert result.months_not_modified == 1
    assert result.requests == 2
    assert result.games_new == 0


def test_resync_picks_up_new_games_in_the_current_month(clean_engine: Engine, fake: FakeChessCom):
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    fake.add_game(ME, "2026-11", make_game("new-one", white="late-opp", black=ME))

    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15 + dt.timedelta(days=1))
    assert result.months_fetched == 1
    assert result.games_new == 1
    assert _count(clean_engine, Game) == 13


def test_a_month_is_refetched_after_it_ends_then_frozen(clean_engine: Engine):
    """A game finished late on the 31st may reach the archive after midnight,
    so the month is revisited until a full day past its end, then never again."""
    fake = FakeChessCom()
    fake.add_player(ME)
    fake.add_game(ME, "2026-10", make_game("early", white=ME, black="a"))
    sync_player(clean_engine, fake.client(), ME, now=dt.datetime(2026, 10, 20, tzinfo=dt.UTC))

    fake.add_game(ME, "2026-10", make_game("last-minute", white=ME, black="b"))
    nov_1_morning = dt.datetime(2026, 11, 1, 6, tzinfo=dt.UTC)
    result = sync_player(clean_engine, fake.client(), ME, now=nov_1_morning)
    assert result.games_new == 1
    with Session(clean_engine) as s:
        assert s.scalar(select(Archive.is_complete)) is False  # still inside the grace day

    nov_2 = dt.datetime(2026, 11, 2, 1, tzinfo=dt.UTC)
    sync_player(clean_engine, fake.client(), ME, now=nov_2)
    with Session(clean_engine) as s:
        assert s.scalar(select(Archive.is_complete)) is True

    fake.log.clear()
    sync_player(clean_engine, fake.client(), ME, now=nov_2 + dt.timedelta(days=1))
    assert f"player/{ME}/games/2026/10" not in fake.log


def test_is_final_boundaries():
    assert not is_final("2026-12", dt.datetime(2027, 1, 1, 23, 59, tzinfo=dt.UTC))
    assert is_final("2026-12", dt.datetime(2027, 1, 2, tzinfo=dt.UTC))


def test_resync_never_resets_analysis_state(clean_engine: Engine, fake: FakeChessCom):
    """Re-ingesting a game must not send a finished analysis back to the queue."""
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    with Session(clean_engine) as s, s.begin():
        s.execute(update(Game).values(analysis_status="complete"))

    # Force November to be re-downloaded in full (new ETag) rather than 304.
    fake.add_game(ME, "2026-11", make_game("extra", white=ME, black="z"))
    sync_player(clean_engine, fake.client(), ME, now=NOV_15 + dt.timedelta(days=1))

    with Session(clean_engine) as s:
        statuses = dict(s.execute(select(Game.chesscom_uuid, Game.analysis_status)).all())
    assert statuses.pop("extra") == "pending"
    assert set(statuses.values()) == {"complete"}


def test_accuracies_that_appear_later_are_backfilled(clean_engine: Engine):
    fake = FakeChessCom()
    fake.add_player(ME)
    game = make_game("g1", white=ME, black="a")
    fake.add_game(ME, "2026-11", game)
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    game["accuracies"] = {"white": 90.1, "black": 60.0}
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    with Session(clean_engine) as s:
        assert s.scalar(select(Game.accuracy_player)) == 90.1


def test_an_unchanged_refetch_writes_no_rows(clean_engine: Engine, fake: FakeChessCom):
    """The upsert's WHERE clause: re-downloading a month must not rewrite every
    game in it (dead tuples, WAL, and updated_at churn for nothing)."""
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    with Session(clean_engine) as s:
        before = s.scalar(select(func.max(Game.updated_at)))

    fake.add_game(ME, "2026-11", make_game("extra", white=ME, black="z"))  # new ETag
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    with Session(clean_engine) as s:
        rewritten = s.scalar(
            select(func.count())
            .select_from(Game)
            .where(Game.updated_at > before, Game.chesscom_uuid != "extra")
        )
    assert rewritten == 0


def test_a_game_between_two_tracked_players_belongs_to_both(clean_engine: Engine):
    """The schema bug M1 found: with chesscom_uuid globally unique, the second
    player's copy of a shared game collided with the first's and vanished."""
    fake = FakeChessCom()
    fake.add_player("alice", player_id=1)
    fake.add_player("bob", player_id=2)
    shared = make_game("shared-uuid", white="Alice", black="Bob")
    fake.add_game("alice", "2026-11", shared)
    fake.add_game("bob", "2026-11", shared)

    sync_player(clean_engine, fake.client(), "alice", now=NOV_15)
    sync_player(clean_engine, fake.client(), "bob", now=NOV_15)

    with Session(clean_engine) as s:
        rows = s.execute(
            select(Player.username, Game.color, Game.result)
            .join(Game.player)
            .where(Game.chesscom_uuid == "shared-uuid")
            .order_by(Player.username)
        ).all()
    assert [tuple(r) for r in rows] == [("alice", "white", "win"), ("bob", "black", "loss")]


def test_unknown_player_creates_nothing(clean_engine: Engine):
    with pytest.raises(PlayerNotFound):
        sync_player(clean_engine, FakeChessCom().client(), "no-such-user", now=NOV_15)
    assert _count(clean_engine, Player) == 0


def test_a_crashed_sync_resumes_where_it_stopped(clean_engine: Engine, fake: FakeChessCom):
    fake.fail(f"player/{ME}/games/2026/10", *[503] * 10)
    with pytest.raises(ChessComUnavailable):
        sync_player(clean_engine, fake.client(max_retries=1), ME, now=NOV_15)

    with Session(clean_engine) as s:
        player = s.scalar(select(Player))
        done = set(s.scalars(select(Archive.month)))
    assert player.sync_status == "error"
    assert "ChessComUnavailable" in player.sync_error
    assert done == {"2026-08", "2026-09"}

    fake.failures.clear()
    fake.log.clear()
    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert result.months_skipped == 2
    assert f"player/{ME}/games/2026/08" not in fake.log
    assert _count(clean_engine, Game) == 12
    with Session(clean_engine) as s:
        player = s.scalar(select(Player))
    assert (player.sync_status, player.sync_error) == ("complete", None)


def test_a_malformed_game_is_counted_and_skipped_not_fatal(clean_engine: Engine):
    fake = FakeChessCom()
    fake.add_player(ME)
    fake.add_game(ME, "2026-11", make_game("good", white=ME, black="a"))
    fake.add_game(ME, "2026-11", make_game("bad", white=ME, black="a", time_class="hyper"))

    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert (result.games_new, result.games_rejected) == (1, 1)


def test_a_second_concurrent_sync_is_refused(clean_engine: Engine, fake: FakeChessCom):
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    with Session(clean_engine) as s:
        player_id = s.scalar(select(Player.id))

    with clean_engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_lock(1, :id)"), {"id": player_id})
        try:
            with pytest.raises(SyncInProgress):
                sync_player(clean_engine, fake.client(), ME, now=NOV_15)
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(1, :id)"), {"id": player_id})

    # And the lock is released after a normal sync, so the next one proceeds.
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)


def test_username_case_does_not_create_a_second_player(clean_engine: Engine, fake: FakeChessCom):
    sync_player(clean_engine, fake.client(), "DeityTheMenace", now=NOV_15)
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert _count(clean_engine, Player) == 1
