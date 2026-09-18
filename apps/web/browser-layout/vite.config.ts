import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  root: path.resolve(__dirname),
  plugins: [react()],
  define: { "process.env.NEXT_PUBLIC_JHIN_DESKTOP": '"0"' },
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: {
      "@": path.resolve(__dirname, ".."),
      "next/link": path.resolve(__dirname, "link.tsx"),
      "next/navigation": path.resolve(__dirname, "api-keys-navigation.tsx"),
    },
  },
  css: { postcss: path.resolve(__dirname, "..") },
  server: { host: "127.0.0.1", port: 4178, strictPort: true },
});
