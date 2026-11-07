import { ButtonLink } from "@/components/Button";
import { PageShell } from "@/components/PageShell";
import { EmptyState } from "@/components/States";

export default function NotFound() {
  return (
    <PageShell>
      <EmptyState title="This page doesn't exist" action={<ButtonLink href="/">Go to Chess Coach</ButtonLink>}>
        The link may be mistyped, or the page may have moved.
      </EmptyState>
    </PageShell>
  );
}
