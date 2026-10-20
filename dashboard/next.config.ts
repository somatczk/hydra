import type { NextConfig } from "next";
import packageJson from "./package.json";

const isProduction = process.env.NODE_ENV === "production";

/**
 * Production CSP: only first-party scripts run; the TradingView chart is a cross-origin iframe.
 * 'unsafe-inline' stays for Next's inline bootstrap until a nonce-based policy (P2/P3).
 */
export const CONTENT_SECURITY_POLICY = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "frame-src https://s.tradingview.com https://www.tradingview.com",
  "frame-ancestors 'none'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join("; ");

export const SECURITY_HEADERS = [
  { key: "Content-Security-Policy", value: CONTENT_SECURITY_POLICY },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
];

const nextConfig: NextConfig = {
  output: "standalone",
  env: {
    APP_VERSION: packageJson.version,
  },
  async headers() {
    // next dev needs eval for fast refresh, so the policy applies to production builds only
    return isProduction ? [{ source: "/:path*", headers: SECURITY_HEADERS }] : [];
  },
  async rewrites() {
    // Local `next dev` only: in production Traefik routes /api to the engine
    if (isProduction) return [];
    const apiUrl = process.env.API_URL || "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
