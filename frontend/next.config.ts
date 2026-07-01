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
    // camera/microphone allowed for same-origin so the mobile WebView can use the
    // native camera (document scan) and voice input; geolocation stays disabled.
    key: "Permissions-Policy",
    value: "camera=(self), microphone=(self), geolocation=()",
  },
  { key: "Content-Security-Policy", value: csp },
];

// /studio embeds the live ComfyUI UI (Workflow tab) in a same-origin iframe
// pointed at our own authenticated backend proxy (backend/app/api/
// comfyui_proxy.py) — the site-wide `frame-src 'none'` above blocks that
// outright (confirmed live: the iframe never even attempted a network
// request, browser silently enforced CSP client-side). Scoped to this one
// route only; every other page keeps the strict no-framing default.
const studioCsp = csp.replace("frame-src 'none'", "frame-src 'self'");
const studioHeaders = securityHeaders.map((h) =>
  h.key === "Content-Security-Policy" ? { ...h, value: studioCsp } : h,
);

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
    return [
      { source: "/(.*)", headers: securityHeaders },
      { source: "/studio", headers: studioHeaders },
    ];
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
