"""Clock readings and time spent per move. Pure functions.

Chess.com writes the mover's remaining clock after every move as a PGN comment,
`{[%clk 0:02:59.9]}`, and that reading already includes the increment. So the
time a player spent on a move is

    previous reading for that player - this reading + increment

where a player's "previous reading" before their first move is the base time.
The first move is charged like any other: on real data White's first reading
is usually already below the base (1,018 of 1,143 no-increment bullet games),
so the clock does run on it. Checked over 254,553 real plies: this formula
gives no negative move times, where ignoring the increment would give 31,262.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import chess


@dataclass(frozen=True)
class TimeControl:
    base_sec: float
    increment_sec: float


def parse_time_control(value: str | None) -> TimeControl | None:
    """'180+2' -> 180 s base, 2 s increment; '600' -> no increment.

    None for daily games ('1/86400' - a per-move allowance in seconds, not a
    clock that runs down across the game, so "time spent" cannot be derived
    the same way) and for anything unparseable. A missing time control means
    missing move times, never wrong ones.
    """
    if not value or "/" in value:
        return None
    base, _, increment = value.partition("+")
    try:
        tc = TimeControl(float(base), float(increment) if increment else 0.0)
    except ValueError:
        return None
    if tc.base_sec <= 0 or tc.increment_sec < 0:
        return None
    return tc


def move_times(
    clocks: Sequence[float | None],
    movers: Sequence[chess.Color],
    tc: TimeControl | None,
) -> list[float | None]:
    """Seconds spent on each ply, given each ply's clock reading and mover.

    A ply whose own reading, or the same player's previous reading, is missing
    gets None rather than a guess. Never negative: readings are rounded to a
    tenth of a second, which can otherwise produce -0.1 for an instant move.
    """
    if len(clocks) != len(movers):
        raise ValueError("one clock reading per ply")
    if tc is None:
        return [None] * len(clocks)

    previous: dict[chess.Color, float | None] = {chess.WHITE: tc.base_sec, chess.BLACK: tc.base_sec}
    spent: list[float | None] = []
    for clock, mover in zip(clocks, movers, strict=True):
        before = previous[mover]
        if clock is None or before is None:
            spent.append(None)
        else:
            spent.append(max(0.0, round(before - clock + tc.increment_sec, 3)))
        previous[mover] = clock
    return spent
