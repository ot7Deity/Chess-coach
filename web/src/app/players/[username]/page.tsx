import type { Metadata } from "next";
import { Suspense } from "react";
import { PageShell } from "@/components/PageShell";
import { decodeParam } from "@/lib/api";
import { Overview } from "./Overview";

interface Props {
  params: Promise<{ username: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { username } = await params;
  return { title: decodeParam(username).toLowerCase() };
}

export default async function PlayerPage({ params }: Props) {
  const { username } = await params;
  return (
    <PageShell>
      {/* Overview reads ?tc= from the URL, which needs a Suspense boundary. */}
      <Suspense>
        {/* Keyed by player so nothing from one player can be shown under another's name. */}
        <Overview key={username} username={decodeParam(username)} />
      </Suspense>
    </PageShell>
  );
}
