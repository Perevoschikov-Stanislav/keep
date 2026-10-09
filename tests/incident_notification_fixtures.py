"""Two real loopback protocols for ready DTO delivery and verified recovery."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class NotificationReceiver:
    def __init__(self):
        self.events, self.posts, self.projection = [], {}, {}
        self.status = 200
        self.tamper_receipt = False
        receiver = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, data):
                payload = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                post = copy.deepcopy(receiver.posts.get(self.path.rsplit("/", 1)[-1]))
                if post and receiver.tamper_receipt:
                    post["channel_id"] = "unrelated-channel"
                self.respond(200 if post else 404, post or {})

            def accept(self, method):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                receiver.events.append({"method": method, "path": self.path, "body": body,
                    "event_id": self.headers.get("X-Keep-Event-ID"),
                    "delivery_id": self.headers.get("X-Keep-Delivery-ID"),
                    "authorization": self.headers.get("Authorization")})
                if self.path.startswith("/api/v4/posts"):
                    identifier = self.path.rsplit("/", 1)[-1] if method == "PUT" else str(len(receiver.posts) + 1).zfill(26)
                    if method == "PUT" and identifier not in receiver.posts:
                        self.respond(404, {})
                        return
                    receiver.posts[identifier] = {**body, "id": identifier}
                    self.respond(receiver.status, receiver.posts[identifier])
                else:
                    current = receiver.projection.get(body["incident_id"])
                    if current is None or body["projection_revision"] > current["projection_revision"]:
                        receiver.projection[body["incident_id"]] = body
                    self.respond(receiver.status, {})

            def do_POST(self):
                self.accept("POST")

            def do_PUT(self):
                self.accept("PUT")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def restart(self):
        self.server = ThreadingHTTPServer(self.server.server_address, self.server.RequestHandlerClass)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
