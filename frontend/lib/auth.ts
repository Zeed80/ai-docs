/**
 * Auth helpers — cookie-based authentication.
 * Token is stored in httpOnly cookie set by the backend.
 * JS cannot access it directly (XSS protection).
 */

function _apiBase(): string {
  if (typeof window !== "undefined") {
    return (
      process.env.NEXT_PUBLIC_API_URL?.trim() ||
      process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ||
      ""
    );
  }
  return process.env.INTERNAL_API_URL ?? "http://127.0.0.1:8000";
}

// Kept as no-ops for backward compatibility — token is in httpOnly cookie now
export function getToken(): string | null {
  return null;
}

export function setToken(_token: string): void {}

export function clearToken(): void {}

// authHeaders() returns empty — cookie is sent automatically with credentials:"include"
export function authHeaders(): HeadersInit {
  return {};
}

// CSRF token for state-changing requests (double-submit cookie pattern)
export function getCSRFToken(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export function csrfHeaders(): HeadersInit {
  const csrf = getCSRFToken();
  return csrf ? { "X-CSRF-Token": csrf } : {};
}

export interface UserInfo {
  sub: string;
  email: string;
  name: string;
  preferred_username: string;
  roles: string[];
  groups: string[];
}

export async function fetchMe(): Promise<UserInfo | null> {
  try {
    const res = await fetch(`${_apiBase()}/api/auth/me`, {
      credentials: "include",
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export function loginUrl(next?: string): string {
  const callback = `${window.location.origin}/auth/callback`;
  let url = `${_apiBase()}/api/auth/login?redirect_uri=${encodeURIComponent(callback)}`;
  if (next && next.startsWith("/")) {
    url += `&next=${encodeURIComponent(next)}`;
  }
  return url;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${_apiBase()}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: csrfHeaders(),
    });
  } catch {
    // ignore errors — redirect to login regardless
  }
}
