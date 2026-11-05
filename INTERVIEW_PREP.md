# Interview Prep: decision log

Every meaningful change and design decision in this project, with the reasoning, the
alternatives that were rejected, and how to talk about it. **Newest entry first.**

This exists because a project is only worth putting on a resume if you can defend it. Read
the "Talking point" lines before an interview; read the "Why" lines when you need to actually
understand the system.

**Entry format:** What changed / Why / Alternatives rejected / Talking point / Where.
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

---

## 2026-11-05 - Classify blunders on capped evaluation, not raw centipawn loss `DECIDED`

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
