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

<!-- M3 ENTRIES BELOW (position cache, scaling, metrics). Newest first. -->

<!-- end of M3 entries -->

---

<!-- M2 ENTRIES BELOW (job queue, Stockfish, analysis worker). Newest first. -->

<!-- end of M2 entries -->

---

<!-- M1 ENTRIES BELOW (ingestion). Newest first. -->

## 2026-11-05 - Chess.com's months run on California time, not world time `DONE`

> **In plain English:** Chess.com groups your games into months. I assumed those months
> follow world standard time (UTC), the clock most computer systems use. My reviewer agent
> checked every one of my 4,550 games against the month it was filed under, and found 71 that
> didn't fit: games that finished just after midnight on the 1st were filed under the
> *previous* month. Every single one of the 4,550 fit perfectly once you use California time
> instead. My code happened to work anyway - it already waits a full day after a month ends
> before treating it as finished, and the difference is at most eight hours - but my written
> explanation of *why* it waits was wrong, and the monthly reports would have quietly
> disagreed with Chess.com's numbers.

**A bit deeper:** The archive endpoint for a month returns games whose end time falls in
that month in `America/Los_Angeles`. A UTC month therefore keeps receiving games for 7-8
hours after it ends. Correctness here came from margin rather than understanding: the 24-hour
grace was chosen for a different reason ("a game finishing at 23:59 might not be indexed by
00:00") and happens to cover the real one.

**How it was found:** the reviewer's live acceptance test on `deitythemenace` recorded, for
every game, which month's archive it came from, and compared that with its `end_time` in UTC
and in Pacific time: 71 of 4,550 (1.6%) mismatched in UTC, 0 in Pacific. The largest gap
was 7 h 26 min past UTC midnight.

**What changed:** the `ARCHIVE_GRACE` comment and the M1 entry's explanation now give the
measured mechanism. The 24-hour value stays.

**Consequence for M4:** a report that groups games by UTC month will not match Chess.com's
monthly archive counts - about 1.6% of games land in a different month. Reports should state
which they use; grouping by the player's own time zone is probably what a person expects.

**Talking point:** "My code was right for the wrong reason. I'd guessed why Chess.com's
monthly archives lag at midnight; the reviewer agent tested it against all 4,550 of my games
and found the archives are bucketed in Pacific time - 71 games filed under the previous month
in UTC, zero in Pacific. The safety margin I'd picked happened to cover an eight-hour skew.
I'd rather know that than be lucky, because the next report I write groups by month."

**Where:** `src/chess_coach/ingest/sync.py` (`ARCHIVE_GRACE`), the reviewer's
`tests/review/test_live_deitythemenace.py` on `review/adversarial`.

---

## 2026-11-05 - The comment said "give up", the code said "wait 25 minutes" `DONE`

> **In plain English:** When Chess.com is overloaded it can reply "try again in N seconds".
> I wrote a rule: if it asks us to wait more than five minutes, something is badly wrong,
> so stop and report a failure. The note above the code said exactly that. The code did
> something else: it shortened the wait to five minutes and tried again - five times. So a
> server asking for an hour would have kept the program asleep for 25 minutes, holding up
> every other download, before finally giving up. My reviewer agent caught it by reading
> the note and the code side by side.

**A bit deeper:** `Retry-After` is an HTTP header a server sends with "too many requests"
or "temporarily unavailable" responses. The intent was a *ceiling*: honour short waits,
refuse long ones. The code implemented a *clamp*, which turns "refuse" into "wait the
maximum, repeatedly". The bug was invisible to the existing tests because they only used
short waits.

**What changed:** A server-requested wait above 300 s now raises `ChessComUnavailable`
immediately. The shared rate-limit bucket is still penalised, by 300 s rather than the full
value, so the other workers back off without stalling for an hour. The ceiling applies only to
waits the server asks for, not to our own configured backoff (there is a test for that too).

**Talking point:** "A small one, but a good example of why the reviewer reads comments
against code: my comment said 'above five minutes, give up', and the code clamped to five
minutes and retried five times. One `min()` was the difference between failing in a
millisecond and stalling every worker for 25 minutes."

**Where:** `src/chess_coach/chesscom/client.py` (`_get`, `_retry_after`),
`tests/test_chesscom_client.py::test_an_absurd_retry_after_fails_fast_instead_of_sleeping`.

---

## 2026-11-05 - Four ways the sync could get stuck forever or lose a game quietly `DONE`

> **In plain English:** After the download feature worked on my real account, my reviewer
> agent went looking for the unusual situations that break it, and found four. If Chess.com
> ever listed the same game twice in a month, the download would fail on that month every
> single time, forever. If I stopped the program with Ctrl-C, my account would say "syncing"
> forever. If one game confused the program, it would skip it - and then never look at that
> month again, even after I fixed the code, so the game would be gone for good. And if
> Chess.com sent back a broken reply, the program would read it as "you played no games
> this month" and lock that in. None of these happened with my real data. They are the kind
> of bug that shows up months later, for someone else, with no clue where to look.

**A bit deeper:** All four are the same shape: a rare input turns into a *permanent* bad
state, because the system remembers its mistakes. A month marked "finished" is never fetched
again; a status column is only reset by the code path that was skipped; a database statement
that fails fails identically on every retry. The fixes make each of those states either
impossible or recoverable, and each has a test that reproduces it.

**What changed / the four stories:**

1. **A duplicate game wedged the sync.** *Symptom:* every sync of the player fails at the
   same month. *Cause:* a month is inserted as one `INSERT ... ON CONFLICT DO UPDATE`, and
   Postgres refuses to let one statement update the same row twice
   (`CardinalityViolation`). The month never commits, so every retry hits it again. *Fix:*
   deduplicate by uuid before inserting, with a warning.
2. **Ctrl-C left the player "syncing" forever.** *Cause:* the error handler caught
   `Exception`, and `KeyboardInterrupt` / `SystemExit` are not Exceptions in Python. The
   advisory lock was still released (the reviewer verified every exit path), but the status
   column was not. *Fix:* `except BaseException`. A hard kill (`kill -9`) can still leave
   it, which is why a later version should derive "syncing" from `pg_locks` instead of a
   column.
3. **A game the parser rejected was lost for good.** *Cause:* the month was still marked
   complete, so it was never fetched again, and its ETag meant even a re-fetch would get a
   304. *Fix:* a month with a rejected game is never frozen and its ETag is dropped, so the
   next sync downloads it in full and a parser fix recovers the game. The test fakes the
   parser fix and checks the game arrives.
4. **A malformed reply read as an empty month.** *Cause:* `resp.json().get("games", [])`.
   *Fix:* a 200 without a `games` list raises, and the month is retried later instead of
   being frozen with zero games.

**Alternatives rejected:** *catching the CardinalityViolation and retrying row by row* -
slower on every month to handle a case that has never happened; deduplicating up front is
one dictionary.

**Talking point:** "The reviewer agent's best finds were rare inputs that become permanent
states. The sync remembers that a month is finished, so any mistake made while finishing it
is remembered too. A duplicate game in Chess.com's JSON would have failed one month on every
retry forever, and a game my parser rejected would have been skipped and then never looked at
again. Each fix either makes the bad state impossible or makes it recoverable, and each one
has a test that reproduces it."

**Where:** `src/chess_coach/ingest/sync.py` (`_parse_month`, `_sync_months`, the
`BaseException` handler), `src/chess_coach/chesscom/client.py` (`month`), tests in
`tests/test_sync.py` and `tests/test_chesscom_client.py`.

---

## 2026-11-05 - The cache was built on a promise the chess engine didn't keep `DONE`

> **In plain English:** The whole speed-up plan rests on one idea: if the chess engine has
> already judged a board position, save the answer and reuse it, because the same position
> always gets the same answer. My reviewer (an AI agent I set up whose only job is to try to
> break things) tested that promise against the real engine, and it was false. The engine
> gave one position four completely different scores depending on how close the game was to
> a "draw after 50 quiet moves" rule - a number my saved answers ignored. It also gave
> different answers depending on what it had been thinking about just before. So whichever
> game happened to look at a position first would have decided the score for every other
> game. The fix makes the engine forget everything before each position and always see it
> fresh, so the same position really does get the same answer.

**A bit deeper:** A cache is only correct if the saved value depends on nothing except the
cache key. Here the key is "this board, this engine version, this search depth". The reviewer
found two hidden inputs: the *halfmove clock* (the 50-move-rule counter, which Stockfish uses
to pull scores towards a draw) and the engine's *transposition table* (its short-term memory
of positions it searched recently, which steers later searches). Neither is in the key. The
fix removes both inputs instead of adding them to the key: the engine is shown each position
with the counter at zero and no move history, its memory is cleared first, and it runs on one
thread, which makes the result a pure function of the key.

