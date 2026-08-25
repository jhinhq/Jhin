import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname) },
  },
  test: {
    environment: "jsdom",
    // 51 suites run in parallel across every core; under that contention the
    // 5s default expires on render-heavy React tests that pass comfortably in
    // isolation. This is headroom for scheduling, not for slow assertions.
    testTimeout: 20_000,
    hookTimeout: 20_000,
    setupFiles: ["tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
