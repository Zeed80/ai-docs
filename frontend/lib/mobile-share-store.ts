/**
 * In-memory hand-off for files shared into the app from other apps.
 * File objects can't survive a route change via URL, so the share handler stashes
 * them here and /mobile/share reads them once.
 */
import type { SharedPayload } from "@/lib/native-bridge";

let pending: SharedPayload | null = null;

export function setPendingShare(payload: SharedPayload): void {
  pending = payload;
}

export function takePendingShare(): SharedPayload | null {
  const p = pending;
  pending = null;
  return p;
}

export function hasPendingShare(): boolean {
  return !!pending && pending.files.length > 0;
}
