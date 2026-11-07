"use client";

import type { Report } from "@/lib/api-types";
import {
  dayToMs,
  formatDay,
  formatInt,
  formatOne,
  formatPct,
  formatSigned,
  TIME_CLASS_LABEL,
} from "@/lib/format";
import { BarList, DivergingBars } from "@/components/charts/Bars";
import { LineChart } from "@/components/charts/LineChart";
import { DataTable, Section } from "@/components/Section";
import { StatRow, StatTile } from "@/components/StatTile";
import { EmptyState, Skeleton } from "@/components/States";
import styles from "./overview.module.css";

const DAY = 86_400_000;
const colorLabel = (c: string) => (c === "white" ? "White" : "Black");

/** Rating change over the 30 days before the report was generated, from the daily trend. */
function ratingChange30d(r: Report): number | null {
  const pts = r.rating_trend;
  const last = pts[pts.length - 1];
  const cutoff = Date.parse(r.generated_at) - 30 * DAY;
  if (!last || dayToMs(last.date) < cutoff) return null;
  const before = [...pts].reverse().find((p) => dayToMs(p.date) < cutoff) ?? pts[0]!;
  return last.rating - before.rating;
}

export function ReportView({ report: r, stale }: { report: Report; stale: boolean }) {
  const s = r.summary;
  const delta = ratingChange30d(r);
  const tcName = TIME_CLASS_LABEL[r.time_class].toLowerCase();
  const analyzedNote = `From ${formatInt(r.analyzed_games)} analyzed ${tcName} game${r.analyzed_games === 1 ? "" : "s"}.`;

  return (
    <div className={styles.report} data-stale={stale || undefined} aria-busy={stale || undefined}>
      <StatRow>
        <StatTile
          label="Games"
          value={formatInt(r.games)}
          detail={`${formatInt(s.wins)} won · ${formatInt(s.draws)} drawn · ${formatInt(s.losses)} lost`}
        />
        <StatTile label="Win rate" value={s.win_rate_pct === null ? null : formatPct(s.win_rate_pct)} missing="No games yet" />
        <StatTile
          label="Rating"
          value={s.current_rating === null ? null : formatInt(s.current_rating)}
          detail={delta === null ? "No rated games in 30 days" : `${formatSigned(delta)} in the last 30 days`}
          missing="No rated games"
        />
        <StatTile
          label="Blunders per game"
          value={s.blunders_per_game === null ? null : formatOne(s.blunders_per_game)}
          detail={`${formatInt(r.analyzed_games)} games analyzed`}
          missing="Waiting for the engine"
        />
      </StatRow>

      <RatingSection r={r} />
      <OpeningsSection r={r} />
      <PhaseSection r={r} note={analyzedNote} />
      <ClockSection r={r} note={analyzedNote} />
    </div>
  );
}

function RatingSection({ r }: { r: Report }) {
  const pts = r.rating_trend;
  const first = pts[0];
  const last = pts[pts.length - 1];
  return (
    <Section
      title="Rating"
      question="Am I getting better?"
      footnote="Your rating after the last rated game of each day you played (UTC)."
      table={
        pts.length > 0 && (
          <DataTable head={["Day", "Rating", "Rated games"]} rows={[...pts].reverse().map((p) => [p.date, p.rating, p.games])} />
        )
      }
    >
      {pts.length < 2 || !first || !last ? (
        <EmptyState title="Not enough rated games for a trend">
          A trend needs rated games on at least two different days.
        </EmptyState>
      ) : (
        <>
          <p className={styles.insight}>
            {formatInt(first.rating)} to {formatInt(last.rating)}
            <span> since {formatDay(dayToMs(first.date))}</span>
          </p>
          <LineChart
            points={pts.map((p) => ({ t: dayToMs(p.date), v: p.rating }))}
            label={`Rating from ${first.rating} on ${first.date} to ${last.rating} on ${last.date}`}
            formatTick={formatInt}
            describe={(i) => ({
              value: formatInt(pts[i]!.rating),
              context: `${formatDay(dayToMs(pts[i]!.date))} · ${pts[i]!.games} game${pts[i]!.games === 1 ? "" : "s"}`,
            })}
          />
        </>
      )}
    </Section>
  );
}

function OpeningsSection({ r }: { r: Report }) {
  const costly = r.openings.filter((o) => o.rating_change < 0).slice(0, 5);
  const helpful = r.openings.filter((o) => o.rating_change > 0).slice(-5);
  const rows = [...costly, ...helpful];
  const worst = costly[0];
  return (
    <Section
      title="Openings"
      question="Which openings cost me rating?"
      footnote="Rating points won or lost across rated games, by opening family and your colour. Openings with at least 5 games."
      table={
        r.openings.length > 0 && (
          <DataTable
            head={["Opening", "Games", "Won", "Win rate", "Rating"]}
            rows={r.openings.map((o) => [
              `${o.opening} (${colorLabel(o.color)})`,
              o.games,
              o.wins,
              formatPct(o.win_rate_pct),
              formatSigned(o.rating_change),
            ])}
          />
        )
      }
    >
      {rows.length === 0 ? (
        <EmptyState title="No opening has 5 games yet">
          Openings show up here once you have played one at least five times in this time control.
        </EmptyState>
      ) : (
        <>
          {worst && (
            <p className={styles.insight}>
              {worst.opening}, with {colorLabel(worst.color)},
              <span> has cost you {formatInt(-worst.rating_change)} points</span>
            </p>
          )}
          <DivergingBars
            label="Rating won or lost by opening"
            rows={rows.map((o) => ({
              key: `${o.opening}-${o.color}`,
              label: o.opening,
              meta: `${colorLabel(o.color)} · ${formatInt(o.games)} games · ${formatPct(o.win_rate_pct)} won`,
              value: o.rating_change,
              display: formatSigned(o.rating_change),
            }))}
          />
        </>
      )}
    </Section>
  );
}

