import { defineConfig } from "@playwright/test";
import { createRequire } from "node:module";
import path from "node:path";

const resolveDependency = createRequire(__filename);
const vitePackage = resolveDependency.resolve("vite/package.json", {
  paths: [resolveDependency.resolve("vitest/package.json")],
});
const viteBin = path.join(path.dirname(vitePackage), "bin", "vite.js");

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  outputDir: "../test-results/layout",
  workers: 2,
  retries: 0,
  use: { baseURL: "http://127.0.0.1:4178", browserName: "chromium", screenshot: "only-on-failure" },
  webServer: {
    command: `"${process.execPath}" "${viteBin}" --config browser-layout/vite.config.ts`,
    cwd: "..",
    url: "http://127.0.0.1:4178",
    reuseExistingServer: !process.env.CI,
  },
});
