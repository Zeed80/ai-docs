"use client";

function isLocalHost(hostname: string): boolean {
  return (
    hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1"
  );
}

export function getApiBaseUrl(): string {
  const configured =
    process.env.NEXT_PUBLIC_API_URL ??
    process.env.NEXT_PUBLIC_API_BASE_URL ??
    "http://localhost:8000";

  if (typeof window === "undefined") return configured;

  try {
    const url = new URL(configured);
    if (isLocalHost(url.hostname)) {
      return `${url.protocol}//${window.location.hostname}:${url.port || "8000"}`;
    }
    return configured;
  } catch {
    return `${window.location.protocol}//${window.location.hostname}:8000`;
  }
}

export function getWebSocketBaseUrl(): string {
  if (typeof window === "undefined") {
    return getApiBaseUrl().replace(/^https?/, (p) =>
      p === "https" ? "wss" : "ws",
    );
  }

  const apiUrl = new URL(getApiBaseUrl());
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${apiUrl.hostname}:${apiUrl.port || "8000"}`;
}

export function getAiAgentWebSocketUrl(): string {
  const configured = process.env.NEXT_PUBLIC_AIAGENT_WS_URL;
  if (configured) return configured;

  if (typeof window === "undefined") {
    return "ws://localhost:18789";
  }

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.hostname}:18789`;
}
