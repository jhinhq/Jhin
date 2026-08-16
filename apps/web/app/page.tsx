import { AutoRefresh } from "@/components/auto-refresh";
import { StatusBadge } from "@/components/status-badge";
import { fetchStackStatus, type DependencyStatus } from "@/lib/status";

export const dynamic = "force-dynamic";

const DEPENDENCY_LABELS: Record<string, string> = {
  postgres: "PostgreSQL",
  nats: "NATS JetStream",
  temporal: "Temporal",
};

function DependencyRow({ dep }: { dep: DependencyStatus }) {
  return (
    <li className="flex items-center justify-between gap-4 rounded-lg border border-white/10 bg-white/[0.03] px-4 py-3">
      <div className="min-w-0">
        <p className="text-sm font-medium">{DEPENDENCY_LABELS[dep.name] ?? dep.name}</p>
        {dep.detail ? (
          <p className="mt-0.5 truncate text-xs text-white/50" title={dep.detail}>
            {dep.detail}
          </p>
        ) : null}
      </div>
      <div className="flex shrink-0 items-center gap-3">
        <span className="text-xs tabular-nums text-white/40">
          {dep.latency_ms.toFixed(0)} ms
        </span>
        <StatusBadge status={dep.status} />
      </div>
    </li>
  );
}

export default async function Home() {
  const status = await fetchStackStatus();
  const appName = status.app ?? process.env.APP_NAME ?? "Jhin";

  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center px-6 py-16">
      <AutoRefresh />
      <header className="mb-8">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">{appName}</h1>
          <StatusBadge status={status.overall} />
        </div>
        <p className="mt-1 text-sm text-white/50">
          Stack status — Phase 1 foundation. Refreshes automatically.
        </p>
      </header>
      <ul className="space-y-2">
        <DependencyRow dep={status.api} />
        {status.dependencies.map((dep) => (
          <DependencyRow key={dep.name} dep={dep} />
        ))}
      </ul>
      {status.overall === "unreachable" ? (
        <p className="mt-6 text-sm text-white/50">
          The API did not return a readiness report; dependency states are unknown.
        </p>
      ) : null}
    </main>
  );
}
