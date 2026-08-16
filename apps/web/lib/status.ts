/**
 * Stack status: readiness of the API and its dependencies (Postgres, NATS,
 * Temporal), as reported by the API readiness endpoint.
 */

export type DependencyState = "ok" | "error";

export interface DependencyStatus {
  name: string;
  status: DependencyState;
  latency_ms: number;
  detail?: string | null;
}

export interface StackStatus {
  /** Overall state; "unreachable" means the API itself did not answer. */
  overall: "ok" | "degraded" | "unreachable";
  app: string | null;
  api: DependencyStatus;
  dependencies: DependencyStatus[];
}

interface ReadinessPayload {
  status: "ok" | "degraded";
  app: string;
  dependencies: DependencyStatus[];
}

function isDependencyStatus(value: unknown): value is DependencyStatus {
  if (typeof value !== "object" || value === null) return false;
  const dep = value as Record<string, unknown>;
  return (
    typeof dep.name === "string" &&
    (dep.status === "ok" || dep.status === "error") &&
    typeof dep.latency_ms === "number"
  );
}

export function parseReadiness(payload: unknown): ReadinessPayload | null {
  if (typeof payload !== "object" || payload === null) return null;
  const body = payload as Record<string, unknown>;
  if (body.status !== "ok" && body.status !== "degraded") return null;
  if (typeof body.app !== "string") return null;
  if (!Array.isArray(body.dependencies) || !body.dependencies.every(isDependencyStatus)) {
    return null;
  }
  return {
    status: body.status,
    app: body.app,
    dependencies: body.dependencies,
  };
}

export function toStackStatus(
  payload: ReadinessPayload | null,
  apiLatencyMs: number,
  apiError?: string,
): StackStatus {
  if (payload === null) {
    return {
      overall: "unreachable",
      app: null,
      api: {
        name: "api",
        status: "error",
        latency_ms: apiLatencyMs,
        detail: apiError ?? "no valid readiness response",
      },
      dependencies: [],
    };
  }
  return {
    overall: payload.status,
    app: payload.app,
    api: { name: "api", status: "ok", latency_ms: apiLatencyMs },
    dependencies: payload.dependencies,
  };
}

/** Server-side fetch of the API readiness report. Never throws. */
export async function fetchStackStatus(): Promise<StackStatus> {
  const base = process.env.API_INTERNAL_URL ?? "http://localhost:8000";
  const started = Date.now();
  try {
    const response = await fetch(`${base}/api/v1/health/ready`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const latency = Date.now() - started;
    // 503 still carries a full per-dependency report.
    const payload = parseReadiness(await response.json());
    return toStackStatus(payload, latency);
  } catch (error) {
    const latency = Date.now() - started;
    const message = error instanceof Error ? error.message : String(error);
    return toStackStatus(null, latency, message);
  }
}
