/**
 * Review streak — manages queue of documents for keyboard-first review.
 *
 * Loads needs_review documents, tracks position, prefetches next,
 * provides streak counter.
 */

import { documents as docsApi, type Document } from "./api-client";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface ReviewQueue {
  /** IDs of documents in the review queue */
  ids: string[];
  /** Current index in queue */
  index: number;
  /** Total streak count (approved + rejected in this session) */
  streakCount: number;
  /** Prefetched next document (or null) */
  prefetched: Document | null;
}

let _queue: ReviewQueue = {
  ids: [],
  index: -1,
  streakCount: 0,
  prefetched: null,
};

let _listeners: Array<(q: ReviewQueue) => void> = [];

function notify() {
  for (const fn of _listeners) fn({ ..._queue });
}

export function subscribeReviewQueue(fn: (q: ReviewQueue) => void): () => void {
  _listeners.push(fn);
  fn({ ..._queue });
  return () => {
    _listeners = _listeners.filter((f) => f !== fn);
  };
}

export async function loadReviewQueue(): Promise<ReviewQueue> {
  try {
    const res = await docsApi.list({ status: "needs_review", limit: "100" });
    _queue = {
      ids: res.items.map((d) => d.id),
      index: -1,
      streakCount: 0,
      prefetched: null,
    };
  } catch {
    _queue = { ids: [], index: -1, streakCount: 0, prefetched: null };
  }
  notify();
  return { ..._queue };
}

export function currentDocId(): string | null {
  if (_queue.index >= 0 && _queue.index < _queue.ids.length) {
    return _queue.ids[_queue.index];
  }
  return null;
}

export function setCurrentIndex(docId: string): void {
  const idx = _queue.ids.indexOf(docId);
  if (idx >= 0) {
    _queue.index = idx;
    prefetchNext();
    notify();
  }
}

export function nextDocId(): string | null {
  const next = _queue.index + 1;
  if (next < _queue.ids.length) {
    return _queue.ids[next];
  }
  return null;
}

export function advanceStreak(): string | null {
  _queue.streakCount++;
  // Remove current from queue (it's been decided)
  if (_queue.index >= 0 && _queue.index < _queue.ids.length) {
    _queue.ids.splice(_queue.index, 1);
    // index now points to the next item (or past end)
    if (_queue.index >= _queue.ids.length) {
      _queue.index = _queue.ids.length - 1;
    }
  }
  const id = currentDocId();
  _queue.prefetched = null;
  prefetchNext();
  notify();
  return id;
}

export function getStreakCount(): number {
  return _queue.streakCount;
}

export function resetStreak(): void {
  _queue.streakCount = 0;
  notify();
}

async function prefetchNext(): Promise<void> {
  const nextId = nextDocId();
  if (!nextId) {
    _queue.prefetched = null;
    return;
  }
  try {
    _queue.prefetched = await docsApi.get(nextId);
  } catch {
    _queue.prefetched = null;
  }
}

export function getQueue(): ReviewQueue {
  return { ..._queue };
}
