"""Metric definitions and the per-worker /metrics endpoint."""

from __future__ import annotations

import socket
import urllib.request

from prometheus_client import REGISTRY

from chess_coach import metrics
from chess_coach.config import Settings

EXPECTED = [
    "chess_coach_games_analyzed_total",
    "chess_coach_positions_evaluated_total",
    "chess_coach_engine_seconds_total",
    "chess_coach_cache_lookups_total",
    "chess_coach_engine_seconds_saved_total",
    "chess_coach_cache_writes_total",
    "chess_coach_cache_redis_errors_total",
    "chess_coach_cache_corrupt_values_total",
    "chess_coach_cache_eval_conflicts_total",
    "chess_coach_cache_redis_up",
    "chess_coach_job_claim_seconds_bucket",
    "chess_coach_jobs_retried_total",
    "chess_coach_jobs_dead_total",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_labelled_series_exist_at_zero_before_any_event():
    """So `rate()` has a series from the first scrape: 'no dead jobs yet' must
    read as 0, not as missing data."""
    for outcome in metrics.GAME_OUTCOMES:
        assert (
            REGISTRY.get_sample_value("chess_coach_games_analyzed_total", {"outcome": outcome})
            is not None
        )
    for result, layer in metrics.CACHE_LOOKUP_LABELS:
        labels = {"result": result, "layer": layer}
        assert REGISTRY.get_sample_value("chess_coach_cache_lookups_total", labels) is not None


def test_record_engine_eval_counts_positions_and_seconds():
    def sample(name):
        return REGISTRY.get_sample_value(name) or 0.0

    before = (
        sample("chess_coach_positions_evaluated_total"),
        sample("chess_coach_engine_seconds_total"),
    )
    metrics.record_engine_eval(0.5)
    metrics.record_engine_eval(-1.0)  # a clock step backwards never subtracts
    assert sample("chess_coach_positions_evaluated_total") - before[0] == 2
    assert sample("chess_coach_engine_seconds_total") - before[1] == 0.5


def test_port_zero_disables_the_endpoint():
    assert metrics.start_metrics_server(port=0) is None


def test_default_config_does_not_open_a_port(monkeypatch):
    """Eight workers with the default config must not fight over one port."""
    monkeypatch.delenv("METRICS_PORT", raising=False)
    assert Settings(_env_file=None).metrics_port == 0


def test_each_worker_serves_on_base_port_plus_its_index():
    base = _free_port()
    server = metrics.start_metrics_server(port=base - 1, addr="127.0.0.1", worker_index=1)
    assert server is not None
    try:
        assert server.port == base
        body = urllib.request.urlopen(f"http://127.0.0.1:{base}/metrics", timeout=5).read()
        text = body.decode()
        for name in EXPECTED:
            assert name in text, name
    finally:
        server.stop()
