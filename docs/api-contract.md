# Report API contract (M4 ⇄ M5)

The dashboard (M5, `web/`) is built against this document; the report API (M4) implements
it. The TypeScript mirror is [`web/src/lib/api-types.ts`](../web/src/lib/api-types.ts) -
if the two ever disagree, this document wins and the types are the bug.

Three endpoints: one to add a player, one for sync progress, one for the report. The
report is one call per time control because every view is scoped to exactly one time
control and the dashboard never shows two at once (DESIGN.md: "results are never mixed
across time controls").

## Conventions

| Thing | Rule |
|---|---|
| Base URL | No prefix, same as `/health`. The dashboard reads it from `NEXT_PUBLIC_API_URL` (e.g. `http://localhost:8000`). |
| Usernames | Case-insensitive. The API lowercases and trims before lookup, and always returns the lowercased form. Valid: `^[a-z0-9_-]{1,50}$` after lowercasing (the same rule as `chesscom/client.py`). |
| `time_class` | One of `bullet`, `blitz`, `rapid`, `daily` (`models.TIME_CLASSES`). |
| Timestamps | UTC, ISO 8601, with a `Z` suffix: `2026-11-06T02:14:05Z`. |
| Calendar dates | `YYYY-MM-DD`, the **UTC** day. |
| Ratings | Integers, Chess.com rating points. |
| Rating changes | Signed integers, rating points. Negative = rating lost. |
| Percentages | Floats on a **0-100** scale (`52.3`, never `0.523`), not rounded. The UI rounds for display. |
| Counts | Non-negative integers. |
| Engine evaluations | **No field in this contract is in centipawns or pawns.** Evals are turned into classifications (blunder etc.) in SQL. If a field carrying an eval is ever added, it is an integer in centipawns from the mover's point of view, named `*_cp`. |
| Missing values | `null`, never omitted and never `0` standing in for "unknown". Every key in a shape is always present. |
| Which games count | Reports use `rules = 'chess'` only. Variants (chess960 and the like) have their own Chess.com ratings and cannot be graded by Stockfish; counting them would make our totals disagree with the player's Chess.com stats page. Progress counts include them (as `skipped`), so progress totals match what was ingested. |
| Errors | FastAPI's default shape, `{"detail": ...}`. `detail` is a string for 404 and 501, and FastAPI's validation array for 422. The UI shows a generic message for arrays. |
| CORS | Allow `http://localhost:3000` (dev) and `http://localhost:3001` (the reviewer), `GET` and `POST`, header `Content-Type`. |

---

## `POST /players`

Start tracking a player. The landing page's "Analyze my games" button.

Request body: `{"username": "DeityTheMenace"}`

- **202 Accepted** with a [`PlayerProgress`](#playerprogress) body. Enqueues a sync if none is
  queued or running. Idempotent: posting a tracked player again returns its current progress
  (and enqueues a re-sync, which costs two Chess.com requests when nothing is new).
- **422** if the username fails the pattern above.

The API does not call Chess.com here, keeping the rule in `api/main.py` that the API only
reads Postgres and enqueues. So it cannot know whether the account exists: a username with
no Chess.com account comes back 202, and the sync worker later sets `sync_status = "error"`
with a `sync_error` the progress page shows.

> **Open question for the lead:** this is the API's only write. It inserts a `players` row
> and a sync job, which needs a `sync` job type on M2's queue. If that is out of scope for
> M4, return **501** and the landing page will say "Run `chess-coach-sync <username>`".

## `GET /players/{username}`

The player and their sync and analysis progress. Polled by the progress page (every 5 s
while work remains) and read once by the overview to label the time-control switcher.

- **200** [`PlayerProgress`](#playerprogress)
- **404** `{"detail": "Player not found"}` if the player is not tracked.

### `PlayerProgress`

| Field | Type | Meaning |
|---|---|---|
| `username` | string | Lowercased. |
| `sync_status` | `"pending" \| "syncing" \| "complete" \| "error"` | `players.sync_status`. Ingestion only; analysis has its own counts below. |
| `sync_error` | string \| null | `players.sync_error`. Shown verbatim when `sync_status = "error"`. |
| `last_synced_at` | timestamp \| null | `players.last_synced_at`. Null until the first sync finishes. |
| `analyzed_last_hour` | integer | Games of this player with `analyzed_at` in the last 60 minutes, all time controls. The UI turns it into "about N hours left". The API does not estimate, because it cannot know how many workers will run. |
| `time_classes` | `TimeClassProgress[]` | **Always four entries**, in the order bullet, blitz, rapid, daily, including classes with zero games. A fixed shape means the UI never has to guess whether a missing class means "zero" or "not loaded". |

### `TimeClassProgress`

| Field | Type | Meaning |
|---|---|---|
| `time_class` | TimeClass | |
| `games` | integer | Games ingested in this class, variants included. |
| `analyzed` | integer | `analysis_status = 'complete'`. |
| `pending` | integer | `analysis_status IN ('pending', 'running')`. One bucket because "queued" and "on a worker right now" look the same to a player. |
| `failed` | integer | `analysis_status = 'error'`, i.e. dead-lettered. |
| `skipped` | integer | `analysis_status = 'skipped'` (variants Stockfish cannot grade). |
| `current_rating` | integer \| null | `player_rating` of the most recent **rated** standard game in this class. Null if there is none. |

Invariant, which a test should check: `analyzed + pending + failed + skipped = games`.

## `GET /players/{username}/report?time_class={time_class}`

Everything the overview shows for one time control.

- `time_class` is **required**; missing or invalid is a **422**.
- **200** [`Report`](#report). A player with zero games in the class still gets a 200 with
  zeros, empty arrays and nulls, so the UI renders an empty state rather than an error.
- **404** if the player is not tracked.

Sections built from ingested games (`summary` except blunders, `rating_trend`, `openings`)
are complete as soon as the sync is. Sections built from engine analysis (`blunders_per_game`,
`blunders_by_phase`, `time_pressure`) cover only analyzed games and grow as the backlog
drains; `analyzed_games` says how many. This is what lets a new user see a report before
the analysis finishes.

### `Report`

| Field | Type | Meaning |
|---|---|---|
| `username` | string | |
| `time_class` | TimeClass | Echoes the query. |
| `generated_at` | timestamp | When the API computed this. |
| `games` | integer | Standard-chess games in this class, rated and unrated. |
| `analyzed_games` | integer | Of `games`, how many have `analysis_status = 'complete'`. The sample size of every move-level field. |
| `summary` | `Summary` | Headline numbers. |
| `rating_trend` | `RatingPoint[]` | Oldest first. Empty if there are no rated games. |
| `openings` | `OpeningResult[]` | See below. Empty if no opening reaches 5 games. |
| `blunders_by_phase` | `PhaseBlunders[] \| null` | Null when `analyzed_games = 0`. Otherwise exactly three entries: opening, middlegame, endgame, in that order. |
| `time_pressure` | `TimePressure \| null` | Null when `analyzed_games = 0`, **and always null for `daily`**, where clocks run in days and "under 30 seconds" means nothing. The UI tells these apart by `time_class`. |

### `Summary`

| Field | Type | Meaning |
|---|---|---|
| `wins`, `draws`, `losses` | integer | Sum to `games`. |
| `win_rate_pct` | float \| null | `wins / games * 100`. Draws are not half-wins here, so the number matches the one on Chess.com's stats page. Null when `games = 0`. |
| `current_rating` | integer \| null | As in `TimeClassProgress`. |
| `blunders_per_game` | float \| null | The player's own blunders (`is_player_move` and `classification = 'blunder'`) across analyzed games, divided by `analyzed_games`. Null when `analyzed_games = 0`. |

### `RatingPoint`

One point per UTC day on which the player finished at least one rated game in this class.

| Field | Type | Meaning |
|---|---|---|
| `date` | date | The UTC day. |
| `rating` | integer | `player_rating` of the **last** rated game that day. |
| `games` | integer | Rated games that day. |

Daily rather than per game: 3,690 bullet games would be 3,690 points for a line that is a few
hundred pixels wide, and a player asks "am I improving this month", not "what was I after
game 2,817". Per day keeps the answer and costs about 900 points for four years of bullet.

### `OpeningResult`

"Which openings cost me rating?" One row per (opening family, colour) with at least
**5 games**, sorted by `rating_change` ascending (most costly first), then `games`
descending, then `opening`, then `color`.

| Field | Type | Meaning |
|---|---|---|
| `opening` | string | The opening **family** (rule below). |
| `color` | `"white" \| "black"` | The player's colour. Separate rows, because how you do with the Caro-Kann as Black has nothing to do with meeting it as White. |
| `games`, `wins`, `draws`, `losses` | integer | Standard games, rated and unrated. |
| `win_rate_pct` | float | `wins / games * 100`. |
| `rating_change` | integer | Sum of the per-game rating change over the rated games in the group. |

**Per-game rating change.** `games.player_rating` is the rating *after* the game (checked
against the real data: in rapid the average change from the previous rated game is +10.9
after a win and -13.1 after a loss). So a game's change is its `player_rating` minus the
previous rated standard game's `player_rating` in the same time class,
`LAG(player_rating) OVER (PARTITION BY time_class ORDER BY played_at, id)` over rated games.
The first rated game in a class has no previous game and contributes 0.

**Opening family.** Chess.com names are too fine-grained to group by (the dev data has 111
names in 434 blitz games, "Queens Pawn Opening Zukertort Variation" and "... Zukertort
Chigorin Variation" among them). The family is the shortest prefix of `opening_name` that
ends in one of the words Opening, Defense, Defence, Game, Gambit, System or Attack, and the
whole name if none does:

```sql
COALESCE(
  substring(opening_name FROM '^(.*?(Opening|Defense|Defence|Game|Gambit|System|Attack))( |$)'),
  opening_name)
```

"Queens Pawn Opening Zukertort Variation" becomes "Queens Pawn Opening"; "Indian Game London
System" becomes "Indian Game"; "Englund Gambit" stays. Games whose `opening_name` is NULL or
`Undefined` are left out of this section (and only this section).

### `PhaseBlunders`

"In which part of the game do I blunder?"

| Field | Type | Meaning |
|---|---|---|
| `phase` | `"opening" \| "middlegame" \| "endgame"` | `moves.phase`, as M2 assigns it. |
| `player_moves` | integer | The player's own classified moves in this phase, across analyzed games. |
| `blunders` | integer | Of those, `classification = 'blunder'`. |

Counts rather than rates, so there is one definition of each rate and it is in the UI:
blunders per game is `blunders / analyzed_games` (the three sum to the headline
`blunders_per_game`) and blunders per 100 moves is `blunders / player_moves * 100` (which
corrects for the middlegame simply being longer).

### `TimePressure`

"Do I fall apart when the clock runs down?" The player's own classified moves split by the
time they had **when they started the move**: under 30 seconds, or 30 seconds and over.

| Field | Type | Meaning |
|---|---|---|
| `threshold_seconds` | integer | Always `30`. Returned so the UI never hard-codes it. |
| `low_clock_games` | integer | Analyzed games in which the player started at least one move under the threshold. |
| `low_clock` | `ClockBucket` | Moves started with less than 30 s. |
| `normal_clock` | `ClockBucket` | Moves started with 30 s or more. |

`ClockBucket`:

| Field | Type | Meaning |
|---|---|---|
| `moves` | integer | Moves in the bucket. |
| `accuracy_pct` | float \| null | Moves classified `best` or `good` / `moves` * 100. Null when `moves = 0`. Defined from our own classifications, not Chess.com's accuracy score, which we do not compute and do not have per move. |
| `blunders` | integer | Moves classified `blunder`. |

Time on the clock when a move was started: `moves.clock_seconds` is the reading *after*
the move, and the opponent's thinking does not spend the player's clock, so it is the
`clock_seconds` of the player's previous move (`LAG` over the player's own moves in the game),
or the base time from `time_control` for their first move. Moves with no clock reading are in
neither bucket.

---

## Example

`GET /players/deitythemenace/report?time_class=rapid` (abridged):

```json
{
  "username": "deitythemenace",
  "time_class": "rapid",
  "generated_at": "2026-11-06T03:20:00Z",
  "games": 424,
  "analyzed_games": 424,
  "summary": {
    "wins": 242, "draws": 29, "losses": 153,
    "win_rate_pct": 57.075471698113205,
    "current_rating": 946,
    "blunders_per_game": 0.94
  },
  "rating_trend": [
    {"date": "2022-07-07", "rating": 812, "games": 3}
  ],
  "openings": [
    {"opening": "Caro Kann Defense", "color": "black", "games": 31,
     "wins": 13, "draws": 2, "losses": 16, "win_rate_pct": 41.9, "rating_change": -54}
  ],
  "blunders_by_phase": [
    {"phase": "opening", "player_moves": 4210, "blunders": 38},
    {"phase": "middlegame", "player_moves": 7905, "blunders": 231},
    {"phase": "endgame", "player_moves": 3550, "blunders": 129}
  ],
  "time_pressure": {
    "threshold_seconds": 30,
    "low_clock_games": 61,
    "low_clock": {"moves": 702, "accuracy_pct": 58.1, "blunders": 41},
    "normal_clock": {"moves": 14963, "accuracy_pct": 84.2, "blunders": 357}
  }
}
```

Values in this example are illustrative. The dashboard's fixtures
(`web/src/lib/fixtures/`) hold the real ingested numbers for `deitythemenace` and
synthetic move-level numbers, labelled as such.
