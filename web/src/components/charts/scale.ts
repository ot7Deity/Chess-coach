/** Small, dependency-free scale helpers for the hand-rolled SVG charts. */

export function linear(d0: number, d1: number, r0: number, r1: number) {
  const span = d1 - d0 || 1;
  return (v: number) => r0 + ((v - d0) / span) * (r1 - r0);
}

/** About `count` round tick values covering [min, max]: steps of 1, 2, 2.5 or 5 x 10^k. */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (min === max) {
    min -= 50;
    max += 50;
  }
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = ([1, 2, 2.5, 5, 10].find((m) => m * mag >= raw) ?? 10) * mag;
  const ticks: number[] = [];
  for (let v = Math.floor(min / step) * step; v <= Math.ceil(max / step) * step + 1e-9; v += step) {
    ticks.push(Math.round(v * 1e6) / 1e6);
  }
  return ticks;
}

/** UTC timestamps for x-axis ticks: Jan 1 of each year for long spans, else every 1-3 months. */
export function timeTicks(t0: number, t1: number): { at: number; kind: "year" | "month" }[] {
  const days = (t1 - t0) / 86_400_000;
  const start = new Date(t0);
  const out: { at: number; kind: "year" | "month" }[] = [];
  if (days > 540) {
    for (let y = start.getUTCFullYear() + 1; Date.UTC(y, 0, 1) <= t1; y++) {
      out.push({ at: Date.UTC(y, 0, 1), kind: "year" });
    }
    return out;
  }
  const every = days > 200 ? 3 : days > 75 ? 1 : 0;
  if (every === 0) return out;
  for (let m = start.getUTCMonth() + 1, y = start.getUTCFullYear(); ; m += every) {
    const at = Date.UTC(y, m, 1);
    if (at > t1) break;
    out.push({ at, kind: "month" });
  }
  return out;
}

/** Index of the point whose x is nearest to `x`, by binary search over sorted xs. */
export function nearestIndex(xs: number[], x: number): number {
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid]! < x) lo = mid;
    else hi = mid;
  }
  return Math.abs(xs[lo]! - x) <= Math.abs(xs[hi]! - x) ? lo : hi;
}
