"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { ApiError, describeError, getPlayer, getReport, normalizeUsername } from "@/lib/api";
import { TIME_CLASSES, type PlayerProgress, type TimeClass } from "@/lib/api-types";
import { formatInt, formatRelative, TIME_CLASS_LABEL } from "@/lib/format";
import { useAsync } from "@/lib/use-async";
import { ButtonLink, Button } from "@/components/Button";
import { SegmentedControl } from "@/components/SegmentedControl";
import { EmptyState, ErrorState, Skeleton } from "@/components/States";
import { ReportSkeleton, ReportView } from "./ReportView";
import styles from "./overview.module.css";

/** The requested class if it has games, else the class with the most games. */
function pickTimeClass(p: PlayerProgress | undefined, requested: string | null): TimeClass | null {
  if (!p) return null;
  const withGames = p.time_classes.filter((c) => c.games > 0);
  const asked = withGames.find((c) => c.time_class === requested);
  if (asked) return asked.time_class;
  return withGames.reduce<PlayerProgress["time_classes"][number] | null>(
    (best, c) => (best === null || c.games > best.games ? c : best),
    null,
  )?.time_class ?? null;
}

export function Overview({ username: raw }: { username: string }) {
  const username = normalizeUsername(raw);
  const router = useRouter();
  const search = useSearchParams();

  const progress = useAsync(username, (signal) => getPlayer(username!, signal));
  const p = progress.data;
  const tc = pickTimeClass(p, search.get("tc"));
  const report = useAsync(username && tc ? `${username}/${tc}` : null, (signal) =>
    getReport(username!, tc!, signal),
  );

  if (!username) {
    return (
      <EmptyState title="That isn't a Chess.com username" action={<ButtonLink href="/">Try another name</ButtonLink>}>
        Chess.com usernames are letters, numbers, dashes and underscores, up to 50 characters.
      </EmptyState>
    );
  }

  if (progress.error && !p) {
    const err = progress.error;
    if (err instanceof ApiError && err.isNotFound) {
      return (
        <EmptyState
          title={`We aren't following ${username} yet`}
          action={<ButtonLink href={`/?u=${encodeURIComponent(username)}`} variant="primary">Analyze their games</ButtonLink>}
        >
          Add them and their Chess.com history starts downloading straight away.
        </EmptyState>
      );
    }
    return (
      <ErrorState
        title="We couldn't load this player"
        action={<Button variant="secondary" onClick={progress.reload}>Try again</Button>}
      >
        {describeError(err)}
      </ErrorState>
    );
  }

  const total = p?.time_classes.reduce((n, c) => n + c.games, 0) ?? 0;
  const analyzed = p?.time_classes.reduce((n, c) => n + c.analyzed, 0) ?? 0;
  const tcProgress = p?.time_classes.find((c) => c.time_class === tc);
  // Progress and the report are read at different moments while the backlog
  // drains, so they can disagree by a game or two. When the report for this
  // class is loaded, the banner quotes its count, matching the tiles below it.
  const tcAnalyzed =
    report.data?.time_class === tc && !report.loading ? report.data.analyzed_games : tcProgress?.analyzed ?? 0;

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <h1 className={styles.name}>{username}</h1>
        {p ? (
          <p className={styles.meta}>
            {p.last_synced_at ? `Synced ${formatRelative(p.last_synced_at)}` : "Not synced yet"}
            {total > 0 && ` · ${formatInt(analyzed)} of ${formatInt(total)} games analyzed`}
            {" · "}
            <Link href={`/players/${username}/progress`}>Progress</Link>
          </p>
        ) : (
          <Skeleton width={280} height={18} />
        )}
      </header>

      {p && total === 0 ? null : p && tc ? (
        <SegmentedControl
          label="Time control"
          value={tc}
          onChange={(v) => router.replace(`?tc=${v}`, { scroll: false })}
          segments={TIME_CLASSES.map((t) => {
            const c = p.time_classes.find((x) => x.time_class === t);
            const games = c?.games ?? 0;
            return { value: t, label: TIME_CLASS_LABEL[t], hint: `${formatInt(games)} games`, disabled: games === 0 };
          })}
        />
      ) : (
        <Skeleton height={50} width="min(520px, 100%)" />
      )}

      {p && tc === null && <NoGames progress={p} />}

      {p && tc !== null && tcProgress && tcProgress.pending > 0 && (
        <p className={styles.partial} role="status">
          The engine has analyzed {formatInt(tcAnalyzed)} of {formatInt(tcProgress.games)}{" "}
          {TIME_CLASS_LABEL[tc].toLowerCase()} games so far. Results, rating and openings are complete;
          blunder and clock numbers fill in as the rest finish.{" "}
          <Link href={`/players/${username}/progress`}>See progress</Link>
        </p>
      )}

      {tc !== null &&
        (report.error && !report.loading ? (
          <ErrorState
            title="We couldn't load this report"
            action={<Button variant="secondary" onClick={report.reload}>Try again</Button>}
          >
            {describeError(report.error)}
          </ErrorState>
        ) : report.data ? (
          <ReportView report={report.data} stale={report.loading || report.data.time_class !== tc} />
        ) : (
          <ReportSkeleton />
        ))}
      {!p && <ReportSkeleton />}
    </div>
  );
}

function NoGames({ progress: p }: { progress: PlayerProgress }) {
  if (p.sync_status === "error") {
    return (
      <ErrorState title="The download from Chess.com failed">
        {p.sync_error ?? "No reason was given."} Check the spelling of the username, or try adding it again.
      </ErrorState>
    );
  }
  if (p.sync_status === "pending" || p.sync_status === "syncing") {
    return (
      <EmptyState
        title="Downloading games from Chess.com"
        action={<ButtonLink href={`/players/${p.username}/progress`}>Watch progress</ButtonLink>}
      >
        A full history usually takes about a minute. Your report appears here as soon as the first games
        arrive.
      </EmptyState>
    );
  }
  return (
    <EmptyState title="No games yet">
      This account has no finished games on Chess.com. Play a few and they will show up after the next sync.
    </EmptyState>
  );
}
