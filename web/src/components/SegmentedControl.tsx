"use client";

import { useRef, type KeyboardEvent } from "react";
import styles from "./SegmentedControl.module.css";

export interface Segment<T extends string> {
  value: T;
  label: string;
  /** Small secondary text under the label, e.g. a game count. */
  hint?: string;
  disabled?: boolean;
}

interface Props<T extends string> {
  label: string;
  segments: Segment<T>[];
  value: T;
  onChange: (value: T) => void;
}

/**
 * A radio group drawn as a segmented control. Arrow keys move between enabled
 * segments (the WAI-ARIA radio pattern), so only the selected one is a tab stop.
 */
export function SegmentedControl<T extends string>({ label, segments, value, onChange }: Props<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const enabled = segments.filter((s) => !s.disabled);
  const index = segments.findIndex((s) => s.value === value);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (step === 0 || enabled.length === 0) return;
    e.preventDefault();
    const at = enabled.findIndex((s) => s.value === value);
    const next = enabled[(at + step + enabled.length) % enabled.length]!;
    onChange(next.value);
    refs.current[segments.indexOf(next)]?.focus();
  }

  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={styles.root}
      style={{ "--count": segments.length, "--index": Math.max(0, index) } as React.CSSProperties}
      onKeyDown={onKeyDown}
    >
      <span className={styles.thumb} aria-hidden="true" data-hidden={index < 0} />
      {segments.map((s, i) => {
        const selected = s.value === value;
        return (
          <button
            key={s.value}
            ref={(el) => {
              refs.current[i] = el;
            }}
            type="button"
            role="radio"
            aria-checked={selected}
            disabled={s.disabled}
            tabIndex={selected ? 0 : -1}
            className={styles.segment}
            onClick={() => onChange(s.value)}
          >
            <span className={styles.label}>{s.label}</span>
            {s.hint && <span className={styles.hint}>{s.hint}</span>}
          </button>
        );
      })}
    </div>
  );
}