**What changed:** `positions.engine_board(board)` returns the board the engine must be given
(normalised FEN, halfmove clock 0, no move stack). The contract now requires the worker to
clear the hash (`ucinewgame`) before every position and to run `Threads=1`, and documents
`mate_in = 0` ("side to move is already checkmated"), which cannot be flipped to the other
side's point of view by negation because `-0 == 0`. A real-engine test proves the guarantee:
the same KRK position with the clock at 0 and at 90 scores differently raw and identically
through `engine_board`.

**Why:** The evidence, from the reviewer's tests against the pinned Stockfish 18:

| Halfmove clock | Score (depth 12) |
| --- | --- |
| 0 | +529 |
| 50 | +359 |
| 90 | +11 |
| 98 | 0 |

and, across one 34-ply game, 31 positions scored differently with the hash kept between
positions (the natural way to write a worker loop) than with it cleared - by up to 42
centipawns, sometimes with a different best move. Cold, single-thread searches repeated
exactly. With the original contract, first-write-wins would have frozen whichever of those
answers arrived first, and the same move could be graded "good" in one game and "inaccuracy"
in another.

This is the **fifth** time this project has hit "the value depends on something the key
doesn't name" - after the `positions` key, the CI engine cache, the engine image and the
`games` idempotency key - and the first one where the fix was to remove the hidden input
rather than add it to the key. Adding the halfmove clock to the key would have split every
position into up to 100 cache entries and destroyed the hit rate the cache exists for.

