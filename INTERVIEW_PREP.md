# Interview Prep: decision log

Every meaningful change and design decision in this project, with the reasoning, the
alternatives that were rejected, and how to talk about it. **Newest entry first.**

This exists because a project is only worth putting on a resume if you can defend it. Read
the "Talking point" lines before an interview; read the "Why" lines when you need to actually
understand the system.

**Entry format:** layers that get more technical as you read down, so you can stop at the
depth the conversation needs.

1. **In plain English** - written for a 10th grader. No jargon, no acronyms, short
   sentences, an everyday comparison where one helps. This is the recruiter-call and
   behavioural-interview version, and it must make sense on its own.
2. **A bit deeper** - for a hiring manager or an engineer outside this specialty. Names the
   real concepts (cache, queue, lock) and explains each in a sentence, still without code.
3. **The engineering detail** - for the engineer on the panel: What changed / Why /
   Alternatives rejected / Talking point / Where.

Read just the plain-English lines to re-load the whole project in two minutes. Read the rest
when you need to defend a specific decision. (Entries written before M1 have layers 1 and 3
only.)

**Where new entries go:** each milestone has its own block below, marked with HTML comments.
Add an entry at the top of your milestone's block. Separate blocks mean work on different
milestones, done in parallel on different branches, merges without conflicts.

**Status tags:** `DONE` = in the code. `DECIDED` = agreed, not built yet.

---

## The 60-second pitch

> "It pulls my whole Chess.com history - about 4,000 games - and runs every position through
> Stockfish to find where I actually lose rating. The interesting part isn't the chess, it's
> that analyzing 220,000 positions is hours of CPU time, so it's a job queue feeding a pool
> of worker processes, with a position-level cache in front of the engine, and an API that
> only ever reads finished results so the site stays fast while the backlog drains."

Numbers to fill in as they are measured: games analyzed, cache hit rate, games/minute at
1/2/4/8 workers, engine seconds saved by the cache.

Measured so far: **4,550 games** ingested for `deitythemenace` across 53 months in 55 seconds
at one request per second; win/loss/draw per time control matches Chess.com's own totals
exactly; a re-sync costs **2 requests**.

---

<!-- M4 ENTRIES BELOW (report SQL, report and sync API). Newest first. -->

## 2026-11-06 - Which openings cost me rating: a window function, checked against real data `DONE`

> **In plain English:** One question the report answers is "which openings lose me rating
> points?" Chess.com stores my rating *after* each game, not how much it changed. So the
> change for a game is "my rating after this game, minus my rating after the game before
> it". The "game before it" has to be the previous *rated* game of the *same* kind (blitz
> with blitz), in time order - including games whose opening has no name, even though those
> are left out of the openings list itself. I wrote that as a database query, tested it on
> small made-up data where I worked out every answer by hand, and then ran it on my real
> 4,549 games. It matched the numbers the dashboard was already built with, exactly, for
> every time control.

**A bit deeper:** A *window function* (`LAG`) looks at the previous row in an ordered set
without collapsing rows the way `GROUP BY` does - here, the previous rated game in the same
time class, ordered by time (and by id, so two games at the same second still have a fixed
order). The change is computed over every game *first* and only then filtered and grouped
by opening, because filtering first would make the "previous game" skip games and inflate
the changes.

**What changed:** `src/chess_coach/analytics/report.py` - `summary`, `rating_trend` and
`openings`, the report sections that come from ingested games and are complete as soon as a
sync is. `analytics/progress.py` holds the "current rating" query, shared so the progress page
and the report can never disagree about it.

**Why the details are the way they are:**

- *Window first, filter second.* A game with no opening name is not an opening row, but it is
  still the previous game for the next one's rating change. A test puts an unnamed game
  between two Italian Games and checks the change is measured from it.
- *Ties break on id, everywhere.* The rating change's `LAG`, "current rating" and "the last
  game of the day" all order by `(played_at, id)`, so no two of them can disagree about which
  game came last.
- *The UTC day, written out.* `(played_at AT TIME ZONE 'UTC')::date`, and a test runs the query
  in a Los Angeles session to show a 02:00 UTC game still lands on its UTC day. Chess.com's own
  monthly archives are filed in US Pacific time, which is why the contract has no month field.
- *Percentages are computed in Python from integer counts.* Postgres `numeric` division returns
  20 significant digits (`51.0392609699769053`), which is not the float the contract promises.

**Verified against real data:** a read-only run against the development database
(`deitythemenace`, 4,549 standard games) compared every summary count, current rating,
rating-trend point and opening row with the dashboard's real-data fixture: blitz 433 games /
141 trend days / 15 opening rows, bullet 3,690 / 907 / 50, rapid 424 / 161 / 17, daily 2 / 2 / 0.
Zero differences.

