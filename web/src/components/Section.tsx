import styles from "./Section.module.css";

interface Props {
  title: string;
  /** The question this section answers, in the player's words. */
  question: string;
  children: React.ReactNode;
  /** Sample size or caveat, shown small under the content. */
  footnote?: React.ReactNode;
  /** The same data as a table, behind a disclosure: the accessible twin of the chart. */
  table?: React.ReactNode;
  /** Dims the content while a newer version loads, instead of flashing a skeleton. */
  stale?: boolean;
}

export function Section({ title, question, children, footnote, table, stale }: Props) {
  return (
    <section className={styles.section} aria-busy={stale || undefined}>
      <header className={styles.header}>
        <h2 className={styles.title}>{title}</h2>
        <p className={styles.question}>{question}</p>
      </header>
      <div className={styles.body} data-stale={stale || undefined}>
        {children}
      </div>
      {(footnote || table) && (
        <footer className={styles.footer}>
          {footnote && <p className={styles.footnote}>{footnote}</p>}
          {table && (
            <details className={styles.details}>
              <summary>Show as table</summary>
              {table}
            </details>
          )}
        </footer>
      )}
    </section>
  );
}

/** A plain data table for the "Show as table" twin of each chart. */
export function DataTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <div className={styles.tableWrap}>
      <table className={styles.table}>
        <thead>
          <tr>
            {head.map((h) => (
              <th key={h} scope="col">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((c, j) => (
                <td key={j}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
