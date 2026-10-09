"""Task 22 local HTTP receiver. No Mattermost or production credentials."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


records = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        with lock:
            data = json.dumps(records if self.path == "/records" else {"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        with lock:
            records.append({"path": self.path, "body": data,
                            "event_id": self.headers.get("X-Keep-Event-ID")})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
