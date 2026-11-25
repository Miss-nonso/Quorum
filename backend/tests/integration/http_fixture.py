"""A real local HTTP server for http_fetch tests (no mocked network)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from quorum.samples import DEMO_DATASET

BAD_ORDERS = [{"id": "ord-x", "customer": "Eve", "total": -1, "status": "stolen"}]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:  # keep test output clean
        return

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        match self.path:
            case "/orders":
                self._send(200, json.dumps(DEMO_DATASET).encode())
            case "/bad-orders":
                self._send(200, json.dumps(BAD_ORDERS).encode())
            case "/text":
                self._send(200, b"hello", "text/plain; charset=utf-8")
            case "/big":
                self._send(200, b"[" + b"1," * 400_000 + b"1]")
            case "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://example.com/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            case "/boom":
                self._send(503, b'{"error": "down"}')
            case _:
                self._send(404, b"{}")


@contextmanager
def local_http_server() -> Iterator[str]:
    """Serve on 127.0.0.1 with an OS-assigned port; yield the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
