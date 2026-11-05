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

## 2026-11-05 - The engine service gets extracted after M3, not before `DECIDED`

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
