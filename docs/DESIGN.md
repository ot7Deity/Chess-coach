# Chess Coach: System Design

> This is the original design document, kept as written on 2026-11-05.
> Where the build has deliberately departed from it, the reason is recorded in
> [`../INTERVIEW_PREP.md`](../INTERVIEW_PREP.md) rather than edited in here, so the
> gap between "what I planned" and "what the API actually did" stays visible.

## Overview

Chess Coach pulls a player's full Chess.com history, runs every game through Stockfish on a
pool of background workers, and turns the results into a coaching report: where they blunder,
which openings cost them rating, and how they play under time pressure. It updates
automatically after new games.

The engineering point is the backend, not the chess: rate-limited ingestion, a job queue with
retries, CPU-heavy parallel work, caching, and SQL analytics over a growing dataset.

Scope: one user type (a player looking at their own games), every time control (bullet, blitz,
rapid, daily) analyzed and reported separately, and a web dashboard. Live play, puzzles and
multiplayer features are out of scope.

## Architecture

Two pipelines that meet in Postgres: ingestion pulls games in, analysis grades them, and the
website only reads finished results.

A sync starts on signup or nightly. The ingest worker fetches only new monthly archives,
upserts games and enqueues one analysis job per game. Analysis workers claim jobs, check the
position cache before calling Stockfish, and write evaluations back. Because the API never
runs Stockfish, the website stays fast no matter how big the backlog gets.

## Data model

Six Postgres tables. The key design choice is splitting `positions` out from `games`: the same
position shows up in thousands of games, so its engine evaluation is stored once and reused.

| Table | What a row is | Key columns | Indexes / constraints |
| --- | --- | --- | --- |
| `players` | A Chess.com account being tracked | `id`, `username`, `last_synced_month`, `sync_status` | unique `username` |
| `archives` | One month of a player's games | `player_id`, `month` (YYYY-MM), `etag`, `fetched_at` | unique (`player_id`, `month`) |
| `games` | One finished game | `id`, `chesscom_uuid`, `player_id`, `color`, `result`, `time_class`, `opening_eco`, `pgn`, `played_at`, `analysis_status` | unique `chesscom_uuid`; (`player_id`, `played_at`) |
| `moves` | One move in a game | `game_id`, `ply`, `fen_before`, `san`, `clock_seconds`, `eval_before`, `eval_after`, `classification` | (`game_id`, `ply`) |
| `positions` | A unique board position + its engine result | `fen_hash`, `fen`, `depth`, `best_move`, `eval_cp`, `mate_in` | primary key `fen_hash` |
| `jobs` | One unit of background work | `id`, `type`, `payload`, `status`, `attempts`, `run_after`, `locked_by`, `locked_at` | (`status`, `run_after`) |

`classification` is computed from the eval swing: blunder (lost >= 300 centipawns), mistake
(>= 100), inaccuracy (>= 50). Store raw evals too so thresholds can change later without
re-running Stockfish.

**Time controls.** Store both `time_class` and the exact `time_control` (e.g. 180+2) on every
game. Index (`player_id`, `time_class`, `played_at`), because every report query filters or
groups by time class. A 15% blunder rate means something very different in bullet than in
rapid, so results are never mixed across time controls. Ratings are tracked per time class
too, since Chess.com keeps a separate rating for each.

Queue priority follows the same split: analyze rapid and blitz first, then bullet. Bullet
players often have thousands of short games, and analyzing them last keeps a new user's first
report fast.

## Key engineering problems

| Problem | Why it's hard | Approach |
| --- | --- | --- |
| Chess.com rate limits | Parallel requests get HTTP 429; the API needs a User-Agent with contact info | One shared token-bucket limiter (~1 request/sec) in Redis that every ingest worker goes through; exponential backoff with jitter on 429 |
| Re-syncing without waste | No "changed since" filter; a player can have 100+ monthly archives | Store each month's ETag and send `If-None-Match` (304 = skip). Only the current month is ever re-fetched |
| Duplicate work | A crash mid-job, or two workers grabbing the same job, means analyzing a game twice | Claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED`; unique `chesscom_uuid`; jobs are safe to run twice (upserts, not inserts) |
| Failures and retries | Stockfish can hang; a worker can die holding a job | Per-job timeout; `attempts` counter with backoff via `run_after`; a reaper that frees jobs locked longer than 5 minutes; dead-letter after 5 attempts |
| Stockfish is slow | One game at depth 18 takes seconds; a 3,000-game history takes hours on one core | Cache by position (`fen_hash`) so repeated positions are free; skip the first ~8 opening moves via cache; scale workers horizontally; let users see partial results as games finish |

Start with Postgres as the queue (the `jobs` table). Only move to Redis Streams or RabbitMQ if
Postgres is measured becoming the bottleneck, and if so, write up the before/after numbers.

## Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| API | FastAPI | Shipped it before; auto-generated docs |
| Workers | Python processes, `python-chess` + Stockfish binary | Mature UCI engine wrapper; one Stockfish per worker process |
| Queue | Postgres `jobs` table (`SKIP LOCKED`) | No extra infrastructure; easy to explain and inspect |
| Database | PostgreSQL | Analytics queries (window functions, GROUP BY opening) |
| Rate limiter / cache | Redis | Shared token bucket across workers; hot position cache in front of Postgres |
| Frontend | Next.js + a chart library | Already on the resume; keep it simple |
| Local dev | Docker Compose | One command starts API, workers, Postgres, Redis |
| CI | GitHub Actions | Tests + lint on every push |
| Hosting | Fly.io or Railway (API + workers), managed Postgres | Cheap, scales worker count with one command |
| Observability | Prometheus metrics + a Grafana dashboard | Real throughput and latency numbers |

## Build plan

- **M0 Setup** - repo, Docker Compose with Postgres + Redis, FastAPI hello-world, CI.
- **M1 Ingestion** - `players`/`archives`/`games`; archives list to monthly fetch with ETag to
  upsert; shared rate limiter + backoff. *Done when:* adding a username loads full history
  once, and re-running fetches only the current month.
- **M2 Queue + Stockfish** - `jobs` table, `SKIP LOCKED` claim, retries, timeouts, reaper;
  analysis worker writes `moves` with evals. *Done when:* killing a worker mid-job still
  analyzes the game exactly once.
- **M3 Caching + scaling** - `positions` + Redis cache; 1/2/4/8 worker throughput table;
  Prometheus metrics. *Done when:* there is a throughput table worth putting in the README.
- **M4 Insights + API** - report SQL per time control (blunders by phase, results by opening,
  accuracy under 30s, rating trend); report and sync-progress endpoints.
- **M5 Frontend + deploy** - Next.js dashboard, deploy, nightly re-sync.
- **M6 Polish** - README with architecture diagram, benchmark table and design decisions.

## Metrics to measure from day one

| Metric | How to measure |
| --- | --- |
| Games analyzed | `COUNT(*)` of analyzed games |
| Throughput | Games/minute at 1, 2, 4, 8 workers |
| Cache hit rate | Positions served from cache / positions requested |
| Engine time saved | Stockfish seconds with cache vs without, same games |
| Failure recovery | Jobs retried and completed after a killed worker |
| Users | Distinct usernames synced |

## Interview talking points

1. **Why Postgres as the queue:** what `SKIP LOCKED` does, and when to switch.
2. **The duplicate-work bug:** a real failure, how it was found, how idempotency fixed it.
3. **The caching decision:** why cache by position rather than by game, and the hit rate.
4. **Scaling numbers:** throughput from 1 to 8 workers, and where it stopped scaling and why.
5. **Why build it:** wanted to know why I kept losing.
