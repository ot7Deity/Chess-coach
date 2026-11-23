# Frontend integration: dashboard (web/) against the live API

Written 2026-11-23 from a read-only investigation. Follow the steps in order, literally.
Every command is for the **Bash tool (Git Bash)** unless it says otherwise. Paths are relative to
the repository root (written `$ROOT` below); run each `cd` from there.

## Rules for whoever executes this doc

1. **Do not edit any source file.** No code change is needed (see "Gaps"). If a verification fails,
   stop and report the exact command and its output. Do not try to fix the code.
2. **Do not stop** the API on `:8000`, or the Docker containers `chess-coach-postgres-1` and
   `chess-coach-redis-1`.
3. **Do not run `next upgrade`** or any `npm install` / `npm update`. See "Out of scope".
4. **Do not run** `chess-coach-sync` or `chess-coach-worker --kind sync`. Both call Chess.com.
5. **Do not run `web/scripts/fixtures.sql` with output redirected** into
   `web/src/lib/fixtures/deitythemenace.real.json`, because that overwrites a committed fixture. It is not a seed
   script (see "Data in the database").

---

## 1. Current state

### Already wired, and it works

| Piece | State |
|---|---|
| `web/src/lib/api.ts` | Every page gets its data through `getPlayer`, `getReport` and `addPlayer` in this file. `NEXT_PUBLIC_API_URL` set → `fetch` to the API; unset → fixtures (`web/src/lib/fixtures/mock.ts`). Trailing slashes are stripped (line 17). |
| Where the fetch runs | **In the browser.** Every caller is a `"use client"` component (`AddPlayerForm.tsx`, `Overview.tsx`, `Progress.tsx`) and goes through `useAsync` (`web/src/lib/use-async.ts`, which uses `useEffect`). The server components (`page.tsx` files) never call the API. So the browser at `http://localhost:3000` calls `http://localhost:8000` directly, which is a cross-origin request and needs CORS. |
| CORS | `src/chess_coach/api/main.py:33-38` adds `CORSMiddleware`. Its origins come from `config.py:88` (`cors_origins = "http://localhost:3000,http://localhost:3001"`, and `.env` does not override it), with methods `GET, POST` and header `Content-Type`. **Checked live:** a preflight `OPTIONS /players` from `Origin: http://localhost:3000` returns 200 with `access-control-allow-origin: http://localhost:3000`, and a GET 404 also carries the header. |
| Endpoints | All three paths, the query param and the methods match exactly (table below). |
| Response shapes | `src/chess_coach/api/schemas.py` and `web/src/lib/api-types.ts` match field for field: names, nullability and literals. The live responses below confirm it. |
| Error body | The API sends `{"detail": "Player not found"}` (a string) for a 404 and `{"detail": [ ... ]}` (an array) for a 422. `api.ts:57-66` uses a string `detail` as the message and falls back to `Request failed (NNN).` for an array. `Overview.tsx:49` and `Progress.tsx:57` turn a 404 into the "We aren't following X yet" empty state. |
| Status codes | `POST /players` returns **202**, and `res.ok` is true for 202, so it counts as success. |
| `npm run typecheck` | Passes (no output). |
| `npm run lint` | Passes (no output, `--max-warnings=0`). |
| Fixtures indicator | `web/src/components/PageShell.tsx:13-25` shows a **"Sample data"** badge and a footer only when `NEXT_PUBLIC_API_URL` is unset. The badge is rendered on the server too, so `curl` can tell whether the live API is in use. |

### Endpoint comparison (frontend call vs backend route)

| Frontend (`web/src/lib/api.ts`) | Backend (`src/chess_coach/api/players.py`) | Match |
|---|---|---|
| `:74` `GET /players/${encodeURIComponent(username)}` | `:98` `@router.get("/{username}")` → `PlayerProgress` | yes |
| `:83` `GET /players/${enc(username)}/report?time_class=${timeClass}` | `:104` `@router.get("/{username}/report")`, `time_class: TimeClass = Query(...)` → `Report` | yes |
| `:88-93` `POST /players`, JSON `{"username": ...}`, `Content-Type: application/json` | `:63` `@router.post("", status_code=202)`, body `AddPlayerRequest` | yes |
| Header `Accept: application/json` (`:50`) | Accept is CORS-safelisted. The preflight also lists it as allowed. | yes |

