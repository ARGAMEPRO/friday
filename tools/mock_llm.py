"""Tiny mock of the Ollama HTTP API. Used only for local smoke tests.

    python tools/mock_llm.py 11500
"""
from __future__ import annotations

import json
import random
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPLIES = [
    "Мок-модель на связи, босс. Ты сказал: «{text}». Живой режим работает.",
    "Слышу тебя, босс. «{text}» — интересный поворот. Даже для меня.",
    "Принято: {text}. Шутку приберегу до следующего раза.",
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._json({"models": [{"name": "mock-friday:latest"}]})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/api/chat"):
            messages = payload.get("messages") or []
            user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
            self._json({"message": {"role": "assistant", "content": random.choice(REPLIES).format(text=user[:80])}})
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 11500
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
