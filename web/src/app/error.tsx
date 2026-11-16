"use client";

import { Button } from "@/components/Button";
import { PageShell } from "@/components/PageShell";
import { ErrorState } from "@/components/States";

/**
 * Last line of defence: a render error anywhere below the root layout lands
 * here instead of a blank page. API failures never get this far; each page
 * handles those itself with a retry.
 */
export default function RootError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <PageShell>
      <ErrorState
        title="This page hit a problem"
        action={
          <Button variant="secondary" onClick={reset}>
            Try again
          </Button>
        }
      >
        Something on this page failed to display. Trying again usually fixes it; if not, go back to the start.
      </ErrorState>
    </PageShell>
  );
}
