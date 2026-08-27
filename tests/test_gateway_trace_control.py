from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telos.gateway.control import post_trace_batch


def test_post_trace_batch_sends_bearer_and_json() -> None:
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            received["body"] = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"accepted":1}')

        def log_message(self, *args) -> None:  # type: ignore[no-untyped-def]
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        result = post_trace_batch(
            "127.0.0.1",
            server.server_port,
            {"schema_version": 1, "operations": []},
            token="secret",
        )
    finally:
        thread.join(timeout=2)
        server.server_close()

    assert result == {"accepted": 1}
    assert received == {
        "path": "/__telos/tracing/v1/batch",
        "authorization": "Bearer secret",
        "body": {"schema_version": 1, "operations": []},
    }
