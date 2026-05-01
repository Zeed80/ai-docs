import { getApiBaseUrl } from "@/lib/api-base";
/**
 * Auth helpers — token storage and user info.
 * When AUTH_ENABLED=false (default dev), all API calls work without a token.
 */

const TOKEN_KEY = "ai_workspace_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export function authHeaders(): HeadersInit {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export interface UserInfo {
  sub: string;
  email: string;
  name: string;
  preferred_username: string;
  roles: string[];
  groups: string[];
}

const API = getApiBaseUrl();

export async function fetchMe(): Promise<UserInfo | null> {
  try {
    const res = await fetch(`${API}/api/auth/me`, {
      headers: authHeaders(),
    });
    if (!res.ok) return null;
    return res.json();
  } catch {
    return null;
  }
}

export function loginUrl(): string {
  const callback = `${window.location.origin}/auth/callback`;
  return `${API}/api/auth/login?redirect_uri=${encodeURIComponent(callback)}`;
}

export async function logout(): Promise<void> {
  await fetch(`${API}/api/auth/logout`, {
    method: "POST",
    headers: authHeaders(),
  });
  clearToken();
}
