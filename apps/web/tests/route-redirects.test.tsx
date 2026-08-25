/** Routes that only redirect. Both are real HTTP redirects declared in
 * next.config.ts so deep links resolve before any React runs: `/connectors`
 * permanently, because Apps absorbed it, and `/` to the Home landing. */

import { describe, expect, it } from "vitest";
import nextConfig from "@/next.config";

async function redirects() {
  return (await nextConfig.redirects?.()) ?? [];
}

describe("route redirects", () => {
  it("sends /connectors to /apps permanently", async () => {
    expect(await redirects()).toContainEqual({
      source: "/connectors",
      destination: "/apps",
      permanent: true,
    });
  });

  it("lands / on Home", async () => {
    expect(await redirects()).toContainEqual({
      source: "/",
      destination: "/home",
      permanent: false,
    });
  });
});
