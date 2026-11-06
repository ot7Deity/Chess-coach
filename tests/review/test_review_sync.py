"""Try to break sync_player: concurrency, the lock, the upsert, atomicity.

Real Postgres; Chess.com is the in-memory fake from tests/chesscom_fake.py.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable

import httpx
import pytest
from sqlalchemy import Engine, func, select, text, update
from sqlalchemy.orm import Session

from chess_coach.chesscom import ChessComClient
from chess_coach.ingest import sync as sync_mod
from chess_coach.ingest.sync import SyncInProgress, SyncResult, sync_player
from chess_coach.models import Archive, Game, Player
from tests.chesscom_fake import FakeChessCom, NoopLimiter, make_game

pytestmark = pytest.mark.postgres

ME = "deitythemenace"
NOV_15 = dt.datetime(2026, 11, 15, 12, tzinfo=dt.UTC)
MONTHS = ["2026-08", "2026-09", "2026-10", "2026-11"]


def _history(fake: FakeChessCom, username: str = ME, per_month: int = 3) -> None:
    fake.add_player(username)
    for i, month in enumerate(MONTHS):
        for j in range(per_month):
            fake.add_game(
                username, month, make_game(f"{username}-{month}-{j}", white=username, black=f"o{i}")
            )


@pytest.fixture
def fake() -> FakeChessCom:
    f = FakeChessCom()
    _history(f)
    return f


def _count(engine: Engine, model, *where) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def _player_id(engine: Engine, username: str = ME) -> int:
    with Session(engine) as s:
        return s.scalar(select(Player.id).where(Player.username == username))


def _advisory_locks_held(engine: Engine, player_id: int) -> int:
    """Two-int advisory locks show up with classid=key1, objid=key2, objsubid=2."""
    with engine.connect() as conn:
        return conn.scalar(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND classid = 1 AND objid = :id AND objsubid = 2 AND granted"
            ),
            {"id": player_id},
        )


def _client(fake: FakeChessCom, hook: Callable[[httpx.Request], None] | None = None):
    """The real ChessComClient against the fake, with an optional hook that runs
    inside each request (to park, synchronise or fail it)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if hook is not None:
            hook(request)
        return fake.handle(request)

    return ChessComClient(
        user_agent="ChessCoach/test",
        limiter=NoopLimiter(),
        sleep=lambda _s: None,
        transport=httpx.MockTransport(handler),
    )


def _create_player_only(engine: Engine, username: str = ME) -> int:
    """A player row with no archives or games yet."""
    empty = FakeChessCom()
    empty.add_player(username)
    sync_player(engine, empty.client(), username, now=NOV_15)
    return _player_id(engine, username)


# --- concurrency --------------------------------------------------------------


def test_a_sync_running_in_another_thread_blocks_a_second_one(
    clean_engine: Engine, fake: FakeChessCom
):
    """A real overlap, not a hand-held lock: sync A is genuinely mid-flight
    (parked inside a month request) when sync B starts."""
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)  # player exists
    inside, release = threading.Event(), threading.Event()

    def park(request: httpx.Request) -> None:
        if request.url.path.endswith("/games/2026/11"):
            inside.set()
            assert release.wait(10)

    outcome: dict[str, object] = {}

    def run_a() -> None:
        try:
            outcome["a"] = sync_player(clean_engine, _client(fake, park), ME, now=NOV_15)
        except BaseException as exc:
            outcome["a"] = exc

    a = threading.Thread(target=run_a)
    a.start()
    try:
        assert inside.wait(10)
        with Session(clean_engine) as s:
            assert s.scalar(select(Player.sync_status)) == "syncing"
        with pytest.raises(SyncInProgress):
            sync_player(clean_engine, fake.client(), ME, now=NOV_15)
        # The refused sync must not have touched the running one's status.
        with Session(clean_engine) as s:
            assert s.scalar(select(Player.sync_status)) == "syncing"
    finally:
        release.set()
        a.join(10)

    assert isinstance(outcome["a"], SyncResult), outcome["a"]
    assert _advisory_locks_held(clean_engine, _player_id(clean_engine)) == 0
    assert _count(clean_engine, Game) == 12


