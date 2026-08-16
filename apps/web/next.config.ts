import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle consumed by the Docker runtime stage.
  output: "standalone",
};

export default nextConfig;
