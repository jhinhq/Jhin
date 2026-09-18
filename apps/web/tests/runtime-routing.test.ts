import { expect, it } from "vitest";
import config, { contentSecurityPolicy } from "../next.config";

it("preserves preview root slashes before forwarding to the private gateway", async () => {
  expect(config.skipTrailingSlashRedirect).toBe(true);
  const rewrites = await config.rewrites?.();
  expect(rewrites).toContainEqual({ source: "/runtime/:path*", destination: "http://runtime-gateway:8086/runtime/:path*" });
  expect(rewrites).toContainEqual({ source: "/runtime/previews/:sessionId/:ticket", destination: "http://runtime-gateway:8086/runtime/previews/:sessionId/:ticket/" });
  if (!Array.isArray(rewrites)) throw new Error("Expected ordered runtime rewrites");
  expect(rewrites.findIndex((rule) => rule.source.endsWith(":ticket"))).toBeLessThan(rewrites.findIndex((rule) => rule.source === "/runtime/:path*"));
  expect(contentSecurityPolicy).toContain("connect-src 'self'");
  const headers = await config.headers?.();
  expect(headers?.every((rule) => rule.source.includes("runtime/"))).toBe(true);
});
