"""Tiny local viewer: stdlib HTTP server + one HTML page that polls a JSON API."""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import urlparse

from .store import Store


def serve(store: Store, port: int = 8765, open_browser: bool = True) -> int:
    lock = threading.Lock()
    page = files("valet").joinpath("viewer.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            with lock:
                if path == "/api/runs":
                    return self._json({"runs": store.list_runs(200), "pending": store.pending_notes()})
                if path.startswith("/api/runs/"):
                    try:
                        run = store.get_run(int(path.rsplit("/", 1)[1]))
                    except ValueError:
                        run = None
                    return self._json(run) if run else self._json({"error": "not found"}, 404)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            with lock:
                if path == "/api/notes" and str(data.get("text", "")).strip():
                    return self._json({"id": store.add_note(data["text"].strip())})
                if path == "/api/notes/delete" and "id" in data:
                    store.delete_note(int(data["id"]))
                    return self._json({"ok": True})
            self._json({"error": "bad request"}, 400)

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"valet viewer on {url}  (recording to {store.home})  ctrl-c to stop")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
