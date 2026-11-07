import type { Metadata } from "next";
import { Suspense } from "react";
import { PageShell } from "@/components/PageShell";
import { Overview } from "./Overview";

interface Props {
  params: Promise<{ username: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { username } = await params;
  return { title: decodeURIComponent(username).toLowerCase() };
}

export default async function PlayerPage({ params }: Props) {
  const { username } = await params;
  return (
    <PageShell>
      {/* Overview reads ?tc= from the URL, which needs a Suspense boundary. */}
      <Suspense>
        <Overview username={decodeURIComponent(username)} />
      </Suspense>
    </PageShell>
  );
}
