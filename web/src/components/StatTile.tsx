import styles from "./StatTile.module.css";

interface Props {
  label: string;
  /** Already formatted. `null` renders an em dash with `missing` as the reason. */
  value: string | null;
  detail?: React.ReactNode;
  /** Shown instead of the detail when value is null, e.g. "Waiting for analysis". */
  missing?: string;
}

/** One headline number: label, a large value, one line of context. */
export function StatTile({ label, value, detail, missing }: Props) {
  return (
    <div className={styles.tile}>
      <dt className={styles.label}>{label}</dt>
      <dd className={styles.value}>{value ?? "—"}</dd>
      <dd className={styles.detail}>{value === null ? missing : detail}</dd>
    </div>
  );
}

export function StatRow({ children }: { children: React.ReactNode }) {
  return <dl className={styles.row}>{children}</dl>;
}