### Live responses (captured 2026-11-23)

```
GET /health                                         200 {"status":"ok","version":"0.1.0","env":"dev"}
GET /players/deitythemenace                         200 sync_status "complete", 4 time_classes:
      bullet 3690 games / 0 analyzed / 3690 pending, blitz 434 / 0 / 433 (+1 skipped),
      rapid 424 / 0 / 424, daily 2 / 0 / 2
GET /players/deitythemenace/report?time_class=rapid 200 games 424, analyzed_games 0,
      summary {games 423, wins 241, draws 29, losses 153, win_rate_pct 56.97, current_rating 946,
      blunders_per_game null}, rating_trend 161 points, openings 17, blunders_by_phase null,
      time_pressure null
      (bullet: 907 trend points / 50 openings; blitz: 141 / 15; daily: 2 / 0)
GET /players/zzznotarealuser123                     404 {"detail":"Player not found"}
GET /players/zzznotarealuser123/report?time_class=rapid 404 {"detail":"Player not found"}
GET /players/deitythemenace/report                  422 {"detail":[{"type":"missing",...}]}
GET /players/deitythemenace/report?time_class=foo   422 {"detail":[{"type":"literal_error",...}]}
```

### Data in the database

- `players`: one row, `deitythemenace`, `sync_status = complete`.
- `games`: 4,550 rows, all `analysis_status = 'pending'` (one blitz game is `skipped`).
- `moves`: **0 rows.** `jobs`: **0 rows.** No analysis has run, and no analysis jobs are queued.
- **`web/scripts/fixtures.sql` does not seed anything.** It is a `BEGIN TRANSACTION READ ONLY` query that
  *reads* the dev DB and prints the JSON stored in `web/src/lib/fixtures/deitythemenace.real.json`.
  It cannot insert data. Do not use it (rule 5).
- So the ingested-game sections of the report (summary, rating trend, openings) already have real
  data. The move-level sections (blunders by phase, time pressure, blunders per game) are `null`, so the
  dashboard shows "Waiting for the engine" for them. Step 4 fills a few of them by running the
  analysis worker on a small number of games.

---

## 2. Gaps and mismatches found

**There are no code mismatches between the frontend and the backend.** Paths, query params, field names,
types, nullability, error shape, status codes and CORS all agree, and typecheck and lint pass. The gaps
are all operational:

| # | Where | Expected | Actual | What this doc does |
|---|---|---|---|---|
| G1 | DB (`moves`, `jobs` tables) | Some analyzed games, so the Blunders and Time pressure sections can be checked against the live API | 0 moves, 0 jobs, so `analyzed_games = 0` in every class and `blunders_by_phase`/`time_pressure` are `null` | Step 4 queues the pending games and analyzes 10 of them |
| G2 | `POST /players` → `src/chess_coach/api/players.py:63-95` | A new player gets synced | It inserts a `players` row and a `sync-player` job. **No sync worker is running**, so a new player stays `sync_status = "pending"` with 0 games for good. Running one would call Chess.com (rule 4). | Step 7 tests the POST with a throwaway name, then deletes it (step 8) |
| G3 | `config.py:88` CORS origins | Any local origin works | Only `http://localhost:3000` and `http://localhost:3001` are allowed. `http://127.0.0.1:3000` gets **no** `access-control-allow-origin` (checked live), so every fetch fails with "Can't reach the Chess Coach server." | Always open the dashboard at `http://localhost:3000`, never `127.0.0.1` |
| G4 | `web/src/lib/api.ts:17` | The env var can be changed while the server is running | `NEXT_PUBLIC_API_URL` is inlined when `next dev` starts. Changing it needs a restart of the dev server. | Step 3 sets it on the start command |
| G5 | `next dev` (Next 16.4.2, `node_modules/next/dist/lib/upgrade/nudge.js`) | The dev server starts | Next's default `experimental.agentUpgrade: 'security'` detects that an agent is running it, prints a security-upgrade notice and **exits 1** the first time. It writes a retry receipt in `web/.next/cache/next-agentic-upgrade-retries/`. Re-running the **same command within 5 minutes** continues with a one-line warning. Each later fresh start does the same thing again. | Step 3 tells you to re-run once |
| G6 | `web/src/lib/api.ts:61` | — | For a 422, `detail` is an array, so the message shown is the generic `Request failed (422).` The landing form validates the name before sending (`AddPlayerForm.tsx:18`), so a user cannot normally reach this. | No change (cosmetic) |

