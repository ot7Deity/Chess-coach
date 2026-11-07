import styles from "./States.module.css";

/** A soft placeholder block with the shape of the content it stands in for. */
export function Skeleton({ height = 16, width = "100%" }: { height?: number; width?: number | string }) {
  return <span className={styles.skeleton} style={{ height, width }} aria-hidden="true" />;
}

interface MessageProps {
  title: string;
  children?: React.ReactNode;
  action?: React.ReactNode;
}

/** Nothing to show yet, and why, and what (if anything) to do about it. */
export function EmptyState({ title, children, action }: MessageProps) {
  return (
    <div className={styles.message}>
      <p className={styles.title}>{title}</p>
      {children && <p className={styles.body}>{children}</p>}
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
}

/** Something went wrong. Says so plainly and offers a retry, never a stack trace. */
export function ErrorState({ title = "Something went wrong", children, action }: Partial<MessageProps>) {
  return (
    <div className={styles.message} role="alert">
      <p className={styles.title}>
        <span className={styles.icon} aria-hidden="true">
          !
        </span>
        {title}
      </p>
      {children && <p className={styles.body}>{children}</p>}
      {action && <div className={styles.action}>{action}</div>}
    </div>
  );
}
