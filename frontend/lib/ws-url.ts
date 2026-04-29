import { getWebSocketBaseUrl } from "@/lib/api-base";

/**
 * crypto.randomUUID() requires a secure context (HTTPS). Use this instead.
 */
export function genId(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  // Fallback: works on HTTP (non-secure context)
  const buf = new Uint8Array(16);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(buf);
  } else {
    for (let i = 0; i < 16; i++) buf[i] = (Math.random() * 256) | 0;
  }
  buf[6] = (buf[6] & 0x0f) | 0x40;
  buf[8] = (buf[8] & 0x3f) | 0x80;
  return [...buf]
    .map((b, i) =>
      [4, 6, 8, 10].includes(i)
        ? `-${b.toString(16).padStart(2, "0")}`
        : b.toString(16).padStart(2, "0"),
    )
    .join("");
}

/**
 * Derives the WebSocket base URL from NEXT_PUBLIC_API_URL or, when running
 * in the browser, from window.location (so remote clients always hit the
 * correct server regardless of the configured env var).
 */
export function getWsUrl(): string {
  return getWebSocketBaseUrl();
}
