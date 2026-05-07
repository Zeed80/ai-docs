"use client";

function isLocalHost(hostname: string): boolean {
  return (
    hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1"
  );
}

function configuredApiUrl(): string {
  return (
    process.env.NEXT_PUBLIC_API_URL?.trim() ||
    process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ||
    ""
  );
}

function configuredWsUrl(): string {
  return process.env.NEXT_PUBLIC_WS_URL?.trim() || "";
}

function isBackendDevPort(url: URL): boolean {
  return url.port === "8000";
}

export function getApiBaseUrl(): string {
  const configured = configuredApiUrl();

  if (typeof window === "undefined") return configured || "http://backend:8000";
  if (!configured || configured === "same-origin") return "";

  try {
    const url = new URL(configured);
    const currentHost = window.location.hostname;
    if (isBackendDevPort(url)) {
      return "";
    }
    if (isLocalHost(url.hostname) && !isLocalHost(currentHost)) {
      return "";
    }
    return configured;
  } catch {
    return "";
  }
}

export function getWebSocketBaseUrl(): string {
  const configured = configuredWsUrl();
  if (typeof window === "undefined") {
    return (configured || getApiBaseUrl() || "http://backend:8000").replace(/^https?/, (p) =>
      p === "https" ? "wss" : "ws",
    );
  }

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  if (configured && configured !== "same-origin") {
    try {
      const url = new URL(configured);
      if (!isBackendDevPort(url) && (!isLocalHost(url.hostname) || isLocalHost(window.location.hostname))) {
        return configured.replace(/^http/, "ws");
      }
    } catch {
      // Fall through to same-origin.
    }
  }
  const apiBase = getApiBaseUrl();
  if (!apiBase) return `${proto}//${window.location.host}`;
  const apiUrl = new URL(apiBase);
  return `${proto}//${apiUrl.host}`;
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
