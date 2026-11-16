/**
 * ============================  SYNTHETIC DATA  ============================
 *
 * Nothing in this file was measured. The dev database has ingested games but
 * no engine analysis yet (M2 has not run), so every move-level number below -
 * how many games are analyzed, blunders by phase, accuracy under 30 seconds -
 * is invented to look plausible for a ~400-1000 rated player, so the dashboard
 * can be built and reviewed before the real numbers exist.
 *
 * The shape of the invention, so it can be judged:
 *   - Analysis follows DESIGN.md's queue priority: rapid and blitz are fully
 *     analyzed, bullet is a third of the way through its backlog.
 *   - Blunder rates rise from opening to endgame and from rapid to bullet.
 *   - Accuracy drops sharply under 30 seconds, most of all in rapid, where
 *     being that low on time is rare and panicked.
 *
 * Replace with real numbers by pointing NEXT_PUBLIC_API_URL at the M4 API.
 * The ingested-game numbers (results, ratings, openings) are REAL and live in
 * deitythemenace.real.json.
 * ===========================================================================
 */
import type { PhaseBlunders, TimeClass, TimePressure } from "../api-types";

interface SyntheticProfile {
  /** Games analyzed when the page first loads. */
  analyzed: number;
  /** Dead-lettered analysis jobs. */
  failed: number;
  /** Player moves per game in opening, middlegame, endgame. */
  movesPerGame: [number, number, number];
  /** Blunders per 100 player moves in each phase. */
  blundersPer100: [number, number, number];
  /** Share of analyzed games where the clock ever dropped under 30 s. */
  lowClockGameShare: number;
  /** Share of all player moves started with under 30 s. */
  lowClockMoveShare: number;
  /** Accuracy (best + good moves, %) with and without time pressure. */
  accuracyLowClock: number;
  accuracyNormalClock: number;
  /** Blunders are this many times likelier per move under 30 s. */
  lowClockBlunderMultiplier: number;
}

export const SYNTHETIC_PROFILES: Record<TimeClass, SyntheticProfile> = {
  bullet: {
    analyzed: 1210,
    failed: 3,
    movesPerGame: [9, 14, 9],
    blundersPer100: [1.4, 6.8, 9.5],
    lowClockGameShare: 0.85,
    lowClockMoveShare: 0.38,
    accuracyLowClock: 63.4,
    accuracyNormalClock: 79.1,
    lowClockBlunderMultiplier: 2.6,
  },
  blitz: {
    analyzed: 433,
    failed: 0,
    movesPerGame: [10, 17, 12],
    blundersPer100: [1.0, 3.9, 4.6],
    lowClockGameShare: 0.41,
    lowClockMoveShare: 0.09,
    accuracyLowClock: 57.2,
    accuracyNormalClock: 82.6,
    lowClockBlunderMultiplier: 3.4,
  },
  rapid: {
    analyzed: 424,
    failed: 0,
    movesPerGame: [10, 19, 13],
    blundersPer100: [0.8, 2.6, 3.1],
    lowClockGameShare: 0.15,
    lowClockMoveShare: 0.03,
    accuracyLowClock: 55.8,
    accuracyNormalClock: 85.9,
    lowClockBlunderMultiplier: 4.1,
  },
  daily: {
    analyzed: 2,
    failed: 0,
    movesPerGame: [11, 16, 9],
    blundersPer100: [0, 3, 5.5],
    lowClockGameShare: 0,
    lowClockMoveShare: 0,
    accuracyLowClock: 0,
    accuracyNormalClock: 88,
    lowClockBlunderMultiplier: 1,
  },
};

/** Synthetic analysis throughput for the progress page, games per hour. */
export const SYNTHETIC_ANALYZED_LAST_HOUR = 412;

const PHASE_ORDER = ["opening", "middlegame", "endgame"] as const;

/** Blunders by phase for `analyzed` games. Deterministic: same input, same output. */
export function syntheticPhases(tc: TimeClass, analyzed: number): PhaseBlunders[] | null {
  if (analyzed === 0) return null;
  const p = SYNTHETIC_PROFILES[tc];
  return PHASE_ORDER.map((phase, i) => {
    const player_moves = Math.round(p.movesPerGame[i]! * analyzed);
    return { phase, player_moves, blunders: Math.round((player_moves * p.blundersPer100[i]!) / 100) };
  });
}

/**
 * Time-pressure buckets, split so their blunders add up to the phase total:
 * the same moves, counted two ways.
 */
export function syntheticTimePressure(tc: TimeClass, analyzed: number): TimePressure | null {
  if (analyzed === 0 || tc === "daily") return null;
  const p = SYNTHETIC_PROFILES[tc];
  const phases = syntheticPhases(tc, analyzed) ?? [];
  const moves = phases.reduce((n, x) => n + x.player_moves, 0);
  const blunders = phases.reduce((n, x) => n + x.blunders, 0);

  const lowMoves = Math.round(moves * p.lowClockMoveShare);
  const normalMoves = moves - lowMoves;
  // Solve lowRate * lowMoves + normalRate * normalMoves = blunders, lowRate = k * normalRate.
  const normalRate = blunders / (p.lowClockBlunderMultiplier * lowMoves + normalMoves);
  const lowBlunders = Math.round(normalRate * p.lowClockBlunderMultiplier * lowMoves);

  return {
    threshold_seconds: 30,
    low_clock_games: Math.round(analyzed * p.lowClockGameShare),
    low_clock: {
      moves: lowMoves,
      accuracy_pct: lowMoves === 0 ? null : p.accuracyLowClock,
      blunders: lowBlunders,
    },
    normal_clock: {
      moves: normalMoves,
      accuracy_pct: normalMoves === 0 ? null : p.accuracyNormalClock,
      blunders: blunders - lowBlunders,
    },
  };
}