---

## 3. Implementation steps

### Step 1: Confirm the backend is up and CORS allows :3000

```bash
curl -s -w "\n%{http_code}\n" http://localhost:8000/health
curl -s -o /dev/null -D - -X OPTIONS http://localhost:8000/players \
  -H "Origin: http://localhost:3000" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" | grep -i "access-control-allow-origin"
docker ps --format '{{.Names}} {{.Status}}'
```

**Pass:** the first command prints `{"status":"ok",...}` then `200`. The second prints
`access-control-allow-origin: http://localhost:3000`. The third lists `chess-coach-postgres-1` and
`chess-coach-redis-1`, both `Up ... (healthy)`.
**Fail:** stop and report. Do not start or restart anything.

### Step 2: Confirm the frontend compiles

```bash
cd web && npm run typecheck && npm run lint
```

**Pass:** both finish with exit code 0 and print nothing after their `> ...` header lines.

### Step 3: Start the dev server against the live API, in the background

1. Check that port 3000 is free:
   ```bash
   netstat -ano | grep -E ":3000 .*LISTEN" || echo "port 3000 free"
   ```
   If it prints a LISTENING line, a dev server is already running. Go to sub-step 4 to check that
   it uses the live API. If that check fails, stop and report that port 3000 is already taken.
2. Start it with the Bash tool and **`run_in_background: true`**:
   ```bash
   cd web && NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
   ```
3. If the background task **exits with code 1** and its output contains a Next.js security or upgrade
   notice ("This command stopped to show the upgrade recommendation. Retry the same command..."),
   run **exactly the same command again** with `run_in_background: true`, within 5 minutes (gap G5).
   Do **not** run the `next upgrade ...` command the notice suggests. The second run prints a one-line
   warning and keeps running.
4. Wait until it serves pages. The first compile can take up to about a minute:
   ```bash
   for i in $(seq 1 60); do c=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:3000/); [ "$c" = "200" ] && echo "up" && break; sleep 2; done; echo "last status: $c"
   ```
   **Pass:** prints `up`.
5. Check that it is **not** using fixtures:
   ```bash
   curl -s http://localhost:3000/ | grep -c "Sample data"
   ```
   **Pass:** prints `0`. If it prints `1` or more, the env var did not reach Next. Stop the dev server
   you started (the background task), and start it again with the exact command from sub-step 2.

### Step 4: Put analysis data in the DB (fills gap G1)

This queues an `analyze-game` job for every pending game (about 4,550 rows in `jobs`, harmless), then
analyzes **10** games with Stockfish and exits. Rapid, blitz and daily jobs have priority 10 and bullet
20, so the 10 games come from rapid, blitz or daily. This runs locally only. It does not call Chess.com.

Run with the Bash tool and **`run_in_background: true`** (expect about 1 to 10 minutes):

```bash
uv run chess-coach-worker --kind analysis --enqueue-pending --max-jobs 10
```

While it runs, step 5's progress-page check can be watched live. Once the task has exited with code 0, run:

```bash
for tc in rapid blitz daily bullet; do printf "%s " $tc; curl -s "http://localhost:8000/players/deitythemenace/report?time_class=$tc" | uv run python -c "import json,sys; r=json.load(sys.stdin); print('analyzed_games=', r['analyzed_games'], 'bbp_null=', r['blunders_by_phase'] is None, 'tp_null=', r['time_pressure'] is None)"; done
```

