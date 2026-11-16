import Link from "next/link";
import { usingFixtures } from "@/lib/api";
import styles from "./PageShell.module.css";

/** Top bar, centred content column, and an honest note when the data is sample data. */
export function PageShell({ children }: { children: React.ReactNode }) {
  return (
    <div className={styles.shell}>
      <header className={styles.bar}>
        <Link href="/" className={styles.wordmark}>
          Chess Coach
        </Link>
        {usingFixtures && (
          <span className={styles.badge} title="NEXT_PUBLIC_API_URL is not set, so the dashboard is showing fixtures.">
            Sample data
          </span>
        )}
      </header>
      <main className={styles.main}>{children}</main>
      {usingFixtures && (
        <footer className={styles.footer}>
          Results, ratings and openings are real games of deitythemenace. Blunder and clock
          numbers are invented placeholders until the analysis engine has run.
        </footer>
      )}
    </div>
  );
}
