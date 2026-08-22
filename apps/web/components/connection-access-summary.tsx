import { Badge, focusRing } from "@/components/ui";
import type { ConnectionAccessSummaryOut } from "@/lib/types";

function formatScope(scope: Record<string, string>): string {
  return Object.entries(scope).map(([key, value]) => `${key}=${value}`).join(" · ");
}

export function ConnectionAccessSummary({ summary }: { summary: ConnectionAccessSummaryOut }) {
  if (summary.agents.length === 0) {
    return <p className="text-sm text-faint">No agents have grants relevant to this connection.</p>;
  }
  return (
    <div className="space-y-3">
      <p className="text-sm text-dim">
        A grant controls tool eligibility; it does not bypass the agent&apos;s current approval policy.
      </p>
      {summary.agents.map((agent) => (
        <article key={agent.agent_id} className="rounded-xl border border-line bg-raised px-4 py-3">
          <header className="flex items-center gap-2">
            <strong className="text-sm font-semibold text-ink">{agent.agent_name}</strong>
            <Badge tone={agent.authorized ? "ok" : "danger"}>
              {agent.authorized ? "Authorized" : "Not authorized"}
            </Badge>
          </header>
          {agent.authorized_tool_names.length > 0 ? (
            <p className="mt-1 text-xs text-dim">{agent.authorized_tool_names.join(", ")}</p>
          ) : null}
          <details className="mt-2">
            <summary className={`cursor-pointer rounded-md text-xs font-medium text-dim ${focusRing}`}>Advanced grant details</summary>
            <ul className="mt-2 space-y-2">
              {agent.grants.map((grant) => (
                <li key={grant.grant_id} className="text-xs">
                  <div className="flex items-center gap-2">
                    <code className="font-mono text-xs text-ink">{grant.capability}</code>
                    <Badge tone={grant.effect === "allow" ? "ok" : "danger"}>{grant.effect}</Badge>
                  </div>
                  <p className="mt-1 text-faint">{formatScope(grant.scope)}</p>
                  {grant.eligibility_reason ? (
                    <p className="mt-1 text-warn">{grant.eligibility_reason}</p>
                  ) : null}
                </li>
              ))}
            </ul>
          </details>
        </article>
      ))}
    </div>
  );
}
