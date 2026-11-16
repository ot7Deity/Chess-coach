# Chess Coach

Pulls a player's full Chess.com history, grades every move with Stockfish on a pool of
background workers, and turns the results into a coaching report: where they blunder, which
openings cost them rating, and how they play under time pressure.

**Status:** in development. See [docs/DESIGN.md](docs/DESIGN.md) for the system design and
[INTERVIEW_PREP.md](INTERVIEW_PREP.md) for the running log of engineering decisions.

## What it is really about

The chess is the domain; the project is a backend systems exercise:

- rate-limited ingestion against a third-party API (token bucket, ETag revalidation, backoff)
- a Postgres-backed job queue with `SELECT ... FOR UPDATE SKIP LOCKED`, retries and a reaper
- CPU-bound parallel work (one Stockfish process per worker, scaled horizontally)
- a position-level evaluation cache (Redis in front of Postgres)
- SQL analytics over a growing dataset, reported per time control

## Architecture

```
            ┌─────────────┐        ┌──────────────┐
Chess.com ──│ ingest      │───────▶│              │
   API      │ worker(s)   │        │   Postgres   │◀── FastAPI ──▶ Next.js
            └─────────────┘        │              │    (reads     dashboard
                   │ enqueue       │  games       │     only)
                   ▼               │  moves       │
            ┌─────────────┐        │  positions   │
            │ analysis    │───────▶│  jobs        │
            │ worker(s)   │        └──────────────┘
            │ + Stockfish │               ▲
            └─────────────┘               │
                   │  position cache      │
                   └──────▶ Redis ────────┘
```

Ingestion and analysis both write to Postgres; the API only ever reads finished results, so
the website stays fast no matter how deep the analysis backlog is.

## Quick start

Requires Docker Desktop and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env            # then edit CHESSCOM_CONTACT
docker compose up -d            # postgres + redis
uv sync --extra dev
uv run python tools/fetch_stockfish.py         # the engine build that runs on this CPU
uv run alembic upgrade head     # create the schema
uv run chess-coach-sync <chess.com username>   # load a player's history, queue its games
uv run chess-coach-worker --until-empty        # analyse the queue, then exit
uv run uvicorn chess_coach.api.main:app --reload
```

`chess-coach-sync` is safe to re-run: every month that has ended is fetched once and then
never again, and the current month is revalidated with its ETag, so an up-to-date player
costs two requests. For `deitythemenace` the first sync loads 4,550 games in about a minute
(at the polite default of one request per second); the second makes two requests and
downloads nothing.

A sync finishes by putting every newly loaded game on the analysis queue (one job per
game, rapid, blitz and daily ahead of bullet). `chess-coach-worker` runs one Stockfish and
works through that queue; start several to go faster. Each one takes a different game, and
a worker that dies mid-game has its game picked up by another once its lease expires.
`--worker-id`, `--max-jobs N` and `--until-empty` exist for benchmarks.

### Running the app in Docker

`docker compose up -d` starts the infrastructure only - Postgres and Redis, which the tests
and host-run workers need. The app is behind the `app` profile:

```bash
docker compose --profile app up -d --build            # migrations, API on :8000, one worker
docker compose --profile app up -d --scale worker=4   # four workers
docker compose logs -f worker
```

The image contains no Stockfish binary. Each worker container runs the installer when it
starts, which picks the fastest build that actually runs on that machine's CPU and stores it
in the `vendor` volume, so the ~80 MB download happens once per machine. This is not a
formality: on the development laptop the host CPU supports AVX-VNNI but Docker's VM does
not pass it through, so the AVX-VNNI build crashes inside the container and the installer
falls back to AVX2. See [INTERVIEW_PREP.md](INTERVIEW_PREP.md) for that story and the
decision behind it.

### Tests

The Postgres-backed tests prove the migrations build the schema from empty, by dropping
and recreating it on every run. A fixture that destructive refuses to touch a database
whose name does not contain `test`, so compose creates `chess_coach_test` alongside the
application database:

```bash
DATABASE_URL=postgresql+psycopg://chess:chess@localhost:5432/chess_coach_test uv run pytest
```

```powershell
$env:DATABASE_URL = "postgresql+psycopg://chess:chess@localhost:5432/chess_coach_test"
uv run pytest
```

Without a reachable Postgres those tests skip rather than fail, so a fresh clone can run
the suite immediately. Engine tests need a binary:
`uv run python tools/fetch_stockfish.py`.

Benchmarks and the rest of the design decisions land here as the milestones complete.
