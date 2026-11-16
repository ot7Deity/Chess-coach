import styles from "./charts.module.css";

export interface BarRow {
  key: string;
  label: React.ReactNode;
  /** Secondary text beside the label, e.g. "42 games". */
  meta?: string;
  value: number;
  /** The value as printed at the bar's tip. */
  display: string;
  /** Accent for the bar this chart is about; muted for context. */
  tone?: "accent" | "muted";
}

/**
 * Horizontal bars on one baseline, one hue, the value printed at each tip.
 * Every value is printed, so nothing depends on hovering.
 */
export function BarList({ rows, max, label }: { rows: BarRow[]; max?: number; label: string }) {
  const top = max ?? Math.max(...rows.map((r) => r.value), 0);
  return (
    <ul className={styles.bars} aria-label={label}>
      {rows.map((r) => (
        <li key={r.key} className={styles.barRow}>
          <div className={styles.barHead}>
            <span className={styles.barLabel}>{r.label}</span>
            {r.meta && <span className={styles.barMeta}>{r.meta}</span>}
          </div>
          <div className={styles.barTrack}>
            <span
              className={styles.bar}
              data-tone={r.tone ?? "accent"}
              style={{ width: `${top > 0 ? (r.value / top) * 70 : 0}%` }}
            />
            <span className={styles.barValue}>{r.display}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

/**
 * Bars that grow left (red, negative) or right (blue, positive) from a centred
 * zero, scaled to the largest magnitude so both sides share one axis.
 */
export function DivergingBars({ rows, label }: { rows: BarRow[]; label: string }) {
  const top = Math.max(...rows.map((r) => Math.abs(r.value)), 1);
  return (
    <ul className={styles.bars} aria-label={label}>
      {rows.map((r) => {
        // 40% of the track at most, leaving room for the value beyond the tip.
        const pct = (Math.abs(r.value) / top) * 40;
        const negative = r.value < 0;
        return (
          <li key={r.key} className={styles.barRow}>
            <div className={styles.barHead}>
              <span className={styles.barLabel}>{r.label}</span>
              {r.meta && <span className={styles.barMeta}>{r.meta}</span>}
            </div>
            <div className={styles.divTrack}>
              <span className={styles.zero} aria-hidden="true" />
              <span
                className={styles.divBar}
                data-sign={negative ? "neg" : "pos"}
                style={negative ? { right: "50%", width: `${pct}%` } : { left: "50%", width: `${pct}%` }}
              />
              <span
                className={styles.divValue}
                style={negative ? { right: `calc(50% + ${pct}% + 6px)` } : { left: `calc(50% + ${pct}% + 6px)` }}
              >
                {r.display}
              </span>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
