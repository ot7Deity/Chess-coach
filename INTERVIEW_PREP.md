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

<!-- M3 ENTRIES BELOW (position cache, scaling, metrics). Newest first. -->

<!-- end of M3 entries -->

---

<!-- M2 ENTRIES BELOW (job queue, Stockfish, analysis worker). Newest first. -->

## 2026-11-06 - The sixth time: the engine's memory size was a hidden input to the cache `DONE`

> **In plain English:** The chess engine has a setting for how much scratch memory it may
> use while thinking. I had assumed that only changes how *fast* it thinks, not *what* it
> concludes. I measured it anyway, and it does change the answer - rarely and slightly (one
> position in six, by a fiftieth of a pawn), but it does. The saved answers were filed under
> a label that named the engine and how deep it thinks, not the memory size. So changing the
> setting would have mixed two slightly different sets of answers under one label. The label
> now includes the memory size. And a second setting I had only *warned* about - letting the
> engine think on several processor cores at once - is now refused outright, because it
> makes the answer change from run to run, and no label can fix that.

**A bit deeper:** The cache key is (position, engine version, depth). With the hash table
cleared before every search, the remaining engine setting that changes a fixed-depth result
is the *hash size*: a smaller table evicts entries during the search and can take a slightly
different path. The fix extends the *engine version* part of the key, rather than adding a
column, so the positions table, the cache protocol and every caller stay as they are.

**Symptom / how it was found:** not a production failure - a measurement taken while building
the engine wrapper. At depth 16 on six positions from one game, Hash = 16 MB gave `-45` where
64, 128 and 256 MB all gave `-43`; the other five positions agreed. I logged it as an open
question rather than acting on it; the decision was to fold it into the key.

