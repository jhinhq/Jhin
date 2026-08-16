import type { NextConfig } from "next";

// Baked into the build (Docker sets API_INTERNAL_URL=http://api:8000).
// Browser calls go same-origin to /api/* so auth cookies never cross origins.
const apiInternalUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  // Self-contained server bundle consumed by the Docker runtime stage.
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiInternalUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