def test_two_first_time_syncs_racing_create_one_player(clean_engine: Engine, fake: FakeChessCom):
    """Both threads pass the 'does the player exist' check before either
    inserts (a barrier inside the profile request forces it)."""
    barrier = threading.Barrier(2, timeout=10)

    def meet(request: httpx.Request) -> None:
        if request.url.path == f"/pub/player/{ME}":
            barrier.wait()

    outcomes: list[object] = []

    def run() -> None:
        try:
            outcomes.append(sync_player(clean_engine, _client(fake, meet), ME, now=NOV_15))
        except BaseException as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert len(outcomes) == 2
    assert all(isinstance(o, SyncResult | SyncInProgress) for o in outcomes), outcomes
    assert any(isinstance(o, SyncResult) for o in outcomes)
    assert _count(clean_engine, Player) == 1
    assert _count(clean_engine, Game) == 12


def test_the_upsert_alone_keeps_overlapping_syncs_correct(clean_engine: Engine, fake: FakeChessCom):
    """sync.py's docstring: 'Correctness does not depend on the lock'. Prove it
    by running the month loop twice at once with no lock at all, overlapping
    inside the same month, and checking every game is stored once and counted
    as new exactly once in total."""
    player_id = _create_player_only(clean_engine)
    barrier = threading.Barrier(2, timeout=10)

    def meet(request: httpx.Request) -> None:
        if "/games/2026/" in request.url.path:
            barrier.wait()

    results = [SyncResult(username=ME), SyncResult(username=ME)]
    errors: list[BaseException] = []

    def run(result: SyncResult) -> None:
        try:
            sync_mod._sync_months(
                clean_engine,
                _client(fake, meet),
                player_id,
                ME,
                NOV_15,
                result,
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(r,)) for r in results]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == []
    assert _count(clean_engine, Game) == 12
    assert sum(r.games_new for r in results) == 12
    assert _count(clean_engine, Archive) == 4


# --- the lock is released on every path --------------------------------------


class _Boom(Exception):
    pass


def _fail_with(exc_factory: Callable[[], BaseException]) -> Callable[[httpx.Request], None]:
    def hook(request: httpx.Request) -> None:
        if request.url.path.endswith("/games/2026/10"):
            raise exc_factory()

    return hook


@pytest.mark.parametrize(
    ("label", "exc_factory", "expected"),
    [
        ("an ordinary exception", lambda: _Boom("x"), _Boom),
        ("Ctrl-C", KeyboardInterrupt, KeyboardInterrupt),
        ("SystemExit", lambda: SystemExit(3), SystemExit),
    ],
)
def test_the_lock_is_released_when_a_sync_dies(
    clean_engine: Engine, fake: FakeChessCom, label, exc_factory, expected
):
    player_id = _create_player_only(clean_engine)

    with pytest.raises(expected):
        sync_player(clean_engine, _client(fake, _fail_with(exc_factory)), ME, now=NOV_15)

    assert _advisory_locks_held(clean_engine, player_id) == 0, label
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)  # and nothing is wedged


def test_the_lock_is_released_after_a_database_error(clean_engine: Engine, fake: FakeChessCom):
    # A value Postgres rejects. (This test used a duplicate uuid until m1/7
    # made duplicates harmless.)
    bad = make_game("overflow", white=ME, black="z")
    bad["white"]["rating"] = 10**12
    fake.add_game(ME, "2026-10", bad)
    with pytest.raises(Exception, match="out of range"):
        sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert _advisory_locks_held(clean_engine, _player_id(clean_engine)) == 0
    with Session(clean_engine) as s:
        assert s.scalar(select(Player.sync_status)) == "error"


