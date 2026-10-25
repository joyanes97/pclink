"""A fake llama-server used to exercise the streaming proxy offline.

It speaks just enough of the OpenAI chat-completions SSE protocol to validate
frame pass-through, termination, and upstream error mapping.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

DEFAULT_FRAMES = [
    b'data: {"id":"1","choices":[{"delta":{"content":"Hel"}}]}\n\n',
    b'data: {"id":"1","choices":[{"delta":{"content":"lo"}}]}\n\n',
    b'data: {"id":"1","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
    b"data: [DONE]\n\n",
]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": body,
            }
        )

        if self.server.force_status:
            payload = json.dumps({"error": {"message": "model exploded"}}).encode()
            self.send_response(self.server.force_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for frame in self.server.frames:
            self.wfile.write(frame)
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, *args):  # silence the test output
        pass


@pytest.fixture
def fake_llama_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.requests = []
    server.frames = list(DEFAULT_FRAMES)
    server.force_status = None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
