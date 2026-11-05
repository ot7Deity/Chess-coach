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
cp .env.example .env     # then edit CHESSCOM_CONTACT
docker compose up -d     # postgres + redis + api + workers
```

Full setup, benchmarks and design decisions land here as the milestones complete.