**Pass:** at least one of rapid, blitz or daily shows `analyzed_games=` greater than 0 with
`bbp_null= False`. For rapid and blitz, `tp_null= False` too. For daily, `tp_null= True` is correct
(the contract says so). Write down one class with `analyzed_games > 0` and `tp_null= False`. It is
called **`$TC`** below (use `rapid` or `blitz`).
**Fail** (worker exits non-zero): report its last 30 lines of output. Do not retry with other flags.

### Step 5: Check each dashboard page through the dev server (server side)

Each `curl` checks that the page shell renders with status 200 and does not show the fixtures badge. The data
itself is fetched by the browser (step 6).

```bash
for p in "/" "/players/deitythemenace" "/players/deitythemenace?tc=rapid" "/players/deitythemenace/progress" "/players/zzznotarealuser123" "/players/zzznotarealuser123/progress"; do
  out=$(curl -s -w "\n%{http_code}" "http://localhost:3000$p"); printf "%-45s %s %s\n" "$p" "$(echo "$out" | tail -n 1)" "$(echo "$out" | grep -c 'Sample data')"; done
```

**Pass:** every line shows `200 0`.

Also check, from the frontend's origin, the exact requests the browser will make:

```bash
H='-H Origin:http://localhost:3000'
curl -s -o /dev/null -w "%{http_code} " $H http://localhost:8000/players/deitythemenace; \
curl -s -D - -o /dev/null $H "http://localhost:8000/players/deitythemenace/report?time_class=rapid" | grep -i "^access-control-allow-origin"; \
curl -s -o /dev/null -w "%{http_code}\n" $H http://localhost:8000/players/zzznotarealuser123
```

**Pass:** prints `200 access-control-allow-origin: http://localhost:3000` and then `404`.

### Step 6: Check in a browser (only if you have a browser tool)

If you have a browser tool (for example Claude in Chrome or the built-in browser), open each URL below
at **`http://localhost:3000`** (not `127.0.0.1`, gap G3). On each one, check the console and the network
log. If you have no browser tool, skip to step 7 and write "browser checks not run" in your report.

| URL | You should see | Network / console |
|---|---|---|
| `/` | Hero "Find out why you lose.", a username box. **No** "Sample data" badge in the header, and no hint "Sample data: try deitythemenace". | No requests to `:8000`. No console errors. |
| `/players/deitythemenace` | Header `deitythemenace`, "Synced ... · N of 4,550 games analyzed · Progress". Time-control switcher with Bullet 3,690 / Blitz 434 / Rapid 424 / Daily 2. The bullet report is the default (it has the most games): record 1,804 wins / 49 draws / 1,837 losses over 3,690 rated games, rating chart, openings table. Blunders and Time pressure say "Waiting for the engine" (bullet was not analyzed). A banner says "The engine has analyzed 0 of 3,690 bullet games so far...". | `GET :8000/players/deitythemenace` 200 and `GET :8000/players/deitythemenace/report?time_class=bullet` 200. No CORS errors. No console errors. |
| `/players/deitythemenace?tc=$TC` | The `$TC` report. The **Blunders** section shows a bar list by phase, and **Time pressure** shows a table with "30 s or more" / "Under 30 s" (or "You never dropped under 30 seconds"). | `report?time_class=$TC` 200. No console errors. |
| `/players/deitythemenace?tc=daily` | Record 1 win / 0 draws / 1 loss. Time pressure says "Not measured for daily games". | 200s only. |
| Click each segment of the switcher | The URL changes to `?tc=...` and the report reloads without a full page load. | One `report?time_class=...` 200 per click. |
| `/players/deitythemenace/progress` | "Analyzing your games", "N of 4,549 games analyzed", per-class meters, "refreshes every 5 seconds". | `GET :8000/players/deitythemenace` 200, **repeated about every 5 s** (pending games remain). No console errors. |
| `/players/zzznotarealuser123` | "We aren't following zzznotarealuser123 yet" with an "Analyze their games" button. | One `GET :8000/players/zzznotarealuser123` **404. This is expected**, and the browser may log it as a failed resource load. No CORS error and no other errors. |
| `/players/Bad%20Name!` | "That isn't a Chess.com username". | No requests to `:8000`. |

### Step 7: Test adding a player (POST, gap G2)