**Alternatives rejected:**
- *Put the halfmove clock in the key*: correct, and multiplies cache entries by up to 100.
- *Keep the warm hash and stop claiming reproducibility*: faster per position, but the cache
  would then serve whichever answer won a race, and the "with cache vs without" benchmark
  would compare two different things.
- *Last-write-wins in the cache*: just picks a different arbitrary answer.

**Talking point:** "My cache rested on 'same position, same depth, same answer'. The reviewer
agent tested it against the real engine and it was false twice over: Stockfish's score moves
with the 50-move counter, and with whatever is left in its hash table from the previous
position. Instead of adding those to the key, which would have wrecked the hit rate, I made
them stop mattering - every position is searched from a clean board with a cleared hash -
and there's now a test that runs the reviewer's counter-example through the engine and
requires identical results. The speed cost of clearing the hash is measured in the M2
entries."

**Where:** `src/chess_coach/positions.py` (`engine_board`, the `Evaluation` and `EvalCache`
docstrings), `tests/test_positions.py::test_an_evaluation_is_a_function_of_its_cache_key`.
The reviewer's original tests are on `review/adversarial` under `tests/review/`.

---

## 2026-11-05 - I reformatted other agents' half-written code by accident `DONE`

> **In plain English:** Several AI agents build different parts of this project at the same
> time, each in its own copy of the code. Those copies live in a hidden folder *inside* my
> main copy. I ran a tidy-up tool on "everything in this folder" - and it went into their
> copies too, rearranging code they were still in the middle of writing. No harm was done,
> because the tool only changes spacing and layout, and they would have run it themselves
> before saving. But it could just as easily have been a tool that deletes or rewrites
> files. The fix is to tell every tool that the hidden folder is off-limits.

**A bit deeper:** Each agent works in a *git worktree* - a second checkout of the same
repository on a different branch. The tooling put those worktrees under `.claude/worktrees/`
inside the main checkout. Git knows they are separate, but other tools do not: to `ruff`
they are just more Python files under the current directory. Repo-wide commands from the
main checkout therefore reached into other branches' uncommitted work.

**What changed:** `.claude/` is excluded from ruff (`extend-exclude`) and gitignored.

**Why:** Formatting is deterministic and the agents format before every commit, so this
instance cost nothing. The class of bug is the problem: any repo-wide script (a codemod, a
`find ... -delete`, a coverage run) would have had the same reach.

**Talking point:** "Running parallel agents in nested worktrees has a sharp edge: git knows
they're separate checkouts, but nothing else does. I found it by tripping over it - a format
run reached into another branch's work in progress. Excluding the directory is one line; the
lesson is that isolation has to hold for every tool, not just the version control."

**Where:** `pyproject.toml` (`[tool.ruff] extend-exclude`), `.gitignore`.

---

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

- *A month is final a day after it ends, not at midnight.* Chess.com files games by their
  end date in **US Pacific time**, so a UTC month keeps gaining games for up to eight hours
  after it ends (see the "Pacific time" entry above - my first explanation here was wrong).
  Freezing at UTC midnight would lose them forever. The grace day costs at most one extra
  304.
- *One commit per month.* A crash mid-sync leaves the finished months marked done, so a
  resumed sync re-requests only what it had not finished - and nothing is half-written,
  because a month's games and its archive row commit together.
- *The upsert never touches analysis state.* A re-ingested game keeps its
  `analysis_status`; otherwise every re-sync would send finished games back to the queue. The
  only field allowed to change is Chess.com's accuracy score, which appears if someone
  requests a review. (Only while the month is still being re-fetched - the current month plus
  the grace day. A review requested after a month is frozen is not picked up; that is fine,
  because these numbers are a cross-check, not a report input.) A `WHERE` on the
  `ON CONFLICT DO UPDATE` means re-downloading an
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
check what it actually returns. Seven assumptions in the design doc were wrong.

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
API and reading the response, and found seven things my design document had wrong - including
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

## 2026-11-05 - Repo moved out of OneDrive to `C:\dev\chess-coach` `DECIDED`

> **In plain English:** I moved the project folder off OneDrive. A project like this generates
> tens of thousands of small working files, and OneDrive tries to back up every one of them as
> it changes. While it's doing that it holds files open, which makes installs and test runs
> fail with "permission denied" and "file in use" - errors that look exactly like bugs in your
> own code and send you hunting in the wrong place. GitHub is already the backup.

**Status update (M1):** this had quietly regressed. The working checkout ended up back under
`OneDrive\Desktop\chess\Chess-coach`, so this entry said `DONE` for something that was no
longer true - which the reviewer agent caught, and then tripped over exactly as predicted:
deleting a temporary worktree failed with "Access denied" on a locked `.pyd` file. With four
agents' worktrees (each with its own virtualenv) inside the folder, the exposure was far
larger than when this entry was written. OneDrive is stopped for now and the move to
`C:\dev\chess-coach` happens once the parallel agents finish their current round. The lesson
worth telling: a decision log needs re-checking against reality, or it turns into a list of
things that used to be true.

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
