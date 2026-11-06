/**
 * Response shapes of the report API. A mirror of docs/api-contract.md, which is
 * the source of truth: if the two disagree, the document wins.
 *
 * Units, once, so no field needs a comment repeating them:
 *   - timestamps: UTC ISO 8601 with "Z"      (Timestamp)
 *   - dates: the UTC day, "YYYY-MM-DD"       (IsoDate)
 *   - percentages: 0-100 floats, unrounded   (Percent)
 *   - ratings and rating changes: integer rating points
 *   - nothing in this API is in centipawns
 * Missing values are null; keys are never omitted.
 */

export const TIME_CLASSES = ["bullet", "blitz", "rapid", "daily"] as const;
export type TimeClass = (typeof TIME_CLASSES)[number];

export const PHASES = ["opening", "middlegame", "endgame"] as const;
export type Phase = (typeof PHASES)[number];

export type Color = "white" | "black";
export type SyncStatus = "pending" | "syncing" | "complete" | "error";

/** UTC ISO 8601, e.g. "2026-11-06T02:14:05Z". */
export type Timestamp = string;
/** UTC calendar day, "YYYY-MM-DD". */
export type IsoDate = string;
/** 0-100, not rounded. */
export type Percent = number;

// --- GET /players/{username}  and  POST /players ----------------------------

export interface TimeClassProgress {
  time_class: TimeClass;
  /** Ingested games, variants included. analyzed + pending + failed + skipped = games. */
  games: number;
  analyzed: number;
  /** Queued or running. */
  pending: number;
  /** Dead-lettered. */
  failed: number;
  /** Variants Stockfish cannot grade. */
  skipped: number;
  /** Rating after the most recent rated standard game in this class. */
  current_rating: number | null;
}

export interface PlayerProgress {
  username: string;
  sync_status: SyncStatus;
  sync_error: string | null;
  last_synced_at: Timestamp | null;
  /** Games analyzed in the last 60 minutes, all time classes. */
  analyzed_last_hour: number;
  /** Always four entries, in TIME_CLASSES order. */
  time_classes: TimeClassProgress[];
}

export interface AddPlayerRequest {
  username: string;
}

// --- GET /players/{username}/report?time_class= -----------------------------

export interface Summary {
  wins: number;
  draws: number;
  losses: number;
  /** wins / games * 100; null when games = 0. */
  win_rate_pct: Percent | null;
  current_rating: number | null;
  /** Player blunders / analyzed_games; null when analyzed_games = 0. */
  blunders_per_game: number | null;
}

export interface RatingPoint {
  date: IsoDate;
  /** Rating after the last rated game that UTC day. */
  rating: number;
  /** Rated games that day. */
  games: number;
}

export interface OpeningResult {
  /** Opening family, e.g. "Queens Pawn Opening". */
  opening: string;
  /** The player's colour. */
  color: Color;
  games: number;
  wins: number;
  draws: number;
  losses: number;
  win_rate_pct: Percent;
  /** Sum of per-game rating change over the rated games; negative = lost. */
  rating_change: number;
}

export interface PhaseBlunders {
  phase: Phase;
  /** The player's classified moves in this phase, across analyzed games. */
  player_moves: number;
  blunders: number;
}

export interface ClockBucket {
  moves: number;
  /** (best + good) / moves * 100; null when moves = 0. */
  accuracy_pct: Percent | null;
  blunders: number;
}

export interface TimePressure {
  /** Always 30. */
  threshold_seconds: number;
  /** Analyzed games where the player started at least one move under the threshold. */
  low_clock_games: number;
  /** Moves started with less than threshold_seconds on the clock. */
  low_clock: ClockBucket;
  /** Moves started with threshold_seconds or more. */
  normal_clock: ClockBucket;
}

export interface Report {
  username: string;
  time_class: TimeClass;
  generated_at: Timestamp;
  /** Standard-chess games in this class, rated and unrated. */
  games: number;
  /** Sample size of every move-level field. */
  analyzed_games: number;
  summary: Summary;
  /** Oldest first. */
  rating_trend: RatingPoint[];
  /** Groups with >= 5 games, most rating lost first. */
  openings: OpeningResult[];
  /** Null when analyzed_games = 0; otherwise opening, middlegame, endgame. */
  blunders_by_phase: PhaseBlunders[] | null;
  /** Null when analyzed_games = 0, and always for daily. */
  time_pressure: TimePressure | null;
}

/** FastAPI's error body: a string for 404 and 501, a validation array for 422. */
export interface ErrorBody {
  detail: string | unknown[];
}
