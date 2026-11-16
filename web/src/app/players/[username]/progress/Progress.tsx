"use client";

import { useEffect } from "react";
import { ApiError, describeError, getPlayer, normalizeUsername } from "@/lib/api";
import type { PlayerProgress, TimeClassProgress } from "@/lib/api-types";
import { formatDuration, formatInt, formatRelative, TIME_CLASS_LABEL } from "@/lib/format";
import { useAsync } from "@/lib/use-async";
import { Button, ButtonLink } from "@/components/Button";
import { EmptyState, ErrorState, Skeleton } from "@/components/States";
import styles from "./progress.module.css";

const POLL_MS = 5000;

const sum = (p: PlayerProgress, f: (c: TimeClassProgress) => number) => p.time_classes.reduce((n, c) => n + f(c), 0);

const capitalize = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

function workRemains(p: PlayerProgress): boolean {
  return p.sync_status === "pending" || p.sync_status === "syncing" || sum(p, (c) => c.pending) > 0;
}

/**
 * Polls progress every 5 s while there is work left, and not at all while the
 * tab is hidden: nobody is watching, and every poll is a query on the database
 * the workers are writing to.
 */
function usePolledProgress(username: string | null) {
  const state = useAsync(username, (signal) => getPlayer(username!, signal));
  const { data, loading, error, reload } = state;

  useEffect(() => {
    // A failed poll keeps polling (with the last good data still on screen):
    // a blip in the API should not freeze the page until a manual reload.
    if (loading || !data || (!error && !workRemains(data))) return;
    const tick = () => {
      if (document.visibilityState === "visible") reload();
      else document.addEventListener("visibilitychange", reload, { once: true });
    };
    const t = setTimeout(tick, POLL_MS);
    return () => {
      clearTimeout(t);
      document.removeEventListener("visibilitychange", reload);
    };
  }, [data, loading, error, reload]);

  return state;
}

export function Progress({ username: raw }: { username: string }) {
  const username = normalizeUsername(raw);
  const { data: p, error, reload, settledAt } = usePolledProgress(username);

  if (!username) {
    return <EmptyState title="That isn't a Chess.com username" action={<ButtonLink href="/">Try another name</ButtonLink>} />;
  }
  if (error && !p) {
    if (error instanceof ApiError && error.isNotFound) {
      return (
        <EmptyState
          title={`We aren't following ${username} yet`}
          action={<ButtonLink href={`/?u=${encodeURIComponent(username)}`} variant="primary">Analyze their games</ButtonLink>}
        />
      );
    }
    return (
      <ErrorState title="We couldn't load progress" action={<Button variant="secondary" onClick={reload}>Try again</Button>}>
        {describeError(error)}
      </ErrorState>
    );
  }
  if (!p) return <ProgressSkeleton />;

  const games = sum(p, (c) => c.games);
  const analyzed = sum(p, (c) => c.analyzed);
  const pending = sum(p, (c) => c.pending);
  const failed = sum(p, (c) => c.failed);
  const skipped = sum(p, (c) => c.skipped);
  const gradable = games - skipped;
  const downloading = p.sync_status === "pending" || p.sync_status === "syncing";

  const headline =
    p.sync_status === "error"
      ? "The download from Chess.com failed"
      : downloading && games === 0
        ? "Downloading your games"
        : pending > 0
          ? "Analyzing your games"
          : "All caught up";

  const pace =
    pending === 0
      ? null
      : p.analyzed_last_hour > 0
        ? `${capitalize(formatDuration((pending / p.analyzed_last_hour) * 60))} left at the current pace`
        : "Waiting for an analysis worker to pick these up";

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <p className={styles.eyebrow}>{username}</p>
        <h1 className={styles.title}>{headline}</h1>
        <div role="status" aria-live="polite" className={styles.lead}>
          {p.sync_status === "error" ? (
            <p>{p.sync_error ?? "No reason was given."}</p>
          ) : games === 0 ? (
            <p>Fetching your history from Chess.com, one month at a time. This usually takes about a minute.</p>
          ) : (
            <>
              <p className={styles.big}>
                {formatInt(analyzed)} <span>of {formatInt(gradable)} games analyzed</span>
              </p>
              {pace && <p>{pace}.</p>}
            </>
          )}
        </div>
        {games > 0 && <Meter value={analyzed} max={gradable} label="Games analyzed" large />}
      </header>

      {games > 0 && (
        <ButtonLink href={`/players/${username}`} variant="primary">
          {pending > 0 ? "See your report so far" : "See your report"}
        </ButtonLink>
      )}

      <section className={styles.section} aria-labelledby="steps">
        <h2 id="steps" className={styles.h2}>
          Download
        </h2>
        <p className={styles.row}>
          <span>
            {p.sync_status === "complete"
              ? `${formatInt(games)} games downloaded`
              : p.sync_status === "error"
                ? "Stopped"
                : `${formatInt(games)} games so far`}
          </span>
          <span className={styles.muted}>
            {p.last_synced_at ? `Last synced ${formatRelative(p.last_synced_at)}` : "First sync in progress"}
          </span>
        </p>
      </section>

      {games > 0 && (
        <section className={styles.section} aria-labelledby="analysis">
          <h2 id="analysis" className={styles.h2}>
            Engine analysis
          </h2>
          <p className={styles.note}>
            Rapid and blitz go first, then bullet: bullet histories are long, and your first report should not wait
            behind thousands of one-minute games.
          </p>
          <ul className={styles.classes}>
            {p.time_classes.map((c) => (
              <ClassRow key={c.time_class} c={c} />
            ))}
          </ul>
          {(failed > 0 || skipped > 0) && (
            <p className={styles.note}>
              {failed > 0 && `${formatInt(failed)} game${failed === 1 ? "" : "s"} could not be analyzed after five tries. `}
              {skipped > 0 &&
                `${formatInt(skipped)} variant game${skipped === 1 ? " was" : "s were"} skipped: the engine only grades standard chess.`}
            </p>
          )}
        </section>
      )}

      {error ? (
        <p className={styles.footer} role="status">
          Couldn&apos;t refresh just now. Showing the last update; trying again shortly.
        </p>
      ) : (
        workRemains(p) &&
        settledAt && (
          <p className={styles.footer}>
            Updated {new Date(settledAt).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" })}{" "}
            · refreshes every 5 seconds
          </p>
        )
      )}
    </div>
  );
}

function ClassRow({ c }: { c: TimeClassProgress }) {
  const gradable = c.games - c.skipped;
  const done = gradable > 0 && c.pending === 0;
  return (
    <li className={styles.classRow}>
      <div className={styles.row}>
        <span>{TIME_CLASS_LABEL[c.time_class]}</span>
        <span className={styles.muted}>
          {gradable === 0
            ? "No games"
            : done
              ? `✓ ${formatInt(c.analyzed)} analyzed`
              : `${formatInt(c.analyzed)} of ${formatInt(gradable)}`}
        </span>
      </div>
      {gradable > 0 && <Meter value={c.analyzed} max={gradable} label={`${TIME_CLASS_LABEL[c.time_class]} games analyzed`} />}
    </li>
  );
}

function Meter({ value, max, label, large }: { value: number; max: number; label: string; large?: boolean }) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={max}
      aria-valuenow={value}
      className={styles.meter}
      data-large={large || undefined}
    >
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

function ProgressSkeleton() {
  return (
    <div className={styles.page} aria-busy="true" aria-label="Loading progress">
      <Skeleton width={120} height={16} />
      <Skeleton width="70%" height={48} />
      <Skeleton height={10} />
      <Skeleton height={160} />
    </div>
  );
}
