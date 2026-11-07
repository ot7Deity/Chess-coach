/**
 * A lightweight stub API server for testing the dashboard against a live API.
 * Serves contract-conformant JSON without dependencies, using only Node.js built-in modules.
 *
 * Serves:
 *   GET /players/{username}
 *   GET /players/{username}/report?time_class={bullet|blitz|rapid|daily}
 *   POST /players
 *
 * Demo players:
 *   deitythemenace  returns sample data
 *   newplayer       returns syncing state
 *   nosuchplayer    returns sync error
 *   servererror     returns 500 on all requests
 *   (any other)     returns 404 until POSTed
 *
 * Usage: node stub-api.mjs [port] [--seed-data]
 * Default port: 8765
 * --seed-data: preload sample players from fixtures (requires --input-type=module)
 */

import http from "http";
import url from "url";

const PORT = parseInt(process.argv[2] ?? "8765", 10);

// In-memory store of added players via POST /players
const addedPlayers = new Set();

// Demo fixture data
const DEMO_DATA = {
  deitythemenace: {
    progress: {
      username: "deitythemenace",
      sync_status: "complete",
      sync_error: null,
      last_synced_at: "2026-11-06T16:26:41Z",
      analyzed_last_hour: 12,
      time_classes: [
        {
          time_class: "bullet",
          games: 434,
          analyzed: 350,
          pending: 80,
          failed: 3,
          skipped: 1,
          current_rating: 646,
        },
        {
          time_class: "blitz",
          games: 430,
          analyzed: 420,
          pending: 8,
          failed: 1,
          skipped: 1,
          current_rating: 1650,
        },
        {
          time_class: "rapid",
          games: 424,
          analyzed: 424,
          pending: 0,
          failed: 0,
          skipped: 0,
          current_rating: 1946,
        },
        {
          time_class: "daily",
          games: 3,
          analyzed: 3,
          pending: 0,
          failed: 0,
          skipped: 0,
          current_rating: 1234,
        },
      ],
    },
    report: {
      bullet: {
        username: "deitythemenace",
        time_class: "bullet",
        generated_at: "2026-11-06T16:26:41Z",
        games: 434,
        analyzed_games: 350,
        summary: {
          games: 431,
          wins: 217,
          draws: 14,
          losses: 200,
          win_rate_pct: 50.35,
          current_rating: 646,
          blunders_per_game: 1.2,
        },
        rating_trend: [
          { date: "2026-10-15", rating: 600, games: 10 },
          { date: "2026-10-16", rating: 620, games: 15 },
        ],
        openings: [
          {
            opening: "French Defense",
            color: "black",
            games: 45,
            wins: 18,
            draws: 2,
            losses: 25,
            win_rate_pct: 40.0,
            rating_change: -75,
          },
        ],
        blunders_by_phase: [
          { phase: "opening", player_moves: 1200, blunders: 15 },
          { phase: "middlegame", player_moves: 3400, blunders: 280 },
          { phase: "endgame", player_moves: 2100, blunders: 140 },
        ],
        time_pressure: {
          threshold_seconds: 30,
          low_clock_games: 120,
          low_clock: {
            moves: 450,
            accuracy_pct: 55.0,
            blunders: 95,
          },
          normal_clock: {
            moves: 6250,
            accuracy_pct: 80.0,
            blunders: 340,
          },
        },
      },
      blitz: {
        username: "deitythemenace",
        time_class: "blitz",
        generated_at: "2026-11-06T16:26:41Z",
        games: 430,
        analyzed_games: 420,
        summary: {
          games: 430,
          wins: 218,
          draws: 15,
          losses: 197,
          win_rate_pct: 50.7,
          current_rating: 1650,
          blunders_per_game: 0.85,
        },
        rating_trend: [
          { date: "2026-09-01", rating: 1500, games: 8 },
          { date: "2026-09-02", rating: 1520, games: 12 },
        ],
        openings: [],
        blunders_by_phase: [
          { phase: "opening", player_moves: 2800, blunders: 50 },
          { phase: "middlegame", player_moves: 8900, blunders: 250 },
          { phase: "endgame", player_moves: 4200, blunders: 135 },
        ],
        time_pressure: {
          threshold_seconds: 30,
          low_clock_games: 200,
          low_clock: {
            moves: 1200,
            accuracy_pct: 62.0,
            blunders: 240,
          },
          normal_clock: {
            moves: 14900,
            accuracy_pct: 82.5,
            blunders: 195,
          },
        },
      },
      rapid: {
        username: "deitythemenace",
        time_class: "rapid",
        generated_at: "2026-11-06T16:26:41Z",
        games: 424,
        analyzed_games: 424,
        summary: {
          games: 423,
          wins: 241,
          draws: 29,
          losses: 153,
          win_rate_pct: 56.97,
          current_rating: 1946,
          blunders_per_game: 0.94,
        },
        rating_trend: [
          { date: "2026-07-15", rating: 1800, games: 5 },
          { date: "2026-07-16", rating: 1820, games: 8 },
        ],
        openings: [
          {
            opening: "Caro-Kann Defense",
            color: "black",
            games: 31,
            wins: 13,
            draws: 2,
            losses: 16,
            win_rate_pct: 41.9,
            rating_change: -54,
          },
        ],
        blunders_by_phase: [
          { phase: "opening", player_moves: 4210, blunders: 38 },
          { phase: "middlegame", player_moves: 7905, blunders: 231 },
          { phase: "endgame", player_moves: 3550, blunders: 129 },
        ],
        time_pressure: {
          threshold_seconds: 30,
          low_clock_games: 61,
          low_clock: {
            moves: 702,
            accuracy_pct: 58.1,
            blunders: 41,
          },
          normal_clock: {
            moves: 14963,
            accuracy_pct: 84.2,
            blunders: 357,
          },
        },
      },
      daily: {
        username: "deitythemenace",
        time_class: "daily",
        generated_at: "2026-11-06T16:26:41Z",
        games: 3,
        analyzed_games: 3,
        summary: {
          games: 2,
          wins: 1,
          draws: 0,
          losses: 1,
          win_rate_pct: 50.0,
          current_rating: 1234,
          blunders_per_game: 0.33,
        },
        rating_trend: [
          { date: "2026-08-01", rating: 1100, games: 1 },
          { date: "2026-09-15", rating: 1150, games: 1 },
        ],
        openings: [],
        blunders_by_phase: [
          { phase: "opening", player_moves: 30, blunders: 0 },
          { phase: "middlegame", player_moves: 45, blunders: 1 },
          { phase: "endgame", player_moves: 20, blunders: 0 },
        ],
        time_pressure: null,
      },
    },
  },
};

