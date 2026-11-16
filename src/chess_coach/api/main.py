"""FastAPI application.

The API never runs Stockfish and never calls Chess.com. It reads finished rows
out of Postgres, which is what keeps response times flat no matter how large
the analysis backlog is.

Endpoints are defined with `def`, not `async def`: the handlers do blocking
SQLAlchemy work, so Starlette runs them in its threadpool. Declaring them
`async` would block the event loop on every query.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chess_coach import __version__
from chess_coach.api import players
from chess_coach.config import get_settings
from chess_coach.logging import setup_logging

settings = get_settings()
setup_logging(settings.log_level, json_output=settings.env != "dev")

app = FastAPI(
    title="Chess Coach",
    version=__version__,
    summary="Coaching reports built from a player's Chess.com history.",
)

# The dashboard in dev (3000) and the reviewer's copy (3001), per the contract.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)
app.include_router(players.router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness probe. Deliberately does not touch Postgres or Redis.

    Container orchestrators restart whatever fails this check, so it must not
    fail because a dependency is briefly unavailable. `/ready` is the endpoint
    that reports dependency health.
    """
    return {"status": "ok", "version": __version__, "env": settings.env}


def run() -> None:
    """Entry point for `chess-coach-api`."""
    import uvicorn

    uvicorn.run("chess_coach.api.main:app", host="0.0.0.0", port=8000, reload=settings.env == "dev")