With curl (this inserts the throwaway player `zz_integration_test`):

```bash
curl -s -w "\n%{http_code}\n" -X POST http://localhost:8000/players \
  -H "Origin: http://localhost:3000" -H "Content-Type: application/json" \
  -d '{"username":"ZZ_Integration_Test"}'
```

**Pass:** status `202`, and the body has `"username":"zz_integration_test"` (lowercased),
`"sync_status":"pending"` and four `time_classes`, each with `"games":0`.

If you have a browser tool: open `http://localhost:3000/`, type `zz_integration_test`, and click
**Analyze my games**. **Pass:** you are taken to `/players/zz_integration_test/progress`, which shows
"Downloading your games" and keeps polling every 5 s. It stays like that, because no sync worker runs (G2).
That is the expected result. The network log shows `POST :8000/players` 202, with no CORS error and no preflight failure.

Validation error path:

```bash
curl -s -w "\n%{http_code}\n" -X POST http://localhost:8000/players -H "Content-Type: application/json" -d '{"username":"bad name!"}'
```

**Pass:** `422` with `{"detail":[...]}`.

### Step 8: Clean up the throwaway player

Read-write SQL, limited to the test row only:

```bash
docker exec chess-coach-postgres-1 psql -U chess -d chess_coach -v ON_ERROR_STOP=1 \
  -c "DELETE FROM jobs WHERE payload->>'username' = 'zz_integration_test';" \
  -c "DELETE FROM players WHERE username = 'zz_integration_test';"
curl -s -w "\n%{http_code}\n" http://localhost:8000/players/zz_integration_test
```

**Pass:** `DELETE 1` and `DELETE 1`, then `{"detail":"Player not found"}` and `404`.
(If step 7 was done both by curl and in the browser, the counts are still 1 and 1, because the row is
idempotent.)
Do **not** delete anything else. The `analyze-game` jobs from step 4 stay, which is intended.

### Step 9: Leave things running

Leave the dev server (step 3), the API, and the containers running. Do not stop anything.

---

## 4. End-to-end checklist (copy this into your report and mark each line)

- [ ] Step 1: `/health` 200, and CORS preflight from `http://localhost:3000` is allowed
- [ ] Step 2: `npm run typecheck` and `npm run lint` exit 0
- [ ] Step 3: dev server up on :3000 with `NEXT_PUBLIC_API_URL=http://localhost:8000`, and `Sample data` count is 0
- [ ] Step 4: worker exited 0, and `$TC` = ___ has `analyzed_games` > 0 with non-null `blunders_by_phase` and `time_pressure`
- [ ] Step 5: all six pages `200 0`, and the cross-origin curl checks pass
- [ ] Step 6: browser checks: `/` · overview (bullet) · overview `?tc=$TC` · `?tc=daily` · switcher clicks · progress (polls every 5 s) · unknown user (only the expected 404) · invalid name. No console errors, and no failed network requests other than the expected 404. (Or write: "browser checks not run, no browser tool".)
- [ ] Step 7: POST 202 lowercased with `sync_status` pending, and the bad name gives 422
- [ ] Step 8: test player deleted, GET returns 404
- [ ] No source files modified (`git status` is not available, since this folder is not a git repo. Confirm you used no Edit or Write on any file.)

---

## 5. Out of scope

- **Next.js security advisory.** `next dev` on 16.4.2 reports that the installed version is affected by a
  known security vulnerability and recommends `next upgrade --agent=security ...`. The advisory itself
  was not identified during this investigation. Upgrading is a separate decision for the project owner.
  Do not run it here. To silence the agent nudge (G5) permanently, the owner could set
  `experimental: { agentUpgrade: false }` in `web/next.config.ts`. That is **not** part of this doc. Do not do it.
- Running a sync worker so that newly added players actually download (G2). It calls Chess.com and needs
  `CHESSCOM_CONTACT` in `.env`.
- Full analysis of the 4,550-game backlog (step 4 analyzes only 10 games).
- Allowing `127.0.0.1` origins (G3). That would be a change to `CORS_ORIGINS`, and the API is not to be restarted.
