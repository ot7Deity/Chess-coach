import type { Metadata } from "next";
import { PageShell } from "@/components/PageShell";
import { decodeParam } from "@/lib/api";
import { Progress } from "./Progress";

interface Props {
  params: Promise<{ username: string }>;
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { username } = await params;
  return { title: `Progress · ${decodeParam(username).toLowerCase()}` };
}

export default async function ProgressPage({ params }: Props) {
  const { username } = await params;
  return (
    <PageShell>
      <Progress key={username} username={decodeParam(username)} />
    </PageShell>
  );
}
