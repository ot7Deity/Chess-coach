"""The position cache (M3). See `position_cache` for the design.

The analysis worker should depend only on `chess_coach.positions.EvalCache`.
"""

from __future__ import annotations

from chess_coach.cache.position_cache import CacheStats, PositionCache

__all__ = ["CacheStats", "PositionCache"]
