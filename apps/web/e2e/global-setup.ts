/**
 * Fail fast, and in plain language, when the dev stack the specs drive is not
 * there. Without this the first spec dies inside a fixture on a connection
 * refused, which says nothing about what to do next.
 */

import { request, type FullConfig } from "@playwright/test";

export default async function globalSetup(config: FullConfig): Promise<void> {
  const baseURL = config.projects[0]?.use.baseURL;
  if (!baseURL) throw new Error("no baseURL configured");

  const context = await request.newContext({ baseURL });
  try {
    const response = await context.get("/api/v1/health", { timeout: 15_000 });
    if (!response.ok()) throw new Error(`health check answered ${response.status()}`);
  } catch (cause) {
    throw new Error(
      `Cannot reach the Jhin dev stack at ${baseURL}: ${(cause as Error).message}\n` +
        "Start it with:  docker compose -f compose.yaml -f compose.dev.yaml up -d\n" +
        "Point the specs elsewhere with JHIN_E2E_BASE_URL.",
    );
  } finally {
    await context.dispose();
  }
}
