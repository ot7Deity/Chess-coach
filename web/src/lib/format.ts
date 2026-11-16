/**
 * Display formatting. The API sends raw numbers (percentages 0-100 unrounded,
 * UTC timestamps); rounding and wording happen here and nowhere else.
 */

const int = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const one = new Intl.NumberFormat("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const dateFmt = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });
const monthFmt = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" });

/** A true minus sign, which lines up with "+" and reads better than a hyphen. */
const MINUS = "−";

export const formatInt = (n: number): string => int.format(n).replace("-", MINUS);

export const formatOne = (n: number): string => one.format(n).replace("-", MINUS);

export const formatPct = (pct: number): string => `${int.format(pct)}%`;

export function formatSigned(n: number): string {
  if (n === 0) return "0";
  return n > 0 ? `+${int.format(n)}` : `${MINUS}${int.format(-n)}`;
}

/** "2026-11-06" (a UTC day) -> epoch ms at UTC midnight. */
export const dayToMs = (day: string): number => Date.parse(`${day}T00:00:00Z`);

export const formatDay = (ms: number): string => dateFmt.format(ms);
export const formatMonth = (ms: number): string => monthFmt.format(ms);

export function formatRelative(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** "about 6 hours", "about 40 minutes", "under a minute": deliberately vague. */
export function formatDuration(minutes: number): string {
  if (minutes < 1) return "under a minute";
  if (minutes < 55) return `about ${Math.max(5, Math.round(minutes / 5) * 5)} minutes`;
  const hours = Math.round(minutes / 60);
  if (hours < 36) return `about ${hours} hour${hours === 1 ? "" : "s"}`;
  return `about ${Math.round(hours / 24)} days`;
}

export const TIME_CLASS_LABEL = {
  bullet: "Bullet",
  blitz: "Blitz",
  rapid: "Rapid",
  daily: "Daily",
} as const;
