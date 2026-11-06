"""Chess.com Published-Data API client.

Every request goes through the shared rate limiter first, retries are bounded,
and a 429 slows down every worker rather than only the one that received it.

Only the three endpoints ingestion needs are wrapped: the profile (once, on a
player's first sync), the list of monthly archives, and one month of games.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from chess_coach.logging import get_logger
from chess_coach.ratelimit import RateLimiter

log = get_logger(__name__)

# Chess.com usernames are letters, digits, '_' and '-'. Checked before the name
# is put into a URL path, so input like '../x' or 'a?b' cannot reach a
# different endpoint than the one intended.
_USERNAME = re.compile(r"^[a-z0-9_-]{1,50}$")
_ARCHIVE_URL = re.compile(r"/games/(\d{4})/(\d{2})/?$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")

# A server-supplied Retry-After is honoured, up to this. Beyond it, something is
# wrong enough that failing the sync is more useful than sleeping for an hour.
_MAX_RETRY_AFTER_SEC = 300.0


class ChessComError(Exception):
    """The API could not give us what we asked for."""


class NotFound(ChessComError):
    pass


class PlayerNotFound(NotFound):
    pass


class ChessComUnavailable(ChessComError):
    """Retries exhausted on 429s, 5xx responses or network errors."""


@dataclass(frozen=True)
class MonthResponse:
    """One month of games, or a 304 saying it has not changed since `etag`."""

    not_modified: bool
    etag: str | None
    last_modified: str | None
    games: list[dict[str, Any]] = field(default_factory=list)


def normalize_username(raw: str) -> str:
    """Lowercase and validate. Chess.com usernames are case-insensitive."""
    username = raw.strip().lower()
    if not _USERNAME.match(username):
        raise ValueError(f"not a valid Chess.com username: {raw!r}")
    return username


class ChessComClient:
    def __init__(
        self,
        *,
        user_agent: str,
        limiter: RateLimiter,
        base_url: str = "https://api.chess.com/pub",
        timeout_sec: float = 30.0,
        max_retries: int = 5,
        backoff_base_sec: float = 1.0,
        backoff_cap_sec: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._limiter = limiter
        self._max_retries = max_retries
        self._backoff_base = backoff_base_sec
        self._backoff_cap = backoff_cap_sec
        self._sleep = sleep
        self._rng = rng
        self.requests_sent = 0
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout_sec,
            transport=transport,
        )

    # --- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> ChessComClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- endpoints ----------------------------------------------------------

    def profile(self, username: str) -> dict[str, Any]:
        resp = self._get(f"player/{normalize_username(username)}", not_found=PlayerNotFound)
        return resp.json()

    def archive_months(self, username: str) -> list[str]:
        """Every month the player has games in, as sorted 'YYYY-MM' strings."""
        resp = self._get(
            f"player/{normalize_username(username)}/games/archives", not_found=PlayerNotFound
        )
        months = []
        for url in resp.json().get("archives", []):
            match = _ARCHIVE_URL.search(url)
            if match is None:
                # The month is parsed out of a URL whose format we do not
                # control. If it ever changes, fail loudly: skipping unknown
                # entries would quietly drop whole months of history.
                raise ChessComError(f"unrecognised archive URL: {url!r}")
            months.append(f"{match.group(1)}-{match.group(2)}")
        return sorted(months)

    def month(self, username: str, month: str, *, etag: str | None = None) -> MonthResponse:
        """One month of games. With `etag`, an unchanged month costs a 0-byte 304.

        Only the ETag is replayed. Chess.com's Last-Modified is not in HTTP date
        format ('Sunday, 06-Jul-2025 21:04:08 GMT+0000'), so it is stored for
        reference but never sent back as If-Modified-Since.
        """
        if not _MONTH.match(month):
            raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
        year, mm = month.split("-")
        headers = {"If-None-Match": etag} if etag else {}
        resp = self._get(f"player/{normalize_username(username)}/games/{year}/{mm}", headers)
        if resp.status_code == 304:
            return MonthResponse(
                not_modified=True,
                etag=resp.headers.get("etag", etag),
                last_modified=resp.headers.get("last-modified"),
            )
        body = resp.json()
        games = body.get("games") if isinstance(body, dict) else None
        if not isinstance(games, list):
            # Never read a malformed 200 as "no games": a finished month would
            # be frozen empty and never fetched again.
            raise ChessComError(f"month {month} for {username}: response has no 'games' list")
        return MonthResponse(
            not_modified=False,
            etag=resp.headers.get("etag"),
            last_modified=resp.headers.get("last-modified"),
            games=games,
        )

    # --- transport ----------------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with "equal jitter": half fixed, half random.

        Full jitter (random over the whole window) can retry almost instantly,
        which is the wrong response to being told to slow down. Equal jitter
        keeps a floor while still spreading workers out so they do not retry in
        lockstep.
        """
        window = min(self._backoff_cap, self._backoff_base * 2**attempt)
        return window / 2 + self._rng() * window / 2

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        value = resp.headers.get("retry-after")
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            return None  # HTTP-date form; fall back to our own backoff
        return min(max(seconds, 0.0), _MAX_RETRY_AFTER_SEC)

    def _get(
        self,
        path: str,
        headers: dict[str, str] | None = None,
        *,
        not_found: type[NotFound] = NotFound,
    ) -> httpx.Response:
        last_problem = ""
        for attempt in range(self._max_retries + 1):
            # Every attempt, retries included, spends a token: a retry is a
            # request as far as Chess.com is concerned.
            self._limiter.acquire()
            self.requests_sent += 1
            try:
                resp = self._http.get(path, headers=headers)
            except httpx.TransportError as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
                delay = self._backoff(attempt)
            else:
                status = resp.status_code
                if status in (200, 304):
                    return resp
                if status == 404:
                    raise not_found(f"404 for {path}")
                if status == 429:
                    delay = self._retry_after(resp) or self._backoff(attempt)
                    self._limiter.penalize(delay)
                    last_problem = "429 Too Many Requests"
                elif status >= 500:
                    delay = self._retry_after(resp) or self._backoff(attempt)
                    last_problem = f"{status} from server"
                else:
                    # Any other 4xx is our bug, not a transient condition, and
                    # retrying it would just spend rate budget.
                    raise ChessComError(f"{status} for {path}: {resp.text[:200]}")

            if attempt == self._max_retries:
                break
            log.warning(
                "chesscom.retry",
                path=path,
                attempt=attempt + 1,
                problem=last_problem,
                delay_sec=round(delay, 2),
            )
            self._sleep(delay)

        raise ChessComUnavailable(
            f"{path}: gave up after {self._max_retries + 1} attempts ({last_problem})"
        )
