/**
 * Auth helpers — cookie-based authentication.
 * Token is stored in httpOnly cookie set by the backend.
 * JS cannot access it directly (XSS protection).
 */

function _apiBase(): string {
  if (typeof window !== "undefined") {
    const val =
      process.env.NEXT_PUBLIC_API_URL?.trim() ||
      process.env.NEXT_PUBLIC_API_BASE_URL?.trim() ||
      "";
    return !val || val === "same-origin" ? "" : val;
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

export async function logout(): Promise<string> {
  const fallback = "/auth/login";
  try {
    const res = await fetch(`${_apiBase()}/api/auth/logout`, {
      method: "POST",
      credentials: "include",
      headers: csrfHeaders(),
    });
    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      return (data.logout_url as string) || fallback;
    }
  } catch {
    // fall through to redirect
  }
  return fallback;
}

/**
 * Fetch wrapper that always includes the auth cookie and CSRF header.
 * On 401 responses it redirects to /auth/login (browser-only).
 */
export async function apiFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const method = (init?.method ?? "GET").toUpperCase();
  const isMutation = ["POST", "PUT", "PATCH", "DELETE"].includes(method);

  const res = await fetch(url, {
    credentials: "include",
    ...init,
    headers: {
      ...(isMutation ? csrfHeaders() : {}),
      ...init?.headers,
    },
  });

  if (
    res.status === 401 &&
    typeof window !== "undefined" &&
    !window.location.pathname.startsWith("/auth/")
  ) {
    window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
  }

  return res;
}