**Root cause:** the same one as five times before in this project - a stored value depending
on an input its key does not name. (The others: engine version and depth in `positions`; the
CI engine cache's probe and host; the engine image's CPU; the games table's player; and the
engine's transposition table and halfmove clock.)

**Fix:** `StockfishEngine.version` is now `sf_18-h128` (release plus hash size), built by
`engine_version(name, hash_mb=...)`; the plain release (`sf_18`) is kept as `.release` for
logs. `ENGINE_THREADS` other than 1 is now rejected - by `Settings` and by the wrapper -
instead of logging a warning, because multi-threaded search gave -186 and -213 for one
position on two runs, and thread timing is not something any key can name.

**Cost:** none at run time. Changing `ENGINE_HASH_MB` now starts a fresh set of cache entries
instead of silently reusing the old ones - which is the point.

**Alternatives rejected:**
- *Pin the hash size and document it*: a rule that holds until someone tunes memory for a
  benchmark, and then fails silently.
- *Add a `hash_mb` column to the key*: correct, but a schema and protocol change across M2 and
  M3 for what the version string already expresses.
- *Ignore a 2 cp difference*: it would never change a classification today, but "never
  matters" is a claim about every future position, and the fix is one string.

**Lesson:** the third time it was caught by reasoning, this time by a measurement taken just
to check an assumption ("hash size only affects speed"). Measuring the boring assumption is
cheap; finding a mixed cache later is not.

**Talking point:** "Six times now the bug has been the same: a stored value depends on an
input its key doesn't name. The latest one I found by checking an assumption I was fairly
sure of - that the engine's hash size only changes speed. It changed one score in six by 2
centipawns. Tiny, but it means two hash sizes would share cache entries that disagree, so the
hash size is now part of the engine version. And multi-threading went from a warning to an
error, because thread timing is an input no key can capture."

**Where:** `src/chess_coach/engine.py` (`engine_version`, `StockfishEngine.version` and
`.release`), `src/chess_coach/config.py`, `.env.example`, `tests/test_engine.py`,
`tests/test_config.py`.

## 2026-11-06 - The container proved the "probe at start" decision on my own laptop `DONE`

> **In plain English:** Earlier I decided the chess engine must be chosen when the
> container starts, on the machine that runs it, not when the package is built - because the
> fastest version crashes on processors that lack one special feature. I expected that to
> matter on *other* machines. The first time I ran the container on my own laptop, the
> fastest version crashed right there. My laptop's processor has the feature, but the little
> virtual computer Docker runs containers in doesn't pass it through. The installer noticed
> the crash, stepped down one level, and the worker started normally. If the engine had been
> picked while building the package on this same laptop, every container would have crashed.

**A bit deeper:** On Windows, Docker Desktop runs Linux containers inside a lightweight
*virtual machine* (WSL 2). A VM can expose a subset of the host CPU's *instruction set
extensions*. Here the host (Intel Core Ultra 7 155H) supports AVX-VNNI, the VM did not
expose it, and the AVX-VNNI build of Stockfish died with an illegal instruction (exit -4).
The fallback chain picked the AVX2 build.

**What changed:** a `Dockerfile` (one image for the API, migrations and the worker; Stockfish
deliberately absent), `docker/entrypoint.sh` (runs `tools/fetch_stockfish.py`, then `exec`s
the command), and `migrate` / `api` / `worker` services in `docker-compose.yml` behind an
`app` profile, with the engine in a named `vendor` volume.

**What I ran, and what it printed** (first start, empty volume):

```
[fetch-stockfish] trying stockfish-ubuntu-x86-64-avxvnni.tar (109 MiB)
[fetch-stockfish]   smoke test exited -4 (wrong SIMD build for this CPU)
[fetch-stockfish] trying stockfish-ubuntu-x86-64-avx2.tar (109 MiB)
[fetch-stockfish]   OK - Stockfish 18 (searched both evaluation networks)
worker.start depth=8 engine=sf_18
```

The second start re-probed the binary already in the volume ("nothing to do"), and the whole
`docker run` took 3.6 s. Then the container analysed the 90-ply test game at depth 12 in
6.3 s against the test database: `complete`, 90 moves. The engine version is `sf_18` on the
host's AVX-VNNI build and the container's AVX2 build alike, which is what lets their cached
evaluations be shared.

**Why the details are the way they are:**

- *The `app` profile.* `docker compose up -d` is what every developer and the tests already
  run for Postgres and Redis. Adding the app without a profile would quietly start a second
  worker competing for the same queue as the ones on the host.
- *`flock` around the installer.* `--scale worker=4` starts four containers at once on one
  shared volume; without a lock, four installers would download into the same directory at
  once. With it, the first downloads and the rest wait, then find a verified binary.
- *Only the worker probes.* The API and migrations never touch the engine, so they set
  `FETCH_STOCKFISH=0` and start in a second instead of waiting on a download.
- *Dependencies are a separate layer* from the source, so a code change rebuilds in seconds;
  uv is pinned to the version the lock file is maintained with; the app runs as a non-root
  user; `stop_grace_period: 60s` gives SIGTERM time to finish the current game.

**Alternatives rejected:** the ones in the original `DECIDED` entry (fetch at build time, bake
the baseline build, one image per SIMD level) - and this run is the evidence for that call:
build-time selection on this very laptop would have produced an image that crashes on this
very laptop.

**Talking point:** "I'd decided the engine build must be picked on the machine that runs it,
because an image moves between CPUs. I assumed that mattered for other machines. The first
container I ran on my own laptop proved it: the host CPU has AVX-VNNI, Docker's VM doesn't
expose it, the fast build died with an illegal instruction, and the probe fell back to AVX2.
A build-time choice would have shipped an image that crashes on the machine that built it."

**Where:** `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.yml`. The original decision is
the "engine is probed at container start" entry in the pre-M1 block.

## 2026-11-06 - M2: kill a worker mid-game, and the game is still analysed exactly once `DONE`

> **In plain English:** This is the test the whole second milestone was aiming at. I start
> a real worker, let it get properly into analysing a 90-move game, and then kill it
> instantly - no warning, no chance to tidy up, like pulling the plug on a computer. Then I
> start a second worker and tell it to finish whatever is left. It has to notice the first
> worker has gone quiet, take over the game, analyse it from the start, and stop. At the end
> the game has exactly 90 moves saved - not 0, not 180 - it is marked finished once, and the
> logs show only the second worker ever finished it. It took about 8 seconds from the first
> worker's last sign of life to a finished game, and it passed on every one of nine runs.

**A bit deeper:** The test uses real *processes* (not threads pretending), a real Postgres and
a real Stockfish. "Killed hard" means `TerminateProcess` on Windows (`SIGKILL` on Linux): the
process gets no chance to run any cleanup code, which is what a crash, an out-of-memory kill
or a power cut looks like. Recovery then depends only on the *lease* expiring and the
*reaper* handing the job back - the mechanisms in the earlier entries.

**What the test does, step by step** (`tests/test_kill_recovery.py`):

1. Insert a 90-ply game and enqueue it. Start worker A as a subprocess at depth 12 with a
   3-second lock timeout (so it heartbeats every second).
2. Wait until A has claimed the job **and heartbeated at least once** - proof it is in the
   middle of the analysis, not just past the claim - and assert no moves are written yet.
3. Kill A. Then watch the job for 2.5 heartbeat intervals and assert its `locked_at` does not
   move: A really is dead. (This check exists because on Windows `sys.executable` in a venv is
   a launcher that starts the real interpreter as a child; if killing the launcher left the
   child running, the test would say so instead of passing by accident. It doesn't.)
4. Start worker B with `--until-empty`. B waits out A's lease, reaps it, re-claims the job as
   attempt 2, analyses the game and exits 0.
5. Assert: the job is `done`, by B, on attempt 2, and its `last_error` records that A's lease
   expired; the game is `complete` with `ply_count` 90; there are exactly 90 move rows with
   90 distinct plies running 1 to 90, all graded; A's log contains no `job.done`, B's contains
   one `job.reaped` and one `job.done`.

A second test runs three workers over eight games concurrently and asserts every job finished
on its first attempt, every game is complete, and the move count is exactly the sum of the
games' lengths.

**Measured:** last heartbeat from A to game complete: 8.0 s, 8.1 s, 8.3 s across three runs
(3 s of that is the lock timeout, ~2.5 s is the test's own "is A dead" wait, the rest is B
starting and analysing 91 positions). The test passed 9 runs out of 9.

**Why "exactly once" holds, in one paragraph:** a job can be *executed* more than once - that
is what recovery means - but its *effect* is written once. A writes nothing until the end;
its results would commit in the same transaction as `done`, and only if its lease (fenced by
the attempt number) is still current. So a killed A leaves no trace, a stalled-then-revived A
writes zero rows, and B's run is the only one that commits. Moves are upserted on
(game_id, ply) underneath that, so even a hypothetical double commit could not create
duplicate rows.

**Alternatives rejected:**
- *Test with threads and a fake "kill"*: a thread cannot be killed without running its
  `finally` blocks, which is precisely the cleanup a real crash skips.
- *Kill at a random moment*: it would sometimes kill before the claim or after the commit,
  which tests nothing interesting. Waiting for the first heartbeat pins the kill inside the
  window that matters.

**Talking point:** "The acceptance test for the queue kills a real worker process with
SIGKILL in the middle of a 90-move game, after it has heartbeated, so it's genuinely
mid-analysis. A second worker waits out the lease, takes the job over through the reaper and
finishes it. Then I assert 90 rows, 90 distinct plies, the game completed once, and from the
logs, only the second worker ever completed it. Recovery took about 8 seconds with a
3-second lease. It's 'at-least-once execution, exactly-once effect', and the test checks
the effect, not the mechanism."

**Where:** `tests/test_kill_recovery.py`, `src/chess_coach/worker.py` (the CLI the test
launches), and the queue, worker and fencing entries below.

## 2026-11-06 - The queue's defaults lived in Python, where only Python could see them `DONE`

> **In plain English:** Every job has a few settings that are normally left at a standard
> value - how urgent it is, how many tries it has had, how many it is allowed. Those standard
> values were written in my Python code, not in the database. So anything that added a job
> *without* going through my Python code - a speed-test script, or me typing into the
> database by hand - got an error demanding every value spelled out. The person building the
> speed tests spotted it. The fix moves the standard values into the database itself, where
> every tool sees them.

**A bit deeper:** SQLAlchemy's `default=` is applied by the Python library when it builds an
`INSERT`; the database knows nothing about it. `server_default=` puts a `DEFAULT` clause on
the column, which the database applies to every insert from anywhere. The columns are
`NOT NULL`, so a plain-SQL insert that omitted them failed outright.

**What changed:** migration `0003` adds database defaults to `jobs.status` (`'pending'`),
`priority` (100), `attempts` (0) and `max_attempts` (5); the `Job` model declares them too,
so the drift test keeps the two honest. A test inserts a job with only `type` and `payload`
in raw SQL and claims it.

**How it was found:** the M3 builder, writing a benchmark harness that enqueues by SQL, read
the model and asked. My own enqueue SQL had silently worked around it by naming every column
- which is exactly how this kind of gap survives: the first author always knows the trick.

**Why it matters for this table in particular:** the jobs table is the one table other tools
are *expected* to write to directly - it is the integration point between ingestion, the
workers, benchmarks and, later, the API's sync jobs. A default that only one client knows
about is part of that client, not part of the schema.

**Alternatives rejected:** *document "always set these columns"* - a rule every future
script has to remember, enforced by a NOT NULL error at run time.

**Talking point:** "An ORM default is a client-side default. My queue table is written to by
several tools, and only the ORM knew what 'priority' defaulted to - a teammate building the
benchmark hit it from raw SQL. Moving the defaults into the schema made the database the
single source of truth for what a new job looks like."

**Where:** `migrations/versions/0003_jobs_server_defaults.py`, `src/chess_coach/models.py`
(`Job`), `tests/test_queue.py::test_a_job_inserted_with_plain_sql_needs_only_a_type_and_a_payload`.

## 2026-11-06 - The worker: nothing is written until the whole game is done `DONE`

> **In plain English:** A worker takes a game off the queue, has the engine judge every
> position in it, and saves the results. The important choice is *when* it saves. It saves
> nothing while the engine is working, then saves the whole game - every move, the "finished"
> mark on the game, and the "done" mark on the job - in one go. Like a bank transfer, it all
> happens or none of it does. So if a worker is killed halfway through, there is no
> half-finished game to tidy up: the game just goes back on the pile and someone does it
> again from the start. While it works, the worker checks in every so often to say "still
> on it"; if it finds out it has been replaced, it stops and throws its work away.

**A bit deeper:** The write is one *database transaction*, which commits atomically. The
lease check (`queue.complete`) is the first statement in it, so a worker whose lease was
taken away writes zero rows. *Heartbeats* run between positions. Re-running a game is safe
regardless, because moves are *upserted* on (game id, ply) - insert, or overwrite if present.

**What changed:** `analysis/game.py` (replay the PGN onto `games.start_fen` or the normal
start, one evaluation per position, rows for `moves`), `analysis/runner.py` (`Worker`:
reap, claim, analyse, complete; `CachedEvaluator`, the single seam where M3's cache plugs
in), `analysis/jobs.py` (one set-based `INSERT ... SELECT` that enqueues every pending game,
priority by time class), and one call in `ingest/cli.py` so a sync enqueues what it found.

**Why the details are the way they are:**

- *One job per `games.id`, keyed `analyze-game:<id>`, payload `{"game_id": <id>}`.* Not the
  Chess.com uuid: a game between two tracked players is two rows with two points of view,
  and each needs its own moves.
- *`moves.fen_hash` is the position the move was played from*, so it joins to the
  `positions` row holding the engine's best move for that decision.
- *Enqueueing is a sweep, not an event.* It inserts a job for every pending game without
  one, so a crash between "games saved" and "jobs enqueued" is repaired by the next run.
- *A game status is never moved backwards.* Every status update excludes `complete` and
  `skipped`, so a stale job can't un-finish a game.
- *The worker refuses a lock timeout it cannot keep.* Heartbeats happen between positions, so
  one search must fit in the gap: `ENGINE_TIMEOUT_SEC` must be under two thirds of
  `JOB_LOCK_TIMEOUT_SEC`, or the worker will not start.
- *A database outage does not kill the worker.* It logs, waits a poll interval and tries
  again; any job it held simply lapses to the reaper.

**Bugs and surprises found while testing it:**

- *A Postgres type error from reusing one parameter.* The enqueue SQL used `:type` both as the
  `jobs.type` value and inside `:type || ':' || g.id` for the dedupe key. psycopg 3 binds
  parameters server-side, so Postgres had to give `$1` one type, and deduced `varchar` from
  one use and `text` from the other: `AmbiguousParameter: inconsistent types deduced for
  parameter $1`. The fix is one parameter per use. Lesson: with server-side binding, a
  parameter is a typed value, not a text substitution.
- *python-chess does not raise on a bad move.* `read_game("1. e4 e5 2. Ke3")` returns a game
  with two moves and the error tucked into `game.errors`. Grading that would silently drop
  the rest of the game, so any recorded error makes it a permanent `BadGame`. A test I wrote
  with obviously-garbage text passed through as an empty, error-free game - which is why the
  test now uses illegal moves instead.
- *The engine disagreed with my chess.* I wrote a test expecting 2...bxa6 (taking a hung
  bishop) to be "best" or "good". Stockfish prefers 2...Nxa6, which keeps the pawns intact:
  +452 vs +377 for Black at depth 16 (an inaccuracy), 126 cp at depth 8 (a mistake). The test
  now asserts what the engine actually says.

**Alternatives rejected:**
- *Write moves as they are evaluated*: a killed worker leaves half a game, and "is this game
  analysed?" needs a second source of truth.
- *A heartbeat thread*: keeps a hung worker's lease alive forever.
- *Enqueue inside the ingest transaction* (a transactional outbox): stronger, but means
  reworking M1's upsert; the sweep gets the same eventual result with one call.

**Talking point:** "The worker's guarantee is all-or-nothing per game. It computes the whole
game, then one transaction checks it still owns the job, writes every move, marks the game
complete and the job done. A worker killed at any point leaves either nothing or everything,
never half, and a zombie writes zero rows. Moves are upserted on game and ply anyway, as a
second line of defence."

**Where:** `src/chess_coach/analysis/`, `src/chess_coach/ingest/cli.py` (one line),
`tests/test_analysis_game.py`, `tests/test_runner.py`, `tests/games.py`.

## 2026-11-06 - Grading a move: whose point of view, a mate already given, and what the cap really does `DONE`

> **In plain English:** The engine always scores a position for whoever's turn it is. So
> after you move, its score is from your *opponent's* side, and the program has to flip it
> to say how your move went. Flipping is just "change the sign" - except in one case:
> checkmate. The engine writes "already checkmated" as zero, and flipping zero gives zero, so
> "I just checkmated them" would quietly turn into "I've been checkmated". That case is now
> handled on its own, and checkmating someone always counts as the best move. Writing the
> tests also caught me misreading my own rule: I'd assumed capping scores at ten pawns made a
> slip in an already-lost game count for nothing. For the example in my own notes it doesn't
> - it shrinks a "blunder" to a "mistake". The tests now say what the rule really does.

**A bit deeper:** Engine scores are *relative* (side-to-move point of view); the report needs
them from the *mover's* point of view, which is a negation for the position after the move.
A *mate score* is a distance ("mate in 3"), signed by who is mating, and *mate in 0* is the
checkmate itself. Classification *clamps* both scores to +/-1000 centipawns before
subtracting, so swings beyond the cap stop counting - but only the part beyond the cap.

**What changed:** `src/chess_coach/analysis/classify.py` (`MoverScore`, `mover_before`,
`mover_after`, `classify`, `classify_move`, `phase`) and `analysis/clock.py`
(`parse_time_control`, `move_times`), with 51 unit tests.

**The two things the tests caught:**

- *Mate in 0 cannot be negated.* `mover_after` special-cases it: the opponent is to move and
  mated, so the mover has delivered mate (`mate_in_after = 0` in mover terms), which clamps to
  +1000. `classify_move` then calls any mating move `best` outright - otherwise, with two mates
  in one on the board, the one the engine didn't name would be graded on arithmetic. The
  reviewer flagged this independently; the tests cover a mating move from a lost position and
  from a position where the engine preferred a different mate.
- *My reading of the clamp was wrong, not the code.* The decision log's example, -900 -> -1300,
  is a 400 cp drop raw. Clamped, it is -900 -> -1000: 100 cp, a **mistake**. I had written the
  test expecting "good". The clamp fully erases swings that are beyond the cap on *both* sides
  (-1100 -> -1500 is "good"), and softens ones that cross it. That is the rule the project
  decided on, so the code stays and the test now documents it honestly.

**Other details:** the engine's own move is `best` whatever the arithmetic says (the score
after a move is a fresh, deeper search and can differ by more than noise); thresholds are
inclusive (exactly 300 cp is a blunder); phase is endgame by material alone (non-pawn material
<= 26 of the starting 62), opening for moves 1-10, middlegame otherwise. Move times use each
player's own previous clock reading plus the increment, never go negative, and are `None`
rather than guessed when a reading is missing or the game is daily.

**Alternatives rejected:**
- *Encode a delivered mate as +1 or a sentinel*: changes the meaning of `moves.mate_in_after`
  for every consumer; an explicit branch in one function is cheaper to explain.
- *Change the test to match my assumption by tightening the clamp*: the clamp value is a
  recorded decision, and the raw evals are stored precisely so it can be revisited with real
  data.

**Talking point:** "Point of view is where move grading goes wrong. The engine scores for the
side to move, so the 'after' score is the opponent's and has to be negated - except checkmate,
which the engine writes as mate-in-zero, and negating zero is still zero. So 'I gave mate'
would have read as 'I got mated'. And my own tests caught me misreading my own clamping rule:
it softens a slip in a lost game from blunder to mistake, it doesn't erase it."

**Where:** `src/chess_coach/analysis/classify.py`, `src/chess_coach/analysis/clock.py`,
`tests/test_classify.py`, `tests/test_clock.py`.

## 2026-11-06 - Bug: my timeout fix made the test run hang forever, one run in four `DONE`

> **In plain English:** If the chess engine freezes, my program is supposed to give up on it
> after a set time, shut it down and start a fresh one. The test for that passed. But
> roughly one time in four, the whole test run then never finished - it just sat there after
> printing that every test had passed. The cause was how I shut the frozen engine down. I
> first told the program "forget about the question you asked", then pulled the plug. Doing
> it in that order confused the library that talks to the engine, so it never noticed the
> engine had actually gone, and kept a helper waiting for it - forever. The program refuses
> to exit while a helper is still waiting. The fix was to just pull the plug, and let the
> unanswered question fail on its own.

**A bit deeper:** python-chess runs each engine on a background *thread* with its own *event
loop* (the machinery that waits on the engine's output). Python will not exit while a
*non-daemon* thread is still running. Cancelling the pending request and then killing the
process put the library's internal state machine into a state where the "process exited"
notification itself crashed (`InvalidStateError`), so the background thread waited for an
exit it would never be told about.

**Symptom:** `pytest tests/test_engine.py` sometimes printed every test passing and then hung.
Run alone, every test passed. The first full run sat for over three minutes before I killed it.

**How I found it:** pytest showed nothing - the tests had finished. A loop that ran the hang,
crash and normal scenarios eight times, with `faulthandler.dump_traceback_later`, reproduced
it on every iteration rather than one in four: each "hang" case took 6.2 s instead of ~1.2 s,
logged my own `engine.kill_unconfirmed` warning (the 5 s wait for the process's exit code ran
out), printed `InvalidStateError` from python-chess's `connection_lost`, and the traceback
dump at the end showed one leaked engine event-loop thread per timeout, plus the main thread
parked in `threading._shutdown`. The "one in four" in pytest was just how often the leaked
thread was still alive when the interpreter tried to exit.

**Root cause:** `future.cancel()` before killing the process. Cancelling the analysis coroutine
left python-chess's command half-torn-down; when the kill then arrived, its termination
handler tried to set a result on a future that was already cancelled.

**Fix:** don't cancel; kill the process, and let the in-flight command fail with the ordinary
"engine terminated" error, which python-chess handles cleanly. The same eight-iteration loop
now runs each hang case in 1.2 s with no warnings and exits. The hang test now also asserts
that the thread count returns to where it started after the engine is closed.

**Lesson:** "the test passed" and "the test run finished" are different claims - a resource
leak shows up as the process not ending, not as a red test. And when a library gives you two
ways to stop something, the more forceful one can be the cleaner one: killing the process
goes through the library's well-tested crash path, while cancelling went through an
interleaving it had never been tested for.

**Talking point:** "My hung-engine timeout leaked a thread. The test was green; the test *run*
hung one time in four. The cause was cancelling the request before killing the engine,
which left the library unable to process the exit. I found it by looping the scenario with a
traceback dump on a timer, which showed a leaked event-loop thread per timeout. Killing
without cancelling fixed it, and the test now checks the thread count goes back down."

**Where:** `src/chess_coach/engine.py` (`StockfishEngine.evaluate`, the comment on the
timeout branch), `tests/test_engine.py::test_a_hung_search_is_killed_restarted_and_reported`.

---

## 2026-11-06 - The same position, the same depth, three different answers `DONE`

> **In plain English:** The project saves the engine's opinion of each chess position so it
> never has to work it out twice. That only makes sense if asking twice would give the same
> answer. It didn't. The engine keeps notes from the positions it looked at before, and those
> notes nudge its next answer - so the same position came back as +264, +266 or +269
> depending on what it had been asked just before. The reviewer on the project also found a
> second leak: the engine's answer depends on how many moves have passed without a capture,
> which my saved-answer label ignores. The fix is to wipe the engine's notes before every
> position and to show it only what the label describes. It costs about 7% in speed, and
> now asking twice gives exactly the same answer, down to the amount of work done.

**A bit deeper:** Stockfish has a *transposition table* (its "hash"): a memory of positions
it has already searched, kept between searches to speed them up. That memory is an input to
the result that the cache key (position, engine version, depth) does not name. So is the
*halfmove clock* - the count towards the fifty-move draw rule - which the position
identifier deliberately drops. And so is the number of search *threads*, because
multi-threaded search depends on timing. A cache is only correct if the value it stores is a
*pure function* of its key; this was the fifth time the project hit "the value depends on
something the key doesn't name".

**How it was found:** two ways, independently. While writing the engine wrapper I searched one
position at depth 16 three times, each after a different preceding search, without clearing
the hash: +264, +266, +269, with node counts of 596k to 1.1M. With the hash cleared first:
+273 and 45,986 nodes, three times out of three. Separately, the reviewer agent showed with
real Stockfish that 31 of 34 positions in one game differ warm vs cold, by up to 42 cp with
some best moves changing, and that one king-and-rook endgame scores +529 / +359 / +11 / 0 at
halfmove clocks 0 / 50 / 90 / 98.

**What changed:** `StockfishEngine.evaluate` sends `chess.Board(normalize_fen(board) + " 0 1")`
- no move history, halfmove clock zero - with a fresh `game` object so python-chess sends
`ucinewgame`, which clears the hash. `ENGINE_THREADS` is documented as "keep at 1" and the
wrapper logs a warning if it is not. The engine version is read from the binary's own UCI name
("Stockfish 18" -> `sf_18`), so it cannot disagree with the binary that actually ran.

**Measured cost** (sf_18 avxvnni, one 91-position game, Threads=1, Hash=128 MB):

| Depth | Hash kept between positions | Hash cleared per position | Cost |
| --- | --- | --- | --- |
| 16 | 25.6 s (3.56 positions/s) | 27.3 s (3.34 positions/s) | +6.6% |
| 12 | 2.5 s (36.5 positions/s) | 5.3 s (17.1 positions/s) | +114% |

The fixed cost is the clear itself: 29 ms per position at 128 MB, 10.5 ms at 16 MB. At the
production depth of 16 it is noise; at shallow test depths it dominates.

**Other measurements:** Threads=2 gave different scores on two runs of the same position at
depth 16 (-186 vs -213, -125 vs -133). Hash size is a much weaker input: 16 MB vs 64/128/256 MB
changed one score in six, by 2 cp.

**Known cost of the fix:** with the clock zeroed, a position that is genuinely one move from a
fifty-move draw is graded as if the clock had just reset. That is rare in real games and is
the price of a value that can be shared between every game that reaches the position.

**Alternatives rejected:**
- *Put the halfmove clock in the key*: correct, and splits one cache entry into up to a
  hundred for a property that changes the answer only near the end of a fifty-move run.
- *Keep the warm hash for speed*: 6.6% faster at depth 16 and the cached numbers stop meaning
  anything - a cache hit and a miss would return different values for the same key.
- *Multi-threaded search per worker*: not reproducible; scale with worker processes instead.

**Talking point:** "I asked Stockfish about the same position three times and got three
answers, because its hash table carries information from earlier searches into later ones.
For a cache keyed on position, engine and depth, that means storing one sample of a
distribution. So the engine only ever sees what the key contains - no history, clock zeroed -
with the hash cleared before every position and one search thread. At depth 16 that costs
6.6%, and now the same position gives the same score and the same node count every time."

**Where:** `src/chess_coach/engine.py`, `src/chess_coach/config.py` (Engine section),
`tests/test_engine.py` (same position after different searches; same position reached by two
move orders with different halfmove clocks).

## 2026-11-05 - Retries back off, crashes count, and hopeless jobs stop retrying `DONE`

> **In plain English:** Sometimes analysing a game fails. If it failed for a passing reason
> - the database hiccupped - it should be tried again, but not instantly, and not all at the
> same moment as every other job that failed at the same time. So each retry waits longer
> than the last, plus a little randomness. If a game has failed five times, it is set aside
> on a "needs a human" pile instead of being retried forever. And if the game itself is
> broken - its move list is garbage - it goes straight to that pile, because trying again
> won't fix it. Finally, a cleaner comes round and takes back games from workers that have
> gone silent, so a crashed worker can never keep a game hostage.

**A bit deeper:** *Exponential backoff* doubles the wait after each failure; *jitter* adds
randomness so jobs that failed together don't retry together (a *thundering herd*). A job that
has used up its attempts is *dead-lettered*: moved to a terminal state where it is visible
for inspection but no longer runs. The *reaper* is a periodic sweep that returns jobs whose
lease expired - their worker stopped heartbeating - to the queue.

**What changed:** `queue.fail`, `queue.reap_expired` and `queue.retry_delay`; Workers
settings `JOB_RETRY_BASE_SEC` / `JOB_RETRY_CAP_SEC`, and `JOB_LOCK_TIMEOUT_SEC` documented
as a heartbeat timeout rather than a job-length limit.

**Why the details are the way they are:**

- *The backoff lives in `run_after`.* The claim query already skips rows whose `run_after`
  is in the future, so a retry policy needs no scheduler and no extra state.
- *Equal jitter, the same shape as the Chess.com client's backoff.* Half the window is fixed
  so a retry is never immediate; half is random to spread the herd out.
- *Two kinds of terminal failure.* `failed` is "the input is bad, retrying cannot help" (a
  PGN that does not replay). `dead` is "five honest attempts failed". They mean different
  things to whoever reads the dead-letter pile, so they are different statuses - and the
  schema already had both.
- *A reaped attempt counts.* A job that kills every worker that touches it - out of memory,
  an engine crash that takes the process down - would otherwise loop forever, one worker
  at a time. It is dead-lettered after `max_attempts` like any other.
- *Reaped jobs come back immediately, not after a backoff.* The lock timeout they just sat
  out already was one; adding more would only delay recovering from an ordinary crash.
- *The reaper uses `SKIP LOCKED` too.* A row that is locked is being heartbeated or completed
  at that moment - the opposite of abandoned. Blocking on it would make the reaper wait for a
  job to finish and then (after the lock released) look again at a row that is now `done`.
  A test holds a completion transaction open and asserts the reaper walks straight past.
- *The lock timeout is about silence, not duration.* Workers heartbeat while they work, so
  a long game at a high depth is never reaped while its worker is alive.

**Alternatives rejected:**
- *Retry immediately*: a database blip that fails 50 jobs brings 50 jobs back at once,
  into the same blip.
- *Retry forever*: a poison job never surfaces; it just costs a worker every few minutes.
- *A reaper that ignores attempts*: the poison job again, via a different door.

**Talking point:** "There are two ways a job ends badly and they mean different things:
'the input is broken' goes straight to failed, 'it failed five times' is dead-lettered. And a
crash counts as an attempt - otherwise a job that crashes its worker takes down the whole
pool, one worker at a time, forever. The reaper skips locked rows on purpose: a locked row
is a job that's completing right now, which is the opposite of abandoned."

**Where:** `src/chess_coach/queue.py`, `tests/test_queue_retries.py` (including three zombie
worker scenarios: reaped and re-claimed by someone else, reaped and not yet re-claimed, and
re-claimed by a restart under the same worker id).

## 2026-11-05 - A claimed job is a lease with a fencing token, not just a lock `DONE`

> **In plain English:** The work queue is a list of games waiting to be analysed, and
> several workers take games off it at the same time. Two rules stop them stepping on each
> other. First, when a worker grabs a game, the others skip straight past it instead of
> waiting in line behind it. Second, a worker's claim on a game is like a numbered parking
> ticket. If a worker goes silent for too long, its game is given to someone else with a
> *new* ticket number. If the first worker wakes up later and tries to hand in its finished
> work, its old ticket no longer matches, so its work is thrown away instead of being saved
> on top of the new worker's.

**A bit deeper:** The queue is a Postgres table. A worker claims a job with
`SELECT ... FOR UPDATE SKIP LOCKED`: *row locking* means only one transaction can hold a row
at a time, and *SKIP LOCKED* means other workers step over locked rows instead of blocking on
them. A claim is a *lease* - ownership that has to be renewed (a *heartbeat*) or it expires.
The danger with leases is the *zombie*: a worker that was presumed dead, had its job handed
on, and then comes back. The defence is a *fencing token* - a number that goes up on every
claim - and every write the worker makes is conditional on still holding the current number.

**What changed:** `src/chess_coach/queue.py`: `enqueue` (idempotent on `jobs.dedupe_key`),
`claim`, `heartbeat`, `complete`, `has_unfinished`, and the `Lease` / `LeaseLost` types.

**Why the details are the way they are:**

- *The fencing token is `attempts`, which already existed.* Every claim increments it, so
  (job id, worker id, attempts) names one specific claim, with no new column. `locked_by`
  alone is not enough: a worker restarted under the same id (`worker-1` in a benchmark) can
  legitimately re-claim the job it was reaped from, and then its zombie's `locked_by` would
  match. There is a test for exactly that case.
- *`complete()` runs inside the transaction that writes the results, and goes first.* Its
  `UPDATE` takes the row lock and checks the lease, so the job's results and its `done` status
  commit together or not at all - and a reaper that got there first leaves nothing to match,
  so a zombie writes zero rows rather than writing moves and then failing.
- *The heartbeat is called from the work loop, not a background thread.* A background thread
  proves the process exists. A worker stuck in a hung call would keep its lease forever.
- *Time is the database's `now()`.* A worker with a skewed clock cannot make a lease look
  fresh or expired - the same reasoning as the rate limiter using Redis's clock.
- *The claim query matches the partial index* `ix_jobs_claimable (priority, run_after) WHERE
  status = 'pending'`, so a claim walks the index in order and stops at the first row it can
  lock, no matter how many finished jobs pile up. A test runs `EXPLAIN` with sequential scans
  disabled to prove the planner can use it.
- *Enqueue is a no-op on a `done` job too.* Re-running finished work should be a deliberate
  act, not a side effect of a sync enqueueing every pending game twice.

**Alternatives rejected:**
- *Plain `FOR UPDATE`*: correct, but every idle worker queues behind whichever one holds the
  head of the queue. A test sets `lock_timeout` so a regression to it fails instead of hanging.
- *`pg_try_advisory_lock` per job*: locks that live on the connection, invisible in the jobs
  table, and lost silently when a pooled connection is recycled.
- *Fencing on `locked_by` only*: fails the restarted-under-the-same-name case above.

**Talking point:** "Claiming a job safely is the easy half - `SKIP LOCKED` does it. The hard
half is the worker you gave up on that isn't actually dead. So a claim is a lease with a
fencing token: I reused the attempts counter, which goes up on every claim, and every write a
worker makes is conditional on it. A zombie's completion matches zero rows, and because
completion shares a transaction with the results, it can't half-write either. Eight threads
racing for 200 jobs claim each exactly once."

**Where:** `src/chess_coach/queue.py`, `tests/test_queue.py`.

<!-- end of M2 entries -->

---

<!-- M1 ENTRIES BELOW (ingestion). Newest first. -->

## 2026-11-05 - M1: the whole history loads once, and every re-sync costs two requests `DONE`

> **In plain English:** The first big piece works. You type in a Chess.com username and the
> program downloads every game that person has ever played - for my account, 4,550 games in
> under a minute. Run it again and it doesn't download everything twice: it asks Chess.com
> "has anything changed since last time?" and usually gets back "no", which costs almost
> nothing. To check it was right, I compared my counts of wins, losses and draws with the
> numbers Chess.com shows on my profile. They matched exactly, for every type of game.

**A bit deeper:** Chess.com stores a player's games in monthly bundles. A month that has
ended can never change, so once it has been downloaded it is marked finished and never asked
for again. Only the current month is re-checked, and that uses a *conditional request*: we
send back a fingerprint (an *ETag*) of the version we already have, and if nothing changed
the server answers "304 Not Modified" with an empty body. Each month is saved on its own, so
if the program crashes halfway through it picks up where it stopped instead of starting over.
And saving a game is an *upsert* - "insert it, or if it is already there, leave it alone" -
which is why running it twice can never create duplicates.

**What changed:** `src/chess_coach/chesscom/` (HTTP client and game parser),
`src/chess_coach/ratelimit.py`, `src/chess_coach/ingest/` (sync and the `chess-coach-sync`
CLI), migration `0002`, and tests for each against real Postgres and Redis.

**Why the details are the way they are:**

- *A month is final a day after it ends, not at midnight.* Games are filed by end time, and a
  game that finishes at 23:59 UTC on the 31st is not guaranteed to be in the archive at
  00:00. Freezing on the stroke of midnight would lose it forever. The grace day costs at
  most one extra 304.
- *One commit per month.* A crash mid-sync leaves the finished months marked done, so a
  resumed sync re-requests only what it had not finished - and nothing is half-written,
  because a month's games and its archive row commit together.
- *The upsert never touches analysis state.* A re-ingested game keeps its
  `analysis_status`; otherwise every re-sync would send finished games back to the queue. The
  only field allowed to change is Chess.com's accuracy score, which appears later if someone
  requests a review. A `WHERE` on the `ON CONFLICT DO UPDATE` means re-downloading an
  unchanged month writes zero rows, and `RETURNING (xmax = 0)` counts which rows were
  genuinely new without a second query.
- *Wins and losses are decided by where `win` appears.* Chess.com marks the winner `win` and
  the loser with the reason (`resigned`, `timeout`, `abandoned`...). Keying on `win` keeps
  working when Chess.com invents a new way to lose; only draws need a list of names. The
  loser's reason is stored as `termination`, so "win + timeout" reads as "my opponent
  flagged".
- *A per-player advisory lock stops two syncs of one player at once.* Not for correctness -
  the upsert already guarantees that - but so two workers do not spend the shared rate
  budget downloading the same months.
- *The username is validated before it goes into a URL.* `../x` or `a?b` cannot reach a
  different endpoint than intended.

**Verified against the real API:** the parser was written after reading a real 505-game
archive, and it found two more things the docs do not say. Chess960 games have Shredder-FEN
castling rights in the PGN header (`HBhb`) but `KQkq` in the JSON `initial_setup`; only the
header is right, so the parser uses it. And Chess.com puts an ECO code on chess960 games,
which means nothing for a shuffled back rank, so openings are only recorded for normal
starts. The first real sync also turned up a variant I had never seen, `oddschess`, which was
stored and skipped exactly as designed.

**Alternatives rejected:**
- *Re-download everything every night*: 53 requests and ~20 MB per player per night, against
  2 requests and 0 bytes.
- *Track "last synced month" on the player* (the design doc's `last_synced_month`): one
  number cannot express "October has gained a late game but August is final", and it cannot
  resume a crashed sync mid-history. Per-month rows with ETags can.
- *One transaction for the whole sync*: a crash in month 50 would throw away 49 months of
  work, and the transaction would hold locks for a minute.

**Talking point:** "The acceptance test wasn't a unit test, it was my own account: 4,550
games, and the per-time-control win/loss/draw totals matched Chess.com's profile numbers
exactly. Then I ran it again and it made two requests - the archive list and the current
month, which came back 304 with zero bytes. Every ended month is frozen and never fetched
again, a crash resumes from the last finished month, and the upsert can't reset analysis
state, so running it nightly is free."

**Where:** `src/chess_coach/ingest/sync.py`, `src/chess_coach/chesscom/`, `tests/test_sync.py`,
`tests/test_chesscom_parse.py`, `tests/test_chesscom_client.py`.

---

## 2026-11-05 - The "no duplicate games" rule would have deleted games instead `DONE`

> **In plain English:** I had a rule in the database that said "each Chess.com game can only
> be stored once", to stop duplicates. It sounds obviously right, but it is wrong. The
> program stores each game *from one player's side* - who had white, who won. If two people
> who both use the app play each other, that one game has to be saved twice: once as a win
> for one, once as a loss for the other. My rule would have quietly thrown the second copy
> away, so one of them would be missing a game and nothing would ever say so. I caught it
> while reading the database design before writing the code that fills it.

**A bit deeper:** The rule is a *unique constraint* - the database refuses to store a second
row with the same value. Uniqueness is how the program stays safe to re-run: it tries to save
every game, and the database ignores the ones it already has. But the constraint was on the
Chess.com game ID alone, while what a row actually represents is "this game, for this
player". The fix makes the pair unique: game ID *plus* player.

**What changed:** Migration `0002` drops the unique constraint on `games.chesscom_uuid` and
adds `uq_games_player_uuid` on `(player_id, chesscom_uuid)`. The ingest upsert targets the
new constraint.

**Why:** Every column that makes a row distinct has to be in its key. `color`, `result`,
`player_rating` and `opponent_rating` are all relative to the tracked player, so the row is
keyed on the player as well as the game. With the old constraint, the second player's
`INSERT ... ON CONFLICT` would hit the first player's row: `DO NOTHING` drops the game
silently; `DO UPDATE` would be worse, overwriting the first player's perspective with the
second's.

This is the **fourth** time this project has turned on the same question - *what does this
value actually depend on?* - after the `positions` cache key, the CI engine cache, and the
engine image. The other three were cache keys; this one is an idempotency key, which is the
same idea from the write side.

**Alternatives rejected:**
- *Store each game once and derive the perspective at read time*: correct, but it turns every
  report query into a "which side was I" `CASE` expression, and the analysis worker's
  `is_player_move` would no longer be a column.
- *Leave it until a second user exists*: it would surface as a missing game in one user's
  report, the hardest kind of bug to trace back to a constraint.

**Talking point:** "My idempotency key was one column short. I'd made the Chess.com game ID
unique, but a row is 'a game from one player's perspective', so when two tracked players
meet, that's two rows - and the second would have silently disappeared. It's the same bug as
a cache key that's missing an input, seen from the write side instead of the read side. I
found it reading the schema before writing the ingestion code, and there's a test where two
tracked players play each other and both see the game."

**Where:** `migrations/versions/0002_games_unique_per_player.py`, `src/chess_coach/models.py`,
`tests/test_sync.py::test_a_game_between_two_tracked_players_belongs_to_both`.

---

## 2026-11-05 - One speed limit shared by every worker, kept in Redis `DONE`

> **In plain English:** Chess.com asks programs not to ask it for data too fast, and if you
> do, it starts saying "slow down" or blocks you. My program can run several downloaders at
> once, so the speed limit has to be shared between them - otherwise four downloaders would
> each think they were allowed the full speed, and together go four times too fast. Think of
> one jar of tickets that every downloader has to take a ticket from before it asks
> Chess.com for anything, with new tickets dropping into the jar at a steady rate. And if
> Chess.com ever does say "slow down" to one of them, they *all* slow down, because they are
> all spending from the same jar.

**A bit deeper:** That jar is a standard design called a *token bucket*: tokens refill at a
fixed rate (one a second here), the bucket holds a few spare so short bursts are allowed, and
each request spends one. It lives in Redis, a fast shared store every process can reach. The
"take a token" step runs as a single small script inside Redis, so two downloaders can never
both grab the last token at the same moment. Retries back off exponentially (wait 1 s, then
2, then 4...) with some randomness, so a group of workers that all failed together do not all
retry together.

**What changed:** `RedisTokenBucket` in `src/chess_coach/ratelimit.py` (a Lua script for
take, another for penalize); `ChessComClient._get` acquires a token before every attempt,
retries 429 / 5xx / network errors with backoff, and honours `Retry-After`.

**Why the details are the way they are:**

- *Redis's clock, not the worker's.* The script reads `TIME` inside Redis. If callers passed
  their own timestamps, a worker whose clock ran ahead would refill the bucket early for
  everyone.
- *Reservation, not polling.* Taking a token may push the balance negative; the script returns
  how long the caller must wait. One round trip and one sleep per request, and waiters are
  served roughly in order instead of hammering Redis in a retry loop.
- *A 429 penalises the shared bucket.* A 429 means the budget everyone shares is spent, so
  `penalize()` drives the shared balance into debt and every worker waits - not only the one
  that got the 429.
- *The wait comes back as a string.* Redis converts Lua numbers to integers on the way out,
  so a 0.2 s wait would arrive as 0 - "go now". There is a test for exactly that.
- *Equal jitter, not full jitter.* Full jitter can pick a near-zero delay, which is the wrong
  response to being told to slow down. Half the window is fixed, half random.
- *Every retry spends a token.* A retry is a request as far as Chess.com is concerned.
- *Other 4xx responses are not retried.* A 400 is our bug; retrying it only burns budget.

**Alternatives rejected:**
- *A limiter per process*: N workers get N times the budget, which is the failure this exists
  to prevent.
- *`INCR` with a one-second key expiry (fixed window)*: allows double the rate across a window
  boundary, and gives no "how long should I wait".
- *Postgres as the limiter*: a row lock taken once a second by every worker is contention on
  the database the queue also depends on.

**Known limitation:** a worker already sleeping on a reservation when a 429 arrives will
still fire when it wakes. Its request may 429 too, which re-penalises the bucket, so the
system converges. Cancelling outstanding reservations would need a coordination channel
that is not worth it at one request per second.

**Talking point:** "The rate limit belongs to the client, not to a process, so the token
bucket lives in Redis and is one atomic Lua script that uses Redis's own clock - otherwise
one worker with a fast clock refills the bucket for everybody. And a 429 isn't a private
message to the worker that got it, it means the shared budget is gone, so it penalises the
shared bucket and every worker backs off together."

**Where:** `src/chess_coach/ratelimit.py`, `src/chess_coach/chesscom/client.py`,
`tests/test_ratelimit.py` (real Redis, including two connections sharing one budget),
`tests/test_chesscom_client.py`.

<!-- end of M1 entries -->

---

<!-- Entries before M1 (setup, CI, schema). -->

## 2026-11-05 - Compose starts the infrastructure, not the app `DONE`

> **In plain English:** Running this project needs a database and a cache server. Rather than
> install those onto my laptop, one command now starts them in containers - the same versions
> the automated tests use, isolated from everything else, and deletable without a trace. I
> deliberately left the application itself out of it, because two of its three parts have not
> been written yet. The immediate payoff is that the tests covering my database design now
> actually run on my own machine; until today they quietly skipped themselves and had only ever
> run on GitHub's servers.

**What changed:** `docker-compose.yml` starts `postgres:17-alpine` and `redis:7-alpine` with
healthchecks, named volumes and a first-boot script that creates a second database,
`chess_coach_test`. A `.dockerignore` landed with it. The API and workers are not in the file.

**Why:**

*The test database is the actual point.* `tests/conftest.py` proves the Alembic migrations can
build the schema from nothing by running `DROP SCHEMA public CASCADE` before every upgrade to
head - a schema that is only ever created from the models is a schema whose migrations have
never been tested. A fixture that destructive refuses to run unless the database name contains
`test`. With no local Postgres at all, that refusal was moot: **every** Postgres-backed test
skipped, so `test_models.py` and `test_migrations.py` had never once executed outside CI. The
schema was being designed with its tests running somewhere I couldn't see.

*Versions are pinned to match CI* because the queue's correctness lives in version-specific
locking behaviour. Deliberately introducing a "passes locally, fails in CI" axis in the one
component where correctness is hardest would be self-sabotage.

*The app is excluded because two thirds of it does not exist.* A compose entry for the ingest
and analysis workers would start containers that idle with nothing to do, and the Dockerfile
would have to answer where the engine binary comes from - a question with real consequences
that deserves to be answered when it bites, not pre-emptively.

*`.dockerignore` was written before the Dockerfile* because the first `docker build` is exactly
when it is too late to discover the build context is 211 MB, 114 MB of which is a Windows
executable that cannot run in a Linux image.

**Alternatives rejected:**
- *Install Postgres natively on Windows*: a second thing to keep version-matched with CI, no
  clean reset, and an uninstall that leaves data directories behind.
- *Point the tests at the development database*: `conftest.py` refuses on purpose, and it is
  right to - it drops the schema.
- *Put the app in compose now with placeholder worker services*: containers that start and do
  nothing, plus a Dockerfile written against code that has not been designed yet.
- *SQLite for local tests*: rejected earlier for the queue, and the same argument applies.

**Talking point:** "My schema and migration tests had never run on my own machine. They skip
cleanly when no database is reachable - which is the right default, so a fresh clone can run
the suite - but it meant I was designing a schema while its tests only ever ran in CI. One
compose file fixed that. The detail I would point at is that it creates a *second*, separate
test database: my fixtures prove the migrations work by dropping the schema and rebuilding it,
and they refuse to run unless the database name contains 'test'. That guard is there because
the cost of that fixture pointing at real data once is unrecoverable."

**Where:** `docker-compose.yml`, `docker/postgres/init/01-create-test-database.sql`,
`.dockerignore`, README quick start. `docs/DESIGN.md` still lists M0 as Compose starting "API,
workers, Postgres, Redis" - the original plan, left unedited on purpose so the gap stays
visible.

---

## 2026-11-05 - The engine is probed at container start, never baked into the image `DECIDED`

> **In plain English:** The chess engine ships in several builds aimed at different processor
> generations, and the fastest one does not politely slow down on an older chip - it crashes
> outright. My laptop is new enough to run the fastest build. If the engine were chosen while
> *building* the shipping package, it would pick the fast build based on my laptop and then
> crash on the older servers it gets deployed to. So the choice happens when the container
> starts, on the machine that will actually run it.

**What changed:** The M2 Dockerfile will **not** run `tools/fetch_stockfish.py` as a build
step. The installer runs in the container entrypoint instead, with `vendor/` mounted as a named
volume so the 109 MB download happens once per machine rather than once per container start.

**Why:** An image is portable; the probe's answer is not. "Which SIMD build runs here" is a
property of the host CPU, so baking it into an image is a cache key that omits something the
value depends on - **the third instance of that same error in this project**, after the
`positions` primary key (caught by reasoning) and the CI engine cache (caught by two red
builds). The pattern is now unmistakable: every time I store a computed answer, I have to ask
what inputs it actually depended on, and a probe's input is the machine.

The fix costs no new code. `fetch_stockfish.py` already re-probes a binary that is present and
re-fetches only if it cannot search, which is the self-healing behaviour added in `c360e9b`.
Calling it from the entrypoint is that same validate-on-read property, applied to images.

**Alternatives rejected:**
- *Fetch at build time*: fastest container start, and the image is silently valid only on CPUs
  resembling the build machine's. Breaks the first time it is deployed or handed to someone
  else - and breaks with `SIGILL`, which tells the next person nothing.
- *Bake the conservative `sse41-popcnt` build*: always runs, and throws away measurable
  nodes/sec on the machine doing hours of analysis. Nodes/sec is the input to every throughput
  number this project is meant to produce.
- *Build one image per SIMD variant*: correct for a product, and triples the build matrix for a
  one-person project with one deployment target.
- *Fetch per container start with no volume*: 109 MB per worker per start. At eight workers
  that is most of a gigabyte to start a benchmark.

**Talking point:** "Same bug, third appearance - and by the third time I recognised it before
it fired. I cache engine evaluations keyed on the engine version and depth because the value
depends on both. My CI cache broke because its key named the release but not the probe that
picked the binary. So when it came to the container image, the question was already familiar:
the build picks the engine by testing the CPU, and an image gets copied to machines with
different CPUs. The probe's answer belongs to the host, so it has to be computed on the host -
at container start, not at build time."

**Where:** planned for the M2 `Dockerfile` entrypoint. `tools/fetch_stockfish.py` already has
the re-probe behaviour this depends on.

---

## 2026-11-05 - I made the exact cache mistake I had designed out of the app `DONE`

> **In plain English:** To save time, the test system keeps a copy of the chess engine rather
> than re-downloading it every run. The copy it kept only works on *some* of the machines that
> run the tests - and nothing ever checked. It just handed the copy over and the tests died.
> Worse, the step that would have replaced the bad copy was skipped precisely *because* a copy
> already existed, so a fix sitting in the project had no way to take effect. The real trap is
> subtler than "the saved copy was bad": the right answer depends on which machine asks, and
> the machine changes from run to run, so **no filing system could have got this right**. The
> only fix is to check the thing still works at the moment you pick it up.

**Keep this one next to the position-cache entry and the engine-image entry. The three
together are the best answer you have to "tell me about a time you made a mistake" - the same
error three times: reasoned out in advance, then committed for real, then recognised before it
fired.**

**What changed:** The CI cache key for the engine binary now hashes the installer script as
well as the lock file, and the installer runs on every build instead of only on a cache miss.

**What happened:** After the dual-net probe fix went green, a later PR failed with the same
`SIGILL` - on a branch that already contained the fix. The probe was correct. The cache was
the cause:

```
Cache Stockfish: Cache hit for: stockfish-Linux-c2ff5d61...
Fetch Stockfish: skipped (if: cache-hit != 'true')
Tests:           engine process died unexpectedly (exit code: -4)
```

**The detail I got wrong when I first wrote this up, and the logs disprove:** I assumed an
earlier *failing* run had saved the bad binary. It had not. `actions/cache` does not save when
the job fails - both red runs show `Post Cache Stockfish: skipped`, and both show
`Cache not found`. The entry was saved by a **green** run on `main`
([42049637201](https://github.com/ot7Deity/Chess-coach/actions/runs/42049637201)), which had
fetched, probed, passed, and cached the binary that worked *on that host*.

**The evidence that settles it.** Two runs on the same PR, two minutes apart, restored the
*identical* entry - same key, same `74804294 B`:

| Run | Cache | Engine tests |
| --- | --- | --- |
| [42051183768](https://github.com/ot7Deity/Chess-coach/actions/runs/42051183768) (15:04) | hit, fetch skipped | **passed** (its 3 failures were schema tests) |
| [42051512216](https://github.com/ot7Deity/Chess-coach/actions/runs/42051512216) (15:06) | hit, fetch skipped | **`SIGILL`** |

Same bytes, opposite outcomes. `ubuntu-latest` is a heterogeneous fleet: the host that saved
the cache had AVX-VNNI, so the probe correctly accepted the `avxvnni` build; the 15:06 host did
not. The green run after the fix proves it directly, because the fetch step ran again:

```
[fetch-stockfish] trying stockfish-ubuntu-x86-64-avxvnni.tar
[fetch-stockfish]   smoke test exited -4 (wrong SIMD build for this CPU)
[fetch-stockfish] trying stockfish-ubuntu-x86-64-avx2.tar
[fetch-stockfish]   OK - Stockfish 18 (searched both evaluation networks)
```

**Why the key was wrong - and why no key could have been right.** The cached value is not
"Stockfish 18". It is *"the build this runner was able to actually run, as judged by that
version of the probe"*. So it depends on the probe, which the key never mentioned - and it
depends on **the runner's CPU**, which is not a property of the repository at all. The key had
already been through two formulations, and neither could have worked: it started as
`stockfish-sf_18-ubuntu-avxvnni`, naming a SIMD variant the job had no way to guarantee, and
became `stockfish-<os>-<hash of stockfish.lock.json>`, naming the release. Adding the
installer's hash is a genuine improvement, because changing the probe should expire entries the
old probe produced. **But it would not have prevented this failure.** Nothing in the repository
changed between 15:04 and 15:06. The machine did.

That is the same error the `positions` primary key was changed to avoid - a cache key that
omits something the value depends on - with a sharper edge. There, every input (position,
engine version, depth) was nameable. Here one input is the host, so the lesson "name everything
the value depends on" runs out of road, and what is left is: **validate on read.**

**Which is why the second fix is the one that actually works:** the installer now runs
unconditionally. It already re-probes a binary that is present and re-fetches if it cannot
search, so running it always makes a bad entry **self-healing** instead of permanently broken.
Gating it on a cache miss was an optimisation worth under a second that cost two red CI runs.
A cache you cannot validate on read is a cache that can only be fixed by hand.

**Alternatives rejected:**
- *Manually purge the cache*: fixes this instance and leaves the mechanism able to do it again.
- *Stop caching the binary*: 80 MB per run, and it would hide the lesson rather than fix it.
- *Only save the cache on success*: this is what `actions/cache` already does - it is exactly
  how the bad entry got saved. Believing otherwise is what made me misdiagnose this the first
  time.
- *Pin the baseline `sse41-popcnt` build in CI*: cacheable by any key and always runs, but it
  stops CI from exercising the fallback chain the project depends on.

**Talking point:** "I corrected my design so the position cache key included the engine version
and depth, because an evaluation depends on both. Then I wrote the identical bug into my CI
config - and when I first wrote it up, I got the cause wrong. I assumed a failing run had
poisoned the cache. The logs said otherwise: a *green* run saved it, and the proof was two runs
on the same branch, two minutes apart, restoring byte-identical bytes - one passed, one died
with an illegal instruction. The runners aren't the same hardware. So the cached value wasn't
'Stockfish 18', it was 'the build that worked on whichever machine happened to save it', and
that's not something a key can name. Two lessons. A cache key has to name everything the value
depends on - and when one of those things is the machine, you can't fix it with a key at all,
you validate on read. Re-probing cost under a second and turned a permanently broken cache into
a self-healing one."

**Where:** `.github/workflows/ci.yml` (`Fetch or verify Stockfish`). Cache saved green by
[42049637201](https://github.com/ot7Deity/Chess-coach/actions/runs/42049637201); the
same-bytes-different-outcome pair is
[42051183768](https://github.com/ot7Deity/Chess-coach/actions/runs/42051183768) and
[42051512216](https://github.com/ot7Deity/Chess-coach/actions/runs/42051512216); the fallback
proof is [42051930368](https://github.com/ot7Deity/Chess-coach/actions/runs/42051930368).
Related: the `positions` cache-key entry, the dual-net probe entry, and the engine-image entry,
which is this bug caught in advance.

---

## 2026-11-05 - The engine service gets extracted after M3, not before `DECIDED`

> **In plain English:** Splitting a big application into smaller independent pieces
> ("microservices") is fashionable, and splitting off the chess-engine part is the one split
> here that genuinely pays for itself. But I'm doing it *after* I've measured how fast the
> current version runs, not before. If I split first I could never answer "what did that cost
> you?" - and being able to answer that is the entire point of doing it.

**What changed:** The engine stays in-process through M3. Once the 1/2/4/8-worker throughput
numbers exist, Stockfish and the position cache get extracted into a separate **analysis
service** behind an HTTP contract, as its own PR, and the benchmark is re-run.

**Why:** The system is already service-oriented in the way that matters - three
independently deployable processes (API, ingest worker, analysis worker) that scale
separately, fail separately, and communicate *asynchronously through the jobs table* rather
than by calling each other. Full textbook microservices would actively hurt it: a database
per service would break the report queries, which are single SQL statements joining games,
moves and positions, and replacing those with eventual consistency buys nothing. Many
services sharing one database is the "distributed monolith" anti-pattern, which is exactly
what an interviewer probes for when they hear the word.

Extracting *the engine specifically* is the one split with real justification: it owns
Stockfish and the cache behind a versioned contract, engine replicas scale independently of
queue workers, an engine upgrade deploys without touching ingestion, and it enables **request
coalescing** - four workers asking for the same position concurrently collapse into one
engine call, which raises the effective cache hit rate rather than just moving code around.

The ordering is the important part. The entire value of the split is being able to say what
it cost, and that requires an in-process baseline to compare against. Splitting first makes
"what did the network boundary cost you?" unanswerable.

**Alternatives rejected:**
- *Extract it now*: same architecture, no baseline, no measurement, nothing to say about it.
- *Full microservices (DB per service + message bus)*: breaks M4's analytics and invites the
  distributed-monolith critique.
- *Never split*: fine engineering, but leaves a measurement worth having unmeasured.

**Talking point:** "I deliberately did not start with microservices. The processes were
already decoupled through a queue, which is the property people actually want. Then I
extracted the engine into its own service once I had baseline throughput numbers, so I could
measure what the boundary cost instead of assuming. It also let the service coalesce
duplicate position requests across workers, which a shared in-process cache could not do."

**Where:** planned for after M3.

---

## 2026-11-05 - A health check that doesn't exercise the real path lies to you `DONE`

> **In plain English:** Before using the downloaded chess engine, the installer tests that it
> works. My test was far too shallow - it asked the engine to introduce itself, which it could
> do, and then the engine crashed the instant it was asked to actually think about a chess
> position. I made the test ask it to think, pushed the fix, and it failed again in exactly
> the same way: the engine turns out to have two separate "brains", one for crowded boards and
> one for nearly-empty ones, and my test only ever used the one that worked. The takeaway is
> that a self-test proves only what it actually does. "It started up" is not "it can do its
> job".

**This is the best bug in the project so far. CI found it, twice, and the second time proved
my first fix wrong.** Learn this one properly - it is the "tell me about a bug you hit"
answer.

**What changed:** The installer's SIMD probe now runs **two** searches - a dense position and
a sparse one - and requires both to return a move. It originally sent only `uci`.

**The failure:** The fallback chain for CPUs without AVX-VNNI was designed in from the start.
It did not fire. CI failed with:

```
chess.engine.EngineTerminatedError: engine process died unexpectedly
    (exit code: -4, binary not compatible with cpu?)
```

Exit code -4 is `SIGILL`: an illegal instruction. The AVX-VNNI binary was running on a runner
whose CPU has no AVX-VNNI, exactly the case the fallback existed to handle.

**Root cause, attempt 1 (wrong fix):** the probe only performed the UCI handshake. Answering
`uci` prints strings and never reaches the vectorised evaluation code, so a binary that cannot
evaluate anything passes it. Note which test agreed with the broken probe:
`test_engine_starts_and_reports_pinned_version` **passed** while
`test_engine_finds_a_mate_in_one` failed - the test that merely started the engine was wrong
in precisely the same way the probe was wrong. I made the probe search `startpos` at depth 8
and pushed.

**It failed again, identically.** The probe reported "searched" and the same two tests died.

**Root cause, attempt 2 (the real one):** Stockfish 18 has **two NNUE networks** - `EvalFile`
and `EvalFileSmall` (visible as separate UCI options) - and it selects the small one for
sparse positions and large material imbalances. The two have **separate vectorised code
paths**. Searching `startpos` is a dense position, so it exercises the big net only. The
illegal instruction lived in the small net's code. Both failing tests touch sparse positions:
a six-piece mate-in-one directly, and a depth-14 search that simplifies into endgames deep in
its tree. So the probe tested the half of the engine that worked.

The fix is to probe a dense position *and* a sparse one - the minimum that touches both nets.

**The transferable lesson:** a health check is only worth the code paths it executes. "The
process started" is not "the process can do its job", and "it handled one request" is not "it
can handle every kind of request". The same reasoning is why `/health` and `/ready` are
separate endpoints here, and why the queue tests kill a worker mid-job rather than asserting
that the worker starts.

**Alternatives rejected:**
- *Pin the baseline `sse41-popcnt` build everywhere*: guaranteed to run, and throws away real
  nodes/sec on the machine doing hours of analysis.
- *Read CPU flags from `/proc/cpuinfo` or `cpuid`*: platform-specific, and - as this bug
  shows - a proxy for "does it work" rather than the thing itself. Running the binary is the
  direct test. Had I done this, I would have had to know in advance that the small net
  needed AVX-VNNI too.
- *Catch the crash at analysis time and retry with another build*: moves a setup failure into
  the hot path, to be re-discovered once per job forever.

**Talking point:** "I had a fallback for CPUs that lacked the instruction set, and it didn't
fire - my probe only did the UCI handshake, which doesn't touch the vectorised evaluation
code. I fixed it to actually search a position, and CI failed again in exactly the same way.
It turned out Stockfish 18 has two neural networks, a big one and a small one for sparse
positions, with separate SIMD code paths - and my probe only ever searched the opening
position, so it tested the half that worked. The second fix searches a dense position and an
endgame. What I took from it is that a health check is worth exactly the code paths it
executes - the first fix felt like a real fix and was still testing the wrong half."

**Where:** `tools/fetch_stockfish.py` (`smoke_test`), regression test
`tests/test_stockfish_binary.py::test_smoke_test_rejects_an_engine_that_cannot_search`, which
fakes both failure modes: an engine that answers the handshake but never moves, and one that
serves the first search then dies. CI runs
[42047974613](https://github.com/ot7Deity/Chess-coach/actions/runs/42047974613) and
[42048894115](https://github.com/ot7Deity/Chess-coach/actions/runs/42048894115).

---

## 2026-11-05 - Engine downloads are checksum-verified and SIMD-probed `DONE`

> **In plain English:** The project downloads Stockfish - the chess engine that does the actual
> judging - off the internet and then runs it. Two things had to be handled. First, a download
> that has been tampered with or truncated should fail loudly rather than run, so every
> download is checked against a fingerprint recorded in the project. Second, the engine comes
> in several builds aimed at different generations of processor, and the fastest build doesn't
> politely slow down on an older chip - it crashes outright. So the installer tries the fastest
> build, actually runs it, and steps down a level if it fails.

**What changed:** `tools/fetch_stockfish.py` downloads Stockfish against SHA-256 digests
pinned in `tools/stockfish.lock.json`, then picks the right build for the CPU by trying
`avxvnni` then `avx2` then `sse41-popcnt` and running a UCI smoke test on each.

**Why:** Two separate problems.

*Supply chain:* the build pipeline downloads an 80 MB binary from the internet and then
executes it on every worker. Verifying it against a digest pinned in the repo costs three
lines and means a compromised or truncated download fails loudly instead of running. The
digests come straight from the GitHub release API, so no manual hash copying was involved.

*Portability:* picking a SIMD build is not a preference, it is a hard constraint - a binary
compiled for AVX-VNNI does not degrade on a CPU that lacks it, it dies with an illegal
instruction. This laptop is Meteor Lake and has AVX-VNNI; GitHub Actions runners are older
Ice Lake server parts that do not. One hardcoded choice breaks either local dev or CI. So the
script tries the fastest first and falls back, and "does it work" is answered by actually
starting the engine and speaking UCI to it, because an illegal instruction cannot be detected
any other way.

Note that the *architecture* may differ between machines while the *version* may not: all
SIMD variants of the same release return identical evaluations, just at different speeds. The
version is what cached evaluations depend on.

**Alternatives rejected:**
- *`curl | tar` in the Dockerfile*: no verification, no fallback, and it would have silently
  picked a build that crashes in CI.
- *`apt-get install stockfish`*: version floats with the base image, which would invalidate
  the position cache on an unrelated image rebuild.
- *Detect CPU flags from `/proc/cpuinfo` or `cpuid`*: platform-specific, and still needs the
  smoke test as a backstop. Running the binary is the direct test.

**Talking point:** "The engine binary is downloaded and executed by every worker, so it is
verified against a checksum pinned in the repo. The more interesting part is SIMD: the fast
AVX-VNNI build raises an illegal instruction on the older CPUs GitHub Actions uses, and you
cannot catch that with a try/except - so the installer tries builds fastest-first and smoke
tests each one by speaking UCI to it. Local dev gets the fast binary, CI gets a working one,
and both are the same Stockfish version, which is what my cache keys depend on."

**Where:** `tools/fetch_stockfish.py`, `tools/stockfish.lock.json`,
`tests/test_stockfish_binary.py`.

---

## 2026-11-05 - CI tests against real Postgres and Redis, not mocks `DONE`

> **In plain English:** The automated tests run against a real database, not a simplified
> stand-in. The hardest problem in this project is making sure that when several background
> workers are chewing through a pile of games, no two ever grab the same game - and that
> depends on specific guarantees only the real database makes. A stand-in would have given me
> a reassuring green tick while hiding the one bug I actually care about.

**What changed:** GitHub Actions runs two jobs - lint, and tests against `postgres:17` and
`redis:7` service containers.

**Why:** The riskiest code in this project is the job queue, and its correctness lives
entirely in behaviour SQLite and mocks cannot reproduce: `SELECT ... FOR UPDATE SKIP LOCKED`,
transaction visibility between concurrent workers, and row-level locking. A test suite that
mocked the database would pass while the queue handed the same job to two workers. The same
argument applies to the Redis token bucket, whose whole purpose is coordination between
processes - a faked Redis tests nothing.

Engine tests are opt-in via `STOCKFISH_PATH` and the binary is cached by release tag, so a
fresh clone can run the unit suite immediately without an 80 MB download.

**Alternatives rejected:**
- *SQLite for tests*: fast, and would make the one part of the system worth testing untestable.
- *A mocked Redis*: tests the mock, not the coordination.
- *Testcontainers*: works, but Actions service containers are already there and need no
  Docker-in-Docker setup.

**Talking point:** "CI runs against a real Postgres because the queue's correctness is
`SKIP LOCKED` and row-level locking - behaviour you cannot mock. If I had tested against
SQLite, the suite would have been green while two workers analysed the same game."

**Where:** `.github/workflows/ci.yml`, `tests/conftest.py`.

---

## 2026-11-05 - Classify blunders on capped evaluation, not raw centipawn loss `DECIDED`

> **In plain English:** The report labels your bad moves as blunders, mistakes or
> inaccuracies, based on how much each one cost you. My first rule just measured the raw drop,
> which gets things backwards in real games: a sloppy move in an already-hopeless position
> looked like a catastrophe, while the single move that threw away a drawn game barely
> registered. The fix treats "already winning by a mile" and "already losing by a mile" as
> single states, so only swings that genuinely change the result count. This matters because
> it's the headline number of the whole report.

**What changed:** The design doc's rule was "blunder = lost >= 300 centipawns". The
implementation will clamp both evaluations to +/-1000 cp *before* subtracting, and store the
raw evals and `mate_in` separately.

**Why:** Centipawn loss is not linear in how much a mistake matters. Going from -900 to -1300
is a 400 cp "blunder" by the naive rule, but the game was already lost - nothing changed.
Going from +50 to -250 is only 300 cp but it is the move that threw a drawn game. Raw
centipawn diffing therefore over-reports blunders in losing positions and under-reports the
ones that actually cost the game - which would make the headline number of the whole report
wrong. Clamping to +/-1000 means "already winning/losing by a lot" is treated as a single
state, so swings inside it stop registering.

**Alternatives rejected:**
- *Raw centipawn loss* (the doc's version): simplest, but produces the bias above.
- *Win-probability delta* (convert cp to expected score, diff that): this is what Lichess
  does and it is the most correct option. Rejected **for now** because it adds a tuned
  sigmoid whose constants would be unexplainable, and clamping captures most of the benefit.
  Revisit once the first report exists - the raw evals are stored, so it is a re-computation,
  not a re-analysis.

**Talking point:** "My first classification rule was wrong in a way that only shows up in
real data: it called moves in already-lost positions blunders. I kept the raw evaluations in
the database precisely so I could change the rule without burning hours of engine time again
- re-classifying 220k positions is a SQL update, re-analyzing them is six hours of CPU."

**Where:** will land in `src/chess_coach/analytics/` and the `moves.classification` column.

---

## 2026-11-05 - Pin Stockfish 18, not the latest release `DECIDED`

> **In plain English:** I deliberately use an older release of the chess engine rather than the
> newest one. The newest is published for Windows only, and I write the code on Windows but run
> the real workload on Linux servers - so I'd have ended up with two different engines giving
> two different opinions on the same position. Since the project saves engine opinions and
> reuses them later, mixing two versions would quietly poison the saved results.

**What changed:** The engine is pinned to Stockfish `sf_18`, using the `avxvnni` build.

**Why:** Two reasons. First, `sf_19` (the latest) ships **Windows binaries only** - no Linux
build. Development happens on Windows and the workers run in Linux containers, so `sf_19`
would mean a different engine version on each side, or compiling from source in the
Dockerfile. `sf_18` ships prebuilt binaries for both. Second, and more important: a cached
evaluation is only valid for the engine version that produced it, so having dev and
production on different versions would quietly poison the shared cache.

The `avxvnni` variant is chosen because this machine is an Intel Core Ultra 7 155H (Meteor
Lake), which has AVX-VNNI but no AVX-512. Picking the right SIMD build changes nodes/sec
measurably, and nodes/sec is the input to every throughput number in this project.

**Alternatives rejected:**
- *Latest release (`sf_19`)*: no Linux binary, so dev and prod engines would diverge.
- *Compile from source in the Dockerfile*: reproducible, but adds minutes to every image
  build for no benefit over a pinned official binary.
- *Debian's `stockfish` apt package*: unpinned and several versions behind; the version would
  change under us on a base-image bump, silently invalidating the cache.

**Talking point:** "I pinned an older engine release on purpose. The newest one had no Linux
build, and since my cache keys include the engine version, running different versions locally
and in production would have meant cache misses everywhere - or worse, if I had keyed the
cache on position alone, silently mixing evaluations from two engines."

**Where:** `tools/fetch_stockfish.py`, `Dockerfile`, `.env.example`.

---

## 2026-11-05 - Position cache is keyed on (position, engine version, depth) `DECIDED`

> **In plain English:** The same chess position turns up in thousands of different games, so
> the engine's verdict on it is worked out once and reused - that's what makes analysing
> thousands of games affordable. The subtlety is that a "verdict" isn't a fact about the
> position; it's what *this* engine concluded after thinking *this* hard. So each saved verdict
> is filed under all three. Without that, turning the thinking time down for a quick
> experiment would have permanently replaced good results with rushed ones, and nothing
> anywhere would have warned me.

**What changed:** The design doc made `positions.fen_hash` the primary key. The primary key
will instead be `(fen_hash, engine_version, depth)`.

**Why:** An evaluation is not a property of a position - it is the output of *one engine* at
*one search depth*. With a bare `fen_hash` key, the first worker to see a position writes its
eval and every later lookup gets that row back regardless of what it asked for. Lower the
depth for a fast local run and those shallow evals become permanent; upgrade the engine and
you are mixing two engines' judgments in the same report. Neither failure throws an error -
the numbers just quietly stop meaning anything, which is the worst kind of bug to have in the
one feature the whole project is about.

**Alternatives rejected:**
- *Bare `fen_hash`* (the doc's version): silently serves wrong-depth and wrong-engine evals.
- *Store only the deepest eval per position, overwrite on deeper search*: avoids duplicates
  and is tempting, but makes a benchmark at depth 12 destroy the depth-16 data it is
  measuring against, and makes "cache hit rate at depth N" unanswerable.

**Talking point:** "This is the cache bug I designed out before it happened. A cache key has
to include everything the value depends on. My value depended on engine version and search
depth, not just the board position - so if I had keyed on position alone, lowering the depth
for a quick test run would have permanently contaminated the dataset with shallow
evaluations, with no error message anywhere."

**Where:** will land in `src/chess_coach/models.py` and the `positions` migration.

---

## 2026-11-05 - Fixed search depth, never movetime `DECIDED`

> **In plain English:** When you ask the engine about a position you can either say "think for
> one second" or "think this many moves ahead". I always use the second. "Think for one second"
> gives a different answer depending on how busy the computer happens to be, which makes the
> answer impossible to save and reuse, and makes my speed measurements meaningless - adding
> more workers wouldn't make the system faster, it would just make every answer sloppier while
> the numbers looked unchanged.

**What changed:** Stockfish is always asked for a fixed `depth`, never a time budget.

**Why:** A movetime search returns whatever it managed to compute in N milliseconds, so the
same position gives different answers depending on machine load - and that makes the result
uncacheable, because a cache entry has to be reproducible to be trustworthy. It also makes
the 1/2/4/8-worker benchmark meaningless: with movetime, more workers would not run faster,
they would each just search more shallowly, and throughput would look constant.

**Alternatives rejected:** *movetime* (common in casual analysis scripts, and what most
tutorials use) - fails both reproducibility and benchmarking. *Node-limited search* is
reproducible too and would work, but depth is the unit the chess world quotes, so it is
easier to compare against Lichess and Chess.com.

**Talking point:** "Fixed depth rather than fixed time, because a time-limited search isn't
reproducible, and an unreproducible result can't be cached or benchmarked. It also means my
scaling numbers measure throughput instead of silently measuring search quality."

**Where:** `.env.example` (`ENGINE_DEPTH`), `src/chess_coach/config.py`.

---

## 2026-11-05 - Sync Python throughout, not async `DONE`

> **In plain English:** There's a popular programming style ("async") that pays off when a
> program spends most of its time waiting for other computers to reply. This program barely
> waits - it spends its time on heavy calculation. Using that style here would have added real
> complexity to the most delicate part of the system and bought no speed at all. To go faster
> I run more copies of the program instead, which is what actually uses the rest of the
> processor.

**What changed:** SQLAlchemy sync sessions and `def` (not `async def`) FastAPI handlers.

**Why:** The design doc listed "async" as a reason to pick FastAPI, but nothing here benefits
from it. The analysis workers are CPU-bound - they block on a Stockfish subprocess, which
async cannot speed up. The ingest worker is rate-limited to about one request per second on
purpose, so there is no concurrency to exploit. Meanwhile async SQLAlchemy would make the
queue's `SELECT ... FOR UPDATE SKIP LOCKED` transaction handling considerably harder to
reason about, in the one part of the system where correctness matters most.

FastAPI runs `def` handlers in a threadpool, so blocking database calls are safe. Declaring
them `async` while doing blocking I/O inside is the actual footgun - it stalls the event loop
and is one of the most common FastAPI mistakes.

**Alternatives rejected:** *async everywhere*: more fashionable, more complexity, and it
would make the transactional queue code harder to get right for zero throughput gain on a
CPU-bound workload.

**Talking point:** "I used sync Python deliberately. The expensive work is a Stockfish
subprocess pegging a core - async doesn't make a CPU-bound task faster, it just adds
complexity to the transaction handling in my job queue. I scale with processes, not
coroutines, because that is what actually uses the other 15 cores."

**Where:** `src/chess_coach/api/main.py`, and all future worker/db code.

---

## 2026-11-05 - Chess.com contact email lives only in gitignored `.env` `DONE`

> **In plain English:** Chess.com asks any program using their data to say who to contact if it
> misbehaves, and a program that doesn't risks being blocked. But this project's code is public
> on GitHub, and an email address sitting in public code gets harvested by spam bots within
> days. So the address lives in a private settings file that is never published: Chess.com gets
> it with every request, the open internet doesn't.

**What changed:** The `User-Agent` is built at runtime as
`ChessCoach/0.1 (<CHESSCOM_CONTACT>)`, with the real address read from `.env`.
`.env.example` ships a placeholder and `.env` is gitignored.

**Why:** Chess.com asks API clients to identify themselves with contact information, and an
unidentified client risks being throttled or blocked - a failure that is miserable to debug
mid-build. But this repository is public, so a hardcoded address would be scraped by bots
within days. Reading it from the environment satisfies Chess.com on every request without
publishing the address.

**Alternatives rejected:** *hardcoded in source* (scraped); *no contact info at all*
(risks a block that looks like a mysterious ingestion bug).

**Talking point:** "Third-party API etiquette: their docs ask for a contactable User-Agent,
so every request carries one, but it comes from the environment rather than source because
the repo is public. Same reason the database URL isn't committed."

**Where:** `src/chess_coach/config.py` (`Settings.user_agent`), `.env.example`.

---

## 2026-11-05 - Seven corrections to the design doc, found by calling the API first `DONE`

> **In plain English:** Before writing any of the code that pulls games off Chess.com, I spent
> ten minutes simply asking their service for a few real games and reading what came back.
> Seven things in my design document turned out to be wrong - including that the field I had
> planned to use for naming chess openings contains a web link rather than the opening's name,
> which would have broken one of the four features the report is built around. These were the
> cheapest fixes in the project, because none of that code had been written yet.

**What changed:** Before writing any ingestion code, the Chess.com API was called directly to
check what it actually returns. Five assumptions in the design doc were wrong.

**Why / what was found:**

1. **`eco` in the JSON is a URL, not an ECO code** (`.../openings/Closed-Sicilian-Defense-...`),
   and it is missing on about 2% of games. The real code is in the PGN header `[ECO "B24"]`.
   The doc's plan to store `opening_eco` straight from the JSON field would have filled the
   column with URLs - and "results by opening" is one of the four report features.
2. **`rules` is not always `chess`.** `chess960`, `bughouse`, `crazyhouse`, `kingofthehill`
   and `threecheck` all appear in archives. Stockfish cannot evaluate those positions, so
   they must be ingested but marked `analysis_status='skipped'` - not dropped, or our game
   count would disagree with Chess.com's and look like an ingestion bug.
3. **Clocks are real and per-move:** `{[%clk 0:01:01.3]}` comments on every move. The
   "accuracy under 30 seconds on the clock" feature is buildable, which was not certain.
4. **Some games have `SetUp`/`FEN` headers** - a non-standard starting position. The analysis
   worker has to start from that FEN or it will replay the moves onto the wrong board.
5. **`accuracies` is already in the response** - Chess.com's own accuracy numbers for most
   games. Storing them gives free ground truth to check our own pipeline against.
6. **`If-None-Match` genuinely returns `304` with 0 bytes**, confirmed against a live archive.
   The entire re-sync design depends on this, so it was worth 30 seconds to verify.
7. **The player-side `result` vocabulary is wide**: `win`, `resigned`, `timeout`,
   `checkmated`, `stalemate`, `insufficient`, `repetition`, `agreed`, `abandoned`... A
   `result` column holding these raw needs normalizing to win/loss/draw for reporting, with
   the raw termination kept for the "how do I lose" breakdown.

**Talking point:** "Before I wrote the ingestion layer I spent ten minutes just calling the
API and reading the response, and found five things my design document had wrong - including
that the field I planned to use for opening classification contains a URL, not an opening
code. Cheapest bug fixes in the project, because none of them had been written yet."

**Where:** `docs/DESIGN.md` keeps the original assumptions; corrections land in
`src/chess_coach/chesscom/`.

---

## 2026-11-05 - Python pinned to 3.12, though 3.14 is installed `DONE`

> **In plain English:** The project runs on an older version of Python than the one installed
> on my laptop. The libraries it depends on ship ready-made installers for each Python
> version, and the newest version often has gaps - when one is missing, your computer tries to
> build the library from scratch, which on Windows means losing an afternoon. On top of that,
> my laptop, the test system and the server all have to be on the same version, or "it works
> on my machine" becomes the normal state of affairs.

**What changed:** `.python-version` pins 3.12; `requires-python = ">=3.12,<3.13"`; uv manages
the interpreter.

**Why:** The machine has Python 3.14.3 system-wide. Packages with compiled extensions
(`psycopg`, `pydantic-core`) publish prebuilt wheels per Python version, and a version that
new often has gaps - a missing wheel means pip tries to compile from source, which on Windows
means needing a C++ toolchain and an afternoon lost. 3.12 has wheels for everything here. The
project also has to run on the same version in CI and in the container, so pinning is
necessary regardless; this just pins to a boring version.

**Alternatives rejected:** *use 3.14*: nothing in the project needs it, and it trades real
setup risk for no feature. *Leave it unpinned*: guarantees local, CI and container drift apart.

**Talking point:** "I pinned 3.12 rather than the newest Python because binary wheel coverage
matters more than language features here, and because local, CI and the container have to be
the same interpreter or 'works on my machine' becomes the default failure mode."

**Where:** `.python-version`, `pyproject.toml`.

---

## 2026-11-05 - Repo moved out of OneDrive to `C:\dev\chess-coach` `DONE`

> **In plain English:** I moved the project folder off OneDrive. A project like this generates
> tens of thousands of small working files, and OneDrive tries to back up every one of them as
> it changes. While it's doing that it holds files open, which makes installs and test runs
> fail with "permission denied" and "file in use" - errors that look exactly like bugs in your
> own code and send you hunting in the wrong place. GitHub is already the backup.

**What changed:** The repository was moved from `OneDrive\Desktop\Chess coach\` to
`C:\dev\chess-coach` while it was still empty.

**Why:** A Python virtualenv and `node_modules` are tens of thousands of small files.
OneDrive tries to sync every one of them, which is slow, burns bandwidth, and - the real
problem - holds file handles open. A sync running during an install or a test run produces
"permission denied" and "file in use" errors that look like bugs in your code. Moving before
any code existed made it a one-line operation.

**Alternatives rejected:** *stay in OneDrive and gitignore the heavy directories*: gitignore
stops git from tracking them, but OneDrive still syncs them - it does not read `.gitignore`.

**Talking point:** Minor, but a real one: "cloud-sync folders and build artifacts don't mix.
Git plus GitHub is already the backup; OneDrive on top of it just adds file locks."

**Where:** repository root. Git remote unchanged.

---

## 2026-11-05 - Line endings normalized to LF via `.gitattributes` `DONE`

> **In plain English:** Windows and Linux disagree about the invisible character that marks the
> end of a line in a text file. I write the code on Windows; it runs on Linux. Without a rule
> forcing one convention, startup scripts fail with error messages that tell you nothing useful,
> and the project history shows whole files as rewritten when only one line actually moved.

**What changed:** `* text=auto eol=lf` plus explicit rules for `.py`, `.sh`, `.yml`, `.sql`.

**Why:** Development is on Windows (CRLF), while CI and the containers are Linux (LF). Without
normalization, every file committed from Windows arrives in the container with CRLF - which
makes shell scripts fail with a confusing `\r: command not found`, and makes diffs show whole
files as changed when only one line moved.

**Talking point:** "Cross-platform hygiene: I develop on Windows and deploy to Linux
containers, so the repo stores LF and checkouts convert. Otherwise a shebang line with a
carriage return breaks an entrypoint script and the error tells you nothing useful."

**Where:** `.gitattributes`.

---

## 2026-11-05 - One PR per feature, merged with a merge commit `DONE`

> **In plain English:** Every change goes through the same formal review-and-test step a team
> would use, even though I'm the only person on the project. Two things come out of that: the
> main copy of the code is always in a working state I can go back to, and every decision has
> a written explanation attached to it. That written trail is why I can still explain choices
> I made months earlier - and it's where the entries in this file come from.

**What changed:** Every feature gets a `feat/*` or `chore/*` branch, a PR with a written
description, CI, and a merge (not a squash).

**Why:** CI gates `main`, so `main` always has a working state to go back to. A merge commit
rather than a squash keeps the individual commits on `main`, which preserves the order the
work was actually done in - useful when writing the project up later. Each PR description
records what changed and why, which is where the entries in this file come from.

**Talking point:** "Every change went through a PR with CI, even solo. It kept main
deployable and gave me a written trail of why each decision was made - which is how I can
still explain choices I made months earlier."

**Where:** `.github/workflows/`, repository history.