function PhaseSection({ r, note }: { r: Report; note: string }) {
  const phases = r.blunders_by_phase;
  const n = r.analyzed_games;
  const worst = phases?.reduce((a, b) => (b.blunders > a.blunders ? b : a));
  const name = (p: string) => p.charAt(0).toUpperCase() + p.slice(1);
  return (
    <Section
      title="Blunders"
      question="When in the game do I blunder?"
      footnote={phases ? `${note} A blunder is a move that loses 3 or more pawns' worth of advantage.` : undefined}
      table={
        phases && (
          <DataTable
            head={["Phase", "Your moves", "Blunders", "Per game", "Per 100 moves"]}
            rows={phases.map((p) => [
              name(p.phase),
              p.player_moves,
              p.blunders,
              (p.blunders / n).toFixed(2),
              p.player_moves ? ((p.blunders / p.player_moves) * 100).toFixed(1) : "—",
            ])}
          />
        )
      }
    >
      {!phases || !worst ? (
        <WaitingForEngine tc={r.time_class} />
      ) : (
        <>
          <p className={styles.insight}>
            {worst.blunders === 0 ? "No blunders yet" : `Most of your blunders come in the ${worst.phase}`}
          </p>
          <BarList
            label="Blunders per game by phase"
            rows={phases.map((p) => ({
              key: p.phase,
              label: name(p.phase),
              meta: p.player_moves ? `${((p.blunders / p.player_moves) * 100).toFixed(1)} per 100 moves` : undefined,
              value: p.blunders / n,
              display: `${(p.blunders / n).toFixed(2)} per game`,
              tone: p === worst ? "accent" : "muted",
            }))}
          />
        </>
      )}
    </Section>
  );
}

function ClockSection({ r, note }: { r: Report; note: string }) {
  const tp = r.time_pressure;
  if (r.time_class === "daily") {
    return (
      <Section title="Time pressure" question="Do I fall apart when the clock runs down?">
        <EmptyState title="Not measured for daily games">
          Daily games give you days per move, so there is no clock pressure to measure.
        </EmptyState>
      </Section>
    );
  }
  const low = tp?.low_clock;
  const normal = tp?.normal_clock;
  const rate = (b: { blunders: number; moves: number }) => (b.moves ? b.blunders / b.moves : 0);
  const ratio = low && normal && rate(normal) > 0 ? rate(low) / rate(normal) : null;
  return (
    <Section
      title="Time pressure"
      question="Do I fall apart when the clock runs down?"
      footnote={
        tp ? `${note} Accuracy is the share of your moves the engine rated best or good. Clock time is what you had when you started the move.` : undefined
      }
      table={
        tp && low && normal && (
          <DataTable
            head={["Clock when moving", "Moves", "Accuracy", "Blunders"]}
            rows={[
              [`${tp.threshold_seconds} s or more`, normal.moves, normal.accuracy_pct === null ? "—" : formatPct(normal.accuracy_pct), normal.blunders],
              [`Under ${tp.threshold_seconds} s`, low.moves, low.accuracy_pct === null ? "—" : formatPct(low.accuracy_pct), low.blunders],
            ]}
          />
        )
      }
    >
      {!tp || !low || !normal ? (
        <WaitingForEngine tc={r.time_class} />
      ) : low.moves === 0 ? (
        <EmptyState title={`You never dropped under ${tp.threshold_seconds} seconds`}>
          In the games analyzed so far, you always had at least {tp.threshold_seconds} seconds when you moved.
        </EmptyState>
      ) : (
        <>
          <p className={styles.insight}>
            {ratio !== null && ratio >= 1.15
              ? `You blunder ${formatOne(ratio)}× as often with under ${tp.threshold_seconds} seconds`
              : `Your play holds up under ${tp.threshold_seconds} seconds`}
            <span>
              {" "}
              · it happened in {formatPct((tp.low_clock_games / Math.max(1, r.analyzed_games)) * 100)} of games
            </span>
          </p>
          <BarList
            label="Accuracy by time on the clock"
            max={100}
            rows={[
              { key: "normal", label: `${tp.threshold_seconds} seconds or more`, meta: `${formatInt(normal.moves)} moves`, value: normal.accuracy_pct ?? 0, display: normal.accuracy_pct === null ? "—" : `${formatPct(normal.accuracy_pct)} accurate`, tone: "muted" },
              { key: "low", label: `Under ${tp.threshold_seconds} seconds`, meta: `${formatInt(low.moves)} moves`, value: low.accuracy_pct ?? 0, display: low.accuracy_pct === null ? "—" : `${formatPct(low.accuracy_pct)} accurate`, tone: "accent" },
            ]}
          />
        </>
      )}
    </Section>
  );
}

function WaitingForEngine({ tc }: { tc: Report["time_class"] }) {
  return (
    <EmptyState title="Waiting for the engine">
      None of your {TIME_CLASS_LABEL[tc].toLowerCase()} games have been analyzed yet.
      {tc === "bullet" ? " Bullet goes last, after rapid and blitz, so your first report is quick." : ""} This
      section fills in as games finish.
    </EmptyState>
  );
}

export function ReportSkeleton() {
  return (
    <div className={styles.report} aria-busy="true" aria-label="Loading report">
      <div className={styles.skeletonTiles}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className={styles.skeletonTile}>
            <Skeleton width={80} height={14} />
            <Skeleton width={110} height={40} />
          </div>
        ))}
      </div>
      <Skeleton height={260} />
      <Skeleton height={200} />
    </div>
  );
}
