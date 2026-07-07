#!/usr/bin/env python3
"""Concurrent smoke/load probe for the studio queue API.

Default mode is read-only and safe for production validation:

    API_URL=https://localhost API_KEY=... python3 scripts/studio_queue_load_smoke.py

Real task enqueueing is opt-in:

    ... python3 scripts/studio_queue_load_smoke.py --enqueue --requests 20
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import ssl
import time
import urllib.error
import urllib.request


def _request(method: str, url: str, api_key: str, payload: dict | None = None) -> tuple[int, str]:
    data = None
    headers = {"X-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl._create_unverified_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:  # noqa: S310
            return resp.status, resp.read(4000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(4000).decode("utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--enqueue", action="store_true")
    args = parser.parse_args()

    api_url = os.environ.get("API_URL", "https://localhost").rstrip("/")
    api_key = os.environ.get("API_KEY") or os.environ.get("AGENT_SERVICE_KEY")
    if not api_key:
        raise SystemExit("Set API_KEY or AGENT_SERVICE_KEY")

    def one(idx: int) -> tuple[int, str]:
        if args.enqueue:
            return _request(
                "POST",
                f"{api_url}/api/image-gen/generate",
                api_key,
                {
                    "operation": "generate",
                    "prompt": f"queue load smoke {idx}",
                    "params": {"seed": idx},
                },
            )
        target = "/api/studio/queue/stats" if idx % 2 == 0 else "/api/studio/queue?limit=20"
        return _request("GET", f"{api_url}{target}", api_key)

    started = time.monotonic()
    counts: dict[int, int] = {}
    samples: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for status, body in pool.map(one, range(args.requests)):
            counts[status] = counts.get(status, 0) + 1
            if status >= 400 and len(samples) < 5:
                samples.append(body[:500])

    elapsed = time.monotonic() - started
    print(json.dumps({
        "requests": args.requests,
        "concurrency": args.concurrency,
        "enqueue": args.enqueue,
        "elapsed_seconds": round(elapsed, 3),
        "status_counts": counts,
        "error_samples": samples,
    }, ensure_ascii=False, indent=2))
    accepted = {200, 201}
    if args.enqueue:
        # Backpressure is a valid enqueue outcome under load; 404/5xx are not.
        accepted.update({429, 503})
    return 0 if all(code in accepted for code in counts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
