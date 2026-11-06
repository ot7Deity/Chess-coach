"""Prometheus metrics, defined once for every process.

One registry per process, one /metrics endpoint per process. Workers are
separate OS processes, so each serves its own counters and Prometheus sums
across targets (`sum(rate(chess_coach_games_analyzed_total[5m]))`). That is
simpler and more honest than prometheus-client's multiprocess mode, which
exists for forking web servers sharing one port, not for independent workers.

Naming: counters are declared without `_total`; the client library appends it.
Label values are fixed vocabularies (listed beside each metric) and are
pre-initialised to zero, so `rate()` has a series from the first scrape rather
than only after the first event - otherwise "zero dead jobs" and "no data" look
the same on a dashboard.

The cache records its own metrics (`chess_coach.cache`). Everything else here is
for the analysis worker to record; see the `record_*` helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from wsgiref.simple_server import WSGIServer

from prometheus_client import Counter, Gauge, Histogram, start_http_server

# --- analysis ---------------------------------------------------------------

GAME_OUTCOMES = ("complete", "skipped", "error")
GAMES_ANALYZED = Counter(
    "chess_coach_games_analyzed",
    "Games the analysis worker finished, by outcome (complete|skipped|error).",
    ["outcome"],
)

POSITIONS_EVALUATED = Counter(
    "chess_coach_positions_evaluated",
    "Positions actually sent to Stockfish, i.e. cache misses that were computed.",
)
ENGINE_SECONDS = Counter(
    "chess_coach_engine_seconds",
    "Wall-clock seconds spent inside Stockfish searches.",
)

# --- position cache -----------------------------------------------------------

# (result, layer): ('hit', 'redis'), ('hit', 'postgres'), ('miss', 'none').
# Hit rate = sum(hit) / sum(all); Redis's share of hits = layer="redis" / hit.
CACHE_LOOKUP_LABELS = (("hit", "redis"), ("hit", "postgres"), ("miss", "none"))
CACHE_LOOKUPS = Counter(
    "chess_coach_cache_lookups",
    "Position cache lookups, one per distinct position asked for.",
    ["result", "layer"],
)
ENGINE_SECONDS_SAVED = Counter(
    "chess_coach_engine_seconds_saved",
    "Stockfish seconds not spent because of cache hits (sum of the hits' compute_ms).",
)
CACHE_WRITE_OUTCOMES = ("inserted", "existing")
CACHE_WRITES = Counter(
    "chess_coach_cache_writes",
    "Evaluations written to the cache: 'inserted' won the row, 'existing' lost a race "
    "or repeated a write, and was a no-op.",
    ["outcome"],
)
CACHE_REDIS_OPS = ("get", "set")
CACHE_REDIS_ERRORS = Counter(
    "chess_coach_cache_redis_errors",
    "Redis commands that failed. The cache carried on from Postgres.",
    ["op"],
)
CACHE_CORRUPT_VALUES = Counter(
    "chess_coach_cache_corrupt_values",
    "Redis values that could not be decoded or were for a different engine/depth; "
    "treated as a miss and overwritten from Postgres.",
)
CACHE_EVAL_CONFLICTS = Counter(
    "chess_coach_cache_eval_conflicts",
    "Losing writes whose score, mate or best move differed from the stored row. "
    "Should stay 0: evaluations are meant to be a pure function of the key.",
)
CACHE_REDIS_UP = Gauge(
    "chess_coach_cache_redis_up",
    "1 while the cache is using Redis, 0 while it has fallen back to Postgres only.",
)

# --- jobs -------------------------------------------------------------------

JOB_OUTCOMES = ("done", "retry", "dead")
JOB_CLAIM_SECONDS = Histogram(
    "chess_coach_job_claim_seconds",
    "Time for one claim query (SELECT ... FOR UPDATE SKIP LOCKED), found work or not.",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
JOB_DURATION_SECONDS = Histogram(
    "chess_coach_job_duration_seconds",
    "Claim-to-finish time of one job, by type and outcome (done|retry|dead).",
    ["job_type", "outcome"],
    # A cached game is sub-second; a cold depth-16 game is tens of seconds.
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 600),
)
JOBS_RETRIED = Counter(
    "chess_coach_jobs_retried",
    "Jobs that failed and were rescheduled with backoff.",
    ["job_type"],
)
JOBS_DEAD = Counter(
    "chess_coach_jobs_dead",
    "Jobs that exhausted their attempts and were dead-lettered.",
    ["job_type"],
)


def _initialise_label_sets() -> None:
    for outcome in GAME_OUTCOMES:
        GAMES_ANALYZED.labels(outcome=outcome)
    for result, layer in CACHE_LOOKUP_LABELS:
        CACHE_LOOKUPS.labels(result=result, layer=layer)
    for outcome in CACHE_WRITE_OUTCOMES:
        CACHE_WRITES.labels(outcome=outcome)
    for op in CACHE_REDIS_OPS:
        CACHE_REDIS_ERRORS.labels(op=op)


_initialise_label_sets()


# --- recording helpers --------------------------------------------------------


def record_engine_eval(seconds: float) -> None:
    """One position searched by Stockfish."""
    POSITIONS_EVALUATED.inc()
    ENGINE_SECONDS.inc(max(0.0, seconds))


def record_cache_hit(layer: str, compute_ms: int | None) -> None:
    CACHE_LOOKUPS.labels(result="hit", layer=layer).inc()
    if compute_ms:
        ENGINE_SECONDS_SAVED.inc(compute_ms / 1000)


def record_cache_miss(count: int = 1) -> None:
    if count:
        CACHE_LOOKUPS.labels(result="miss", layer="none").inc(count)


# --- HTTP endpoint ------------------------------------------------------------


@dataclass
class MetricsServer:
    port: int
    _server: WSGIServer

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def start_metrics_server(
    port: int | None = None, addr: str | None = None, *, worker_index: int = 0
) -> MetricsServer | None:
    """Serve /metrics on a background thread; None when metrics are disabled.

    `port` defaults to METRICS_PORT and 0 means disabled, so running several
    workers with the default config cannot fail on a port clash. Workers on one
    host pass their index and listen on port + index.
    """
    from chess_coach.config import get_settings

    settings = get_settings()
    base = settings.metrics_port if port is None else port
    if base == 0:
        return None
    server, _thread = start_http_server(base + worker_index, addr or settings.metrics_addr)
    return MetricsServer(port=server.server_port, _server=server)