/**
 * Get player progress by username
 */
function getPlayerProgress(username) {
  const lower = username.toLowerCase().trim();

  if (lower === "servererror") {
    return { status: 500, body: { detail: "Internal Server Error" } };
  }

  if (lower === "deitythemenace") {
    return { status: 200, body: DEMO_DATA.deitythemenace.progress };
  }

  if (lower === "nosuchplayer") {
    return {
      status: 200,
      body: {
        username: lower,
        sync_status: "error",
        sync_error: `Chess.com has no player named "${lower}".`,
        last_synced_at: null,
        analyzed_last_hour: 0,
        time_classes: [
          { time_class: "bullet", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
          { time_class: "blitz", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
          { time_class: "rapid", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
          { time_class: "daily", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
        ],
      },
    };
  }

  if (lower === "newplayer" || addedPlayers.has(lower)) {
    return {
      status: 200,
      body: {
        username: lower,
        sync_status: "syncing",
        sync_error: null,
        last_synced_at: null,
        analyzed_last_hour: 0,
        time_classes: [
          { time_class: "bullet", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
          { time_class: "blitz", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
          { time_class: "rapid", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
          { time_class: "daily", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
        ],
      },
    };
  }

  // Unknown player
  return { status: 404, body: { detail: "Player not found" } };
}

/**
 * Get report for a player and time control
 */
function getReport(username, timeClass) {
  const lower = username.toLowerCase().trim();
  const validTimeClasses = ["bullet", "blitz", "rapid", "daily"];

  // Validate time_class
  if (!validTimeClasses.includes(timeClass)) {
    return { status: 422, body: { detail: `Invalid time_class: ${timeClass}` } };
  }

  if (lower === "servererror") {
    return { status: 500, body: { detail: "Internal Server Error" } };
  }

  if (lower === "deitythemenace") {
    const report = DEMO_DATA.deitythemenace.report[timeClass];
    if (report) {
      return { status: 200, body: report };
    }
  }

  // Unknown player
  return { status: 404, body: { detail: "Player not found" } };
}

/**
 * Add a player
 */
function addPlayer(username) {
  const lower = username.toLowerCase().trim();

  // Validate username pattern
  if (!/^[a-z0-9_-]{1,50}$/.test(lower)) {
    return { status: 422, body: { detail: "Invalid username pattern" } };
  }

  if (lower === "servererror") {
    return { status: 500, body: { detail: "Internal Server Error" } };
  }

  addedPlayers.add(lower);

  // Return progress as if just added
  return {
    status: 202,
    body: {
      username: lower,
      sync_status: "syncing",
      sync_error: null,
      last_synced_at: null,
      analyzed_last_hour: 0,
      time_classes: [
        { time_class: "bullet", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
        { time_class: "blitz", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
        { time_class: "rapid", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
        { time_class: "daily", games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null },
      ],
    },
  };
}

/**
 * CORS headers
 */
function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "http://localhost:3000",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

/**
 * Route handler
 */
async function handleRequest(req, res) {
  const pathname = url.parse(req.url).pathname;
  const search = url.parse(req.url).search || "";
  const query = new URLSearchParams(search);

  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, corsHeaders());
    res.end();
    return;
  }

  const commonHeaders = {
    ...corsHeaders(),
    "Content-Type": "application/json",
  };

  // GET /players/{username}
  const playerMatch = pathname.match(/^\/players\/([^/?]+)$/);
  if (req.method === "GET" && playerMatch) {
    const username = decodeURIComponent(playerMatch[1]);
    const result = getPlayerProgress(username);
    res.writeHead(result.status, commonHeaders);
    res.end(JSON.stringify(result.body));
    return;
  }

  // GET /players/{username}/report
  const reportMatch = pathname.match(/^\/players\/([^/?]+)\/report$/);
  if (req.method === "GET" && reportMatch) {
    const username = decodeURIComponent(reportMatch[1]);
    const timeClass = query.get("time_class");

    if (!timeClass) {
      res.writeHead(422, commonHeaders);
      res.end(JSON.stringify({ detail: "Missing required parameter: time_class" }));
      return;
    }

    const result = getReport(username, timeClass);
    res.writeHead(result.status, commonHeaders);
    res.end(JSON.stringify(result.body));
    return;
  }

  // POST /players
  if (req.method === "POST" && pathname === "/players") {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk.toString();
    });
    req.on("end", () => {
      try {
        const payload = JSON.parse(body);
        const username = payload.username;
        if (!username) {
          res.writeHead(422, commonHeaders);
          res.end(JSON.stringify({ detail: "Missing field: username" }));
          return;
        }
        const result = addPlayer(username);
        res.writeHead(result.status, commonHeaders);
        res.end(JSON.stringify(result.body));
      } catch (e) {
        res.writeHead(400, commonHeaders);
        res.end(JSON.stringify({ detail: "Invalid JSON" }));
      }
    });
    return;
  }

  // 404 for unknown routes
  res.writeHead(404, commonHeaders);
  res.end(JSON.stringify({ detail: "Not found" }));
}

const server = http.createServer(handleRequest);

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Stub API server running on http://127.0.0.1:${PORT}`);
  console.log(`  GET  /players/{username}`);
  console.log(`  GET  /players/{username}/report?time_class={bullet|blitz|rapid|daily}`);
  console.log(`  POST /players`);
  console.log(`\nDemo players:`);
  console.log(`  deitythemenace  → sample data`);
  console.log(`  newplayer       → syncing state`);
  console.log(`  nosuchplayer    → sync error`);
  console.log(`  servererror     → 500 on all requests`);
  console.log(`  (other)         → 404 until POSTed`);
});

process.on("SIGINT", () => {
  console.log("\nShutting down...");
  server.close();
  process.exit(0);
});