def test_a_month_with_a_rejected_game_is_downloaded_in_full_on_every_sync(
    clean_engine: Engine,
):
    """The price of m1/7's recovery rule, pinned so it is a decision rather than
    a surprise: until the parser accepts the game, that month is re-downloaded
    in full on every sync (no ETag, never frozen) - a re-sync is 3 requests, not
    2, forever, for every such month in the player's history."""
    fake = FakeChessCom()
    fake.add_player(ME)
    fake.add_game(ME, "2024-03", make_game("ok", white=ME, black="x"))
    fake.add_game(ME, "2024-03", make_game("weird", white=ME, black="y", time_class="hyper"))
    fake.add_game(ME, "2026-11", make_game("now", white=ME, black="z"))
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    for day in (1, 2, 3):
        fake.log.clear()
        result = sync_player(clean_engine, fake.client(), ME, now=NOV_15 + dt.timedelta(days=day))
        assert f"player/{ME}/games/2024/03" in fake.log
        assert (result.requests, result.months_fetched, result.games_new) == (3, 1, 0)
        assert result.games_rejected == 1
    with Session(clean_engine) as s:
        march = s.scalar(select(Archive).where(Archive.month == "2024-03"))
    assert (march.is_complete, march.etag) == (False, None)


def test_ctrl_c_mid_sync_does_not_leave_the_player_marked_syncing(
    clean_engine: Engine, fake: FakeChessCom
):
    _create_player_only(clean_engine)
    with pytest.raises(KeyboardInterrupt):
        sync_player(
            clean_engine,
            _client(fake, _fail_with(KeyboardInterrupt)),
            ME,
            now=NOV_15,
        )
    with Session(clean_engine) as s:
        assert s.scalar(select(Player.sync_status)) != "syncing"


# --- the upsert ---------------------------------------------------------------


def test_a_game_listed_twice_in_one_month_does_not_wedge_the_sync(
    clean_engine: Engine, fake: FakeChessCom
):
    fake.add_game(ME, "2026-10", make_game("dup", white=ME, black="z"))
    fake.add_game(ME, "2026-10", make_game("dup", white=ME, black="z"))
    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert result.games_new == 13
    assert _count(clean_engine, Game, Game.chesscom_uuid == "dup") == 1


def test_an_accuracy_backfill_is_an_update_not_a_new_game(clean_engine: Engine):
    """Exercises the DO UPDATE branch, which the existing tests never count:
    `xmax = 0` must be false for an updated row, and analysis state survives."""
    fake = FakeChessCom()
    fake.add_player(ME)
    games = [make_game(f"g{i}", white=ME, black="x") for i in range(4)]
    for g in games:
        fake.add_game(ME, "2026-11", g)
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    statuses = ["running", "complete", "skipped", "error"]
    with Session(clean_engine) as s, s.begin():
        for g, status in zip(games, statuses, strict=True):
            s.execute(
                update(Game).where(Game.chesscom_uuid == g["uuid"]).values(analysis_status=status)
            )
    for g in games:
        g["accuracies"] = {"white": 81.5, "black": 70.0}

    result = sync_player(clean_engine, fake.client(), ME, now=NOV_15 + dt.timedelta(hours=1))

    assert result.months_fetched == 1
    assert result.games_new == 0
    with Session(clean_engine) as s:
        rows = dict(s.execute(select(Game.chesscom_uuid, Game.analysis_status)).all())
        accuracies = set(s.scalars(select(Game.accuracy_player)))
    assert rows == {g["uuid"]: st for g, st in zip(games, statuses, strict=True)}
    assert accuracies == {81.5}


def test_an_accuracy_that_disappears_is_not_erased(clean_engine: Engine):
    fake = FakeChessCom()
    fake.add_player(ME)
    game = make_game("g1", white=ME, black="x", accuracies={"white": 90.0, "black": 50.0})
    fake.add_game(ME, "2026-11", game)
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    del game["accuracies"]
    fake.add_game(ME, "2026-11", make_game("g2", white=ME, black="y"))  # new ETag
    sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    with Session(clean_engine) as s:
        assert s.scalar(select(Game.accuracy_player).where(Game.chesscom_uuid == "g1")) == 90.0


