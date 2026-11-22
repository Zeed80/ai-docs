#!/usr/bin/env python3
"""W7: a search engine inside the eval world.

SearXNG-compatible JSON (GET /search?q=…&format=json). The index is the
world's own test sites, so a query only finds pages that really exist there;
results are ranked by whether the query's words appear in the page text.
Without this every query returned the whole world and the agent read pages
about a different model.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent

PAGES = [
    ("http://eval-sites/pump/index.html", "pump/index.html"),
    ("http://eval-sites/pump/search.html", "pump/search.html"),
    ("http://eval-sites/pump/km-e-50.html", "pump/km-e-50.html"),
    ("http://eval-sites/pump/km-e-80.html", "pump/km-e-80.html"),
    ("http://eval-sites/pump/contact.html", "pump/contact.html"),
    ("http://eval-sites/pump/files/km-e-50-passport.pdf", "pump/files/km-e-50-passport.txt"),
    ("http://eval-sites/dealer/km-e-50.html", "dealer/km-e-50.html"),
    ("http://eval-sites/guard/price.html", "guard/price.txt"),
]


def plain(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def load():
    index = []
    for url, rel in PAGES:
        path = ROOT / rel
        if not path.exists():
            continue
        text = plain(path.read_text(encoding="utf-8"))
        title = text[:120]
        index.append({"url": url, "title": title, "text": text.lower()})
    return index


INDEX = load()


def terms(query: str):
    # Model designations split into their numbers too: "50-32-125" → 50, 32, 125.
    words = re.findall(r"[0-9a-zA-Zа-яА-ЯёЁ]+", query.lower())
    return [w for w in words if len(w) > 1 and w not in {"pdf", "site", "or", "и", "в", "на"}]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        params = parse_qs(urlparse(self.path).query)
        query = (params.get("q") or [""])[0]
        wanted = terms(query)
        scored = []
        for page in INDEX:
            hits = sum(1 for w in wanted if w in page["text"])
            if wanted and not hits:
                continue
            score = hits / max(1, len(wanted))
            if score < 0.4:
                continue
            scored.append((score, page))
        scored.sort(key=lambda item: -item[0])
        results = [
            {
                "url": page["url"],
                "title": page["title"],
                "content": page["text"][:300],
                "engine": "eval-search",
                "score": round(score, 3),
            }
            for score, page in scored[:10]
        ]
        body = json.dumps(
            {"query": query, "number_of_results": len(results), "results": results},
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
