import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";
import { withSentryConfig } from "@sentry/nextjs";

const withNextIntl = createNextIntlPlugin("./i18n/request.ts");

// CSP: 'unsafe-inline' needed for Next.js inline styles; 'unsafe-eval' for dev HMR.
// In production with HTTPS, replace 'unsafe-eval' with nonce-based CSP.
const csp = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "connect-src 'self' ws: wss:",
  "font-src 'self'",
  "frame-src 'none'",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

const securityHeaders = [
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  {
    key: "Permissions-Policy",
    value: "camera=(), microphone=(), geolocation=()",
  },
  { key: "Content-Security-Policy", value: csp },
];

const nextConfig: NextConfig = {
  output: "standalone",
  // Allow HMR from local network so changes are visible when accessed via 192.168.x.x
  allowedDevOrigins: [
    "192.168.1.246",
    "192.168.1.0/24",
    "10.0.0.0/8",
    "172.16.0.0/12",
  ],
  async headers() {
    return [{ source: "/(.*)", headers: securityHeaders }];
  },
  // /api/* is handled by the Route Handler (runtime BACKEND_URL).
  // /ws/* uses a rewrite because Route Handlers don't support WebSocket upgrades;
  // the destination is baked at build time from BACKEND_URL env (see Dockerfile).
  async rewrites() {
    const backend = process.env.BACKEND_URL ?? "http://localhost:8000";
    return [
      { source: "/ws/:path*", destination: `${backend}/ws/:path*` },
      { source: "/health", destination: `${backend}/health` },
    ];
  },
};

export default withSentryConfig(withNextIntl(nextConfig), {
  silent: true,
  sourcemaps: { disable: true },
});
