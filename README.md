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
uv run alembic upgrade head     # create the schema
uv run uvicorn chess_coach.api.main:app --reload
```

Compose starts the infrastructure only. The API and workers run on the host for now -
they join the compose file in M2, once there is a worker to run and the engine binary
can be probed on the machine that will execute it. See
[INTERVIEW_PREP.md](INTERVIEW_PREP.md) for why it is split that way.

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
