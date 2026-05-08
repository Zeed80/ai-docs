import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

const nextConfig: NextConfig = {
  output: "standalone",
  // Allow HMR from local network so changes are visible when accessed via 192.168.x.x
  allowedDevOrigins: [
    "192.168.1.246",
    "192.168.1.0/24",
    "10.0.0.0/8",
    "172.16.0.0/12",
  ],
  async rewrites() {
    const backend = process.env.BACKEND_URL ?? "http://localhost:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/ws/:path*", destination: `${backend}/ws/:path*` },
      { source: "/health", destination: `${backend}/health` },
    ];
  },
};

export default withNextIntl(nextConfig);
