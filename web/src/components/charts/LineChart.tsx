"use client";

import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { linear, nearestIndex, niceTicks, timeTicks } from "./scale";
import styles from "./charts.module.css";

export interface LinePoint {
  /** Epoch ms. */
  t: number;
  v: number;
}

interface Props {
  points: LinePoint[];
  /** Accessible summary of the whole chart, e.g. "Rating from 354 to 946". */
  label: string;
  /** Tooltip content for point i: the value leads, context follows. */
  describe: (i: number) => { value: string; context: string };
  formatTick: (v: number) => string;
  height?: number;
}

const M = { top: 12, right: 44, bottom: 28, left: 40 };

function useWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.floor(entry!.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

/**
 * A single-series line over time: 2px line, a 10% area wash, hairline grid, the
 * latest value labelled at the end. Hover or arrow keys move a crosshair that
 * snaps to the nearest point.
 */
export function LineChart({ points, label, describe, formatTick, height = 240 }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const width = useWidth(box);
  const [active, setActive] = useState<number | null>(null);

  const ready = width > 0 && points.length > 1;
  const first = points[0];
  const last = points[points.length - 1];
  const values = points.map((p) => p.v);
  const ticks = niceTicks(Math.min(...values), Math.max(...values), 4);
  const x = linear(first?.t ?? 0, last?.t ?? 1, M.left, width - M.right);
  const y = linear(ticks[0]!, ticks[ticks.length - 1]!, height - M.bottom, M.top);
  const xs = points.map((p) => x(p.t));

  const line = points.map((p, i) => `${i ? "L" : "M"}${xs[i]!.toFixed(1)},${y(p.v).toFixed(1)}`).join("");
  const area = `${line}L${xs[xs.length - 1]?.toFixed(1)},${height - M.bottom}L${xs[0]?.toFixed(1)},${height - M.bottom}Z`;

  function onPointer(e: PointerEvent<SVGSVGElement>) {
    const rect = e.currentTarget.getBoundingClientRect();
    setActive(nearestIndex(xs, e.clientX - rect.left));
  }

  function onKey(e: KeyboardEvent<SVGSVGElement>) {
    const n = points.length;
    const at = active ?? n - 1;
    const next =
      e.key === "ArrowLeft" ? at - 1 : e.key === "ArrowRight" ? at + 1 : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : null;
    if (next === null) return;
    e.preventDefault();
    setActive(Math.min(n - 1, Math.max(0, next)));
  }

  const tip = active !== null ? describe(active) : null;
  const tipX = active !== null ? xs[active]! : 0;

  return (
    <div ref={box} className={styles.lineBox} style={{ height }}>
      {ready && first && last && (
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={label}
          tabIndex={0}
          className={styles.svg}
          onPointerMove={onPointer}
          onPointerLeave={() => setActive(null)}
          onFocus={() => setActive(points.length - 1)}
          onBlur={() => setActive(null)}
          onKeyDown={onKey}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line x1={M.left} x2={width - M.right} y1={y(t)} y2={y(t)} className={styles.grid} />
              <text x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className={styles.tick}>
                {formatTick(t)}
              </text>
            </g>
          ))}
          {timeTicks(first.t, last.t).map(({ at, kind }) => (
            <text key={at} x={x(at)} y={height - 8} textAnchor="middle" className={styles.tick}>
              {kind === "year"
                ? new Date(at).getUTCFullYear()
                : new Date(at).toLocaleString("en-US", { month: "short", timeZone: "UTC" })}
            </text>
          ))}
          <path d={area} className={styles.area} />
          <path d={line} className={styles.line} />
          {active !== null && (
            <line x1={tipX} x2={tipX} y1={M.top} y2={height - M.bottom} className={styles.crosshair} />
          )}
          <circle
            cx={active !== null ? tipX : xs[xs.length - 1]}
            cy={y(active !== null ? points[active]!.v : last.v)}
            r={4.5}
            className={styles.dot}
          />
          <text x={xs[xs.length - 1]! + 10} y={y(last.v)} dy="0.32em" className={styles.endLabel}>
            {formatTick(last.v)}
          </text>
        </svg>
      )}
      {tip && (
        <div
          className={styles.tooltip}
          style={{ left: Math.min(Math.max(tipX, 70), width - 70) }}
          aria-live="polite"
        >
          <strong>{tip.value}</strong>
          <span>{tip.context}</span>
        </div>
      )}
    </div>
  );
}