**Alternatives rejected:**
- *Compute rating change in Python*: pulls every game of a time class over the wire to do what
  one window function does next to the data.
- *Filter out unnamed openings before the `LAG`*: simpler SQL, wrong numbers.

**Talking point:** "Chess.com gives you the rating after each game, not the change, so the
change is a LAG over the previous rated game in the same time class. The subtle part is the
order of operations: the window has to run over every game before you filter to games with a
named opening, or the 'previous game' skips some and the numbers inflate. I tested it on
hand-computed data and then against my real 4,549 games, where it matched the dashboard's
fixture exactly."

**Where:** `src/chess_coach/analytics/report.py`, `src/chess_coach/analytics/progress.py`,
`tests/test_report_games.py`.

## 2026-11-06 - Asking for a sync is a queued job, and a re-sync re-arms the old one `DONE`

> **In plain English:** When someone types their username into the website, the website
> must not go and download their games itself - that takes a minute and has to wait its turn
> behind a strict speed limit, and the website should answer instantly. So the website just
> writes down "please fetch this player" as a job, and a separate background worker does the
> downloading. Two details matter. If the same person clicks twice, there should still be
> only one fetch, so the job is written down once per player and reused. And when an old,
> finished job is reused, its numbered ticket keeps counting up instead of starting again at
> one - otherwise a long-lost worker holding ticket number one could mistake the new job for
> its own.

**A bit deeper:** The API inserts the player and a `sync-player` job and returns `202
Accepted`. A *sync worker* (`chess-coach-worker --kind sync`) claims it, runs the M1 sync and
then enqueues analysis for the games it found. Idempotency is an *upsert* on the job's dedupe
key: if no job exists it is inserted; if one is queued or running nothing happens; if the old
one finished, failed or died, it is *re-armed* back to pending. The `attempts` counter is the
lease's *fencing token*, so re-arming extends the attempt budget instead of resetting it.

**What changed:** `src/chess_coach/jobrunner.py` (the worker loop extracted from the analysis
worker, so both job types share leases, retries and outage handling),
`src/chess_coach/ingest/jobs.py` (`enqueue_sync`, `SyncWorker`), `--kind sync` on the worker
CLI, a `sync-worker` compose service, and the reaper can be limited to one runner's job types.

**Why the details are the way they are:**

- *One job row per player, re-armed.* The dedupe key is unique for ever, so "a second sync
  later" cannot be a second row. `INSERT ... ON CONFLICT DO UPDATE ... WHERE status IN ('done',
  'failed', 'dead')` gives "enqueue unless one is queued or running" in one atomic statement:
  two simultaneous clicks cannot create two syncs.
- *Never reset the fencing token.* The obvious re-arm sets `attempts = 0`, and I nearly wrote
  it - it looks like simple bookkeeping. That would let the next claim be attempt 1 again - the same (job, worker, attempt) triple a zombie from
  the first run could still hold. It now sets `max_attempts = attempts + JOB_MAX_ATTEMPTS`, and
  a test shows the old run's lease is refused after a re-arm.
- *The sync heartbeats from inside the work, through the HTTP client.* `sync_player` has no
  progress hook, so the worker hands it a thin wrapper around the Chess.com client that renews
  the lease before every request. Every request is progress, so this is the same rule as the
  analysis worker's heartbeat between positions - never a background thread.
- *A missing account is a permanent failure.* The API never calls Chess.com, so it cannot know
  a username is a typo. The sync records it on the player (`sync_status = 'error'`, shown by
  the progress page) and the job goes to `failed`, not round the retry loop five times. Chess.com
  being down (`ChessComUnavailable`) is retried with backoff.
- *Each worker kind reaps only its own job types*, so the hook that repairs a reaped job's
  domain status (a game back to `pending`, a player out of `syncing`) is always the right one.

**Alternatives rejected:**
- *Call Chess.com from the API request*: a minute-long request, competing with every sync
  worker for the one-request-per-second budget, and the API stops being read-only.
- *Insert a new sync job each time, with no dedupe key*: two clicks, two syncs; the advisory
  lock would stop them running at once, but one would fail and retry for nothing.
- *A separate CLI for the sync worker*: the loop, the signals and the flags are the same; only
  the job type differs.

**Talking point:** "The website's only write is 'please sync this player', and it's a job, not
a call to Chess.com. Clicking twice is one atomic upsert, so it's still one job. When a finished
job is re-armed I deliberately don't reset its attempt counter, because that counter is the
fencing token for leases - resetting it would let a stale worker's old lease match the new run."

**Where:** `src/chess_coach/ingest/jobs.py`, `src/chess_coach/jobrunner.py`,
`src/chess_coach/worker.py`, `tests/test_sync_jobs.py`.

<!-- end of M4 entries -->

---


