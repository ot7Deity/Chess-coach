import { Suspense } from "react";
import { PageShell } from "@/components/PageShell";
import { AddPlayerForm } from "./AddPlayerForm";
import styles from "./landing.module.css";

const QUESTIONS = [
  { title: "Where you blunder", body: "Opening, middlegame or endgame, from every move of every game." },
  { title: "Which openings cost you", body: "Rating points won and lost, opening by opening, with each colour." },
  { title: "How you play on the clock", body: "Your accuracy with under 30 seconds left, against the rest." },
];

export default function Home() {
  return (
    <PageShell>
      <div className={styles.hero}>
        <h1 className={styles.title}>Find out why you lose.</h1>
        <p className={styles.lead}>
          Chess Coach reads your whole Chess.com history, grades every move with a chess engine, and shows you where
          your rating actually goes. Bullet, blitz, rapid and daily, each on its own.
        </p>
        {/* The form reads ?u= (prefilled from "Analyze their games" links). */}
        <Suspense>
          <AddPlayerForm />
        </Suspense>
      </div>
      <ul className={styles.questions}>
        {QUESTIONS.map((q) => (
          <li key={q.title}>
            <h2>{q.title}</h2>
            <p>{q.body}</p>
          </li>
        ))}
      </ul>
    </PageShell>
  );
}
