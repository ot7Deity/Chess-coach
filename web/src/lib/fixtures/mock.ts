/**
 * The fixture-backed stand-in for the M4 API, used when NEXT_PUBLIC_API_URL is
 * unset. Same signatures as the real client in ../api.ts, same errors, plus a
 * little latency so loading states are visible in development.
 *
 * Demo players:
 *   deitythemenace  real ingested games (deitythemenace.real.json) with
 *                   synthetic analysis that keeps draining while you watch
 *   newplayer       just added: syncing, no games yet
 *   nosuchplayer    sync failed: Chess.com has no such account
 *   servererror     every request fails with a 500
 * Any other name is a 404 until it is added through the landing page, after
 * which it behaves like `newplayer`.
 */
import { ApiError } from "../api-errors";
import type {
  OpeningResult,
  PlayerProgress,
  RatingPoint,
  Report,
  TimeClass,
  TimeClassProgress,
} from "../api-types";
import { TIME_CLASSES } from "../api-types";
import real from "./deitythemenace.real.json";
import {
  SYNTHETIC_ANALYZED_LAST_HOUR,
  SYNTHETIC_PROFILES,
  syntheticPhases,
  syntheticTimePressure,
} from "./synthetic";

interface RealClass {
  ingested: number;
  skipped: number;
  /** All standard games, rated and unrated. */
  games: number;
  /** The record below is rated games only, as on Chess.com. */
  rated_games: number;
  wins: number;
  draws: number;
  losses: number;
  win_rate_pct: number | null;
  current_rating: number | null;
  rating_trend: RatingPoint[];
  openings: OpeningResult[];
}

const REAL = real as unknown as { generated_at: string; time_classes: Record<TimeClass, RealClass> };
const LATENCY_MS = 450;
/** Simulated drain of the bullet backlog, games per second, so progress visibly moves. */
const DRAIN_PER_SECOND = 1.5;
const STARTED_AT = Date.now();
const ADDED_KEY = "chess-coach:mock-added";

function wait(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(resolve, LATENCY_MS);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("Aborted", "AbortError"));
    });
  });
}

function addedPlayers(): Set<string> {
  try {
    return new Set(JSON.parse(sessionStorage.getItem(ADDED_KEY) ?? "[]") as string[]);
  } catch {
    return new Set();
  }
}

function rememberAdded(username: string): void {
  try {
    const all = addedPlayers();
    all.add(username);
    sessionStorage.setItem(ADDED_KEY, JSON.stringify([...all]));
  } catch {
    // Private mode or storage disabled: the player is simply forgotten on reload.
  }
}

/** Analyzed games right now, including the simulated drain. */
function analyzedNow(tc: TimeClass): number {
  const p = SYNTHETIC_PROFILES[tc];
  const r = REAL.time_classes[tc];
  const ceiling = r.ingested - r.skipped - p.failed;
  const drained = tc === "bullet" ? Math.floor(((Date.now() - STARTED_AT) / 1000) * DRAIN_PER_SECOND) : 0;
  return Math.min(ceiling, p.analyzed + drained);
}

function emptyClass(time_class: TimeClass): TimeClassProgress {
  return { time_class, games: 0, analyzed: 0, pending: 0, failed: 0, skipped: 0, current_rating: null };
}

function deityProgress(): PlayerProgress {
  return {
    username: "deitythemenace",
    sync_status: "complete",
    sync_error: null,
    last_synced_at: REAL.generated_at,
    analyzed_last_hour: SYNTHETIC_ANALYZED_LAST_HOUR,
    time_classes: TIME_CLASSES.map((tc) => {
      const r = REAL.time_classes[tc];
      const analyzed = analyzedNow(tc);
      const failed = SYNTHETIC_PROFILES[tc].failed;
      return {
        time_class: tc,
        games: r.ingested,
        analyzed,
        pending: r.ingested - analyzed - failed - r.skipped,
        failed,
        skipped: r.skipped,
        current_rating: r.current_rating,
      };
    }),
  };
}

function progressFor(username: string): PlayerProgress {
  if (username === "servererror") throw new ApiError(500, "Internal Server Error");
  if (username === "deitythemenace") return deityProgress();
  const base = {
    username,
    last_synced_at: null,
    analyzed_last_hour: 0,
    time_classes: TIME_CLASSES.map(emptyClass),
  };
  if (username === "nosuchplayer") {
    return { ...base, sync_status: "error", sync_error: `Chess.com has no player named "${username}".` };
  }
  if (username === "newplayer" || addedPlayers().has(username)) {
    return { ...base, sync_status: "syncing", sync_error: null };
  }
  throw new ApiError(404, "Player not found");
}

export async function getPlayer(username: string, signal?: AbortSignal): Promise<PlayerProgress> {
  await wait(signal);
  return progressFor(username);
}

export async function addPlayer(username: string, signal?: AbortSignal): Promise<PlayerProgress> {
  await wait(signal);
  if (username === "servererror") throw new ApiError(500, "Internal Server Error");
  rememberAdded(username);
  return progressFor(username);
}

export async function getReport(username: string, tc: TimeClass, signal?: AbortSignal): Promise<Report> {
  await wait(signal);
  progressFor(username); // 404s and 500s exactly as the progress endpoint does
  const generated_at = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
  const r = username === "deitythemenace" ? REAL.time_classes[tc] : null;
  if (!r) {
    return {
      username, time_class: tc, generated_at, games: 0, analyzed_games: 0,
      summary: { games: 0, wins: 0, draws: 0, losses: 0, win_rate_pct: null, current_rating: null, blunders_per_game: null },
      rating_trend: [], openings: [], blunders_by_phase: null, time_pressure: null,
    };
  }
  const analyzed = analyzedNow(tc);
  const phases = syntheticPhases(tc, analyzed);
  const blunders = phases?.reduce((n, p) => n + p.blunders, 0) ?? 0;
  return {
    username,
    time_class: tc,
    generated_at,
    games: r.games,
    analyzed_games: analyzed,
    summary: {
      games: r.rated_games,
      wins: r.wins,
      draws: r.draws,
      losses: r.losses,
      win_rate_pct: r.win_rate_pct,
      current_rating: r.current_rating,
      blunders_per_game: analyzed === 0 ? null : blunders / analyzed,
    },
    rating_trend: r.rating_trend,
    openings: r.openings,
    blunders_by_phase: phases,
    time_pressure: syntheticTimePressure(tc, analyzed),
  };
}
