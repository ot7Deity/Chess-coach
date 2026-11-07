import Link from "next/link";
import styles from "./Button.module.css";

type Variant = "primary" | "secondary";

type Props = React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant };

export function Button({ variant = "primary", className, ...rest }: Props) {
  return <button {...rest} className={[styles.button, className].filter(Boolean).join(" ")} data-variant={variant} />;
}

export function ButtonLink({ href, variant = "secondary", children }: { href: string; variant?: Variant; children: React.ReactNode }) {
  return (
    <Link href={href} className={styles.button} data-variant={variant}>
      {children}
    </Link>
  );
}
