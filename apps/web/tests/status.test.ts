import { describe, expect, it } from "vitest";

import { parseReadiness, toStackStatus } from "@/lib/status";

const validPayload = {
  status: "ok",
  app: "Jhin",
  dependencies: [
    { name: "postgres", status: "ok", latency_ms: 2.1 },
    { name: "nats", status: "ok", latency_ms: 1.4 },
    { name: "temporal", status: "ok", latency_ms: 5.9 },
  ],
};

describe("parseReadiness", () => {
  it("accepts a valid readiness payload", () => {
    const parsed = parseReadiness(validPayload);
    expect(parsed).not.toBeNull();
    expect(parsed?.dependencies.map((d) => d.name)).toEqual(["postgres", "nats", "temporal"]);
  });

  it("accepts a degraded payload with error details", () => {
    const parsed = parseReadiness({
      ...validPayload,
      status: "degraded",
      dependencies: [
        { name: "nats", status: "error", latency_ms: 5000, detail: "timeout" },
      ],
    });
    expect(parsed?.status).toBe("degraded");
    expect(parsed?.dependencies[0].detail).toBe("timeout");
  });

  it.each([null, "nope", {}, { status: "weird", app: "Jhin", dependencies: [] }, { ...validPayload, dependencies: [{ name: 1 }] }])(
    "rejects malformed payload %#",
    (payload) => {
      expect(parseReadiness(payload)).toBeNull();
    },
  );
});

describe("toStackStatus", () => {
  it("maps a parsed payload to per-dependency stack status", () => {
    const status = toStackStatus(parseReadiness(validPayload), 12);
    expect(status.overall).toBe("ok");
    expect(status.api.status).toBe("ok");
    expect(status.api.latency_ms).toBe(12);
    expect(status.dependencies).toHaveLength(3);
  });

  it("reports unreachable when the API gave no valid response", () => {
    const status = toStackStatus(null, 8000, "fetch failed");
    expect(status.overall).toBe("unreachable");
    expect(status.api.status).toBe("error");
    expect(status.api.detail).toBe("fetch failed");
    expect(status.dependencies).toEqual([]);
  });
});