def test_new_game_count_is_right_across_insert_chunks(clean_engine: Engine):
    """1,201 games in one month is three INSERT statements (chunks of 500)."""
    fake = FakeChessCom()
    fake.add_player(ME)
    games = [make_game(f"bulk{i}", white=ME, black="x") for i in range(1201)]
    for g in games:
        fake.add_game(ME, "2026-11", g)
    first = sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert first.games_new == 1201

    for g in games[::2]:
        g["accuracies"] = {"white": 60.0, "black": 61.0}
    fake.add_game(ME, "2026-11", make_game("bulk-new", white=ME, black="x"))
    second = sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert second.games_new == 1
    assert _count(clean_engine, Game) == 1202
    assert _count(clean_engine, Game, Game.accuracy_player == 60.0) == 601


def test_a_month_is_all_or_nothing(clean_engine: Engine, fake: FakeChessCom, monkeypatch):
    """INTERVIEW_PREP: 'nothing is half-written, because a month's games and its
    archive row commit together'. Fail the *second* insert chunk of a month and
    check the first chunk was rolled back with it."""
    monkeypatch.setattr(sync_mod, "_UPSERT_CHUNK", 2)
    bad = make_game("overflow", white=ME, black="x")
    bad["white"]["rating"] = 10**12  # integer out of range on the 2nd chunk
    fake.add_game(ME, "2026-10", bad)  # October: 3 good games then this one

    with pytest.raises(Exception, match="out of range"):
        sync_player(clean_engine, fake.client(), ME, now=NOV_15)

    with Session(clean_engine) as s:
        months = set(s.scalars(select(Archive.month)))
        october = s.scalar(
            select(func.count()).select_from(Game).where(Game.chesscom_uuid.like("%2026-10%"))
        )
    assert months == {"2026-08", "2026-09"}
    assert october == 0


# --- finality ---------------------------------------------------------------


def test_a_304_after_the_grace_day_freezes_the_month_and_keeps_its_count(clean_engine: Engine):
    fake = FakeChessCom()
    fake.add_player(ME)
    fake.add_game(ME, "2026-10", make_game("a", white=ME, black="x"))
    fake.add_game(ME, "2026-10", make_game("b", white=ME, black="y"))
    sync_player(clean_engine, fake.client(), ME, now=dt.datetime(2026, 10, 20, tzinfo=dt.UTC))
    with Session(clean_engine) as s:
        etag = s.scalar(select(Archive.etag))

    result = sync_player(
        clean_engine, fake.client(), ME, now=dt.datetime(2026, 11, 3, tzinfo=dt.UTC)
    )
    assert result.months_not_modified == 1
    with Session(clean_engine) as s:
        archive = s.scalar(select(Archive))
    assert (archive.is_complete, archive.etag, archive.game_count) == (True, etag, 2)


def test_a_rejected_game_in_a_final_month_is_recoverable_after_a_parser_fix(
    clean_engine: Engine,
):
    fake = FakeChessCom()
    fake.add_player(ME)
    fake.add_game(ME, "2026-09", make_game("ok", white=ME, black="x"))
    weird = make_game("weird", white=ME, black="y", time_class="hyperbullet")
    fake.add_game(ME, "2026-09", weird)
    first = sync_player(clean_engine, fake.client(), ME, now=NOV_15)
    assert first.games_rejected == 1

    weird["time_class"] = "bullet"  # stands in for "the parser learned this case"
    sync_player(clean_engine, fake.client(), ME, now=NOV_15 + dt.timedelta(days=1))
    assert _count(clean_engine, Game, Game.chesscom_uuid == "weird") == 1
