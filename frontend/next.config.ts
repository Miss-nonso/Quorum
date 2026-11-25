import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  // A self-contained server, only for the Docker image (its Dockerfile sets NEXT_OUTPUT).
  // `next start` (local, E2E) and Vercel use the default output.
  output: process.env.NEXT_OUTPUT === "standalone" ? "standalone" : undefined,
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
