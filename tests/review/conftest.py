"""Adversarial review suite. Inherits every fixture from tests/conftest.py.

Tests that pin down a confirmed bug or a design disagreement are marked
`xfail(strict=True)` with a reason starting `BUG:` or `DISAGREEMENT:`. They
assert the behaviour the code *should* have, so the suite stays green while the
problem exists and turns red (XPASS) the moment someone fixes it - at which
point the marker should be removed and the test kept as a regression test.
"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "live: calls the real Chess.com API; skipped unless RUN_LIVE=1"
    )
