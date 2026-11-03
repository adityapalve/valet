"""Local viewer: a stdlib HTTP server plus one HTML page that polls a JSON API.

Lifecycle, designed so a viewer can never get stuck or block anything:
- `valet ui` starts the server as a detached background process and returns at once.
  Running it again finds the viewer already serving this project and just prints its URL.
- `valet ui --stop` asks the server to shut itself down over HTTP (authenticated with a
  token from .valet/ui.json). That works even when the process was started in a context
  you are not allowed to kill, e.g. by an agent's sandboxed terminal.
- The server exits by itself after `--idle` minutes without requests. An open viewer tab
  polls every 1.5s, so this only happens once nobody is looking.
- The server only answers requests addressed to 127.0.0.1/localhost, and POSTs must come
  from its own page, so other websites can't post "notes" for the agent.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

from .store import Store

UI_FILE = "ui.json"
# Bundled, served from the package so the viewer works offline (no CDN; corp networks often block them).
STATIC = {"diffs.js": "text/javascript; charset=utf-8", "THIRD_PARTY_LICENSES.txt": "text/plain; charset=utf-8"}
DEFAULT_IDLE_MINUTES = 30.0
_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # corp proxies: never for localhost


# ---- talking to a running viewer -------------------------------------------------------

def read_info(home: Path) -> dict | None:
    try:
        return json.loads((home / UI_FILE).read_text())
    except (OSError, ValueError):
        return None


def ping(info: dict | None, home: Path, timeout: float = 1.5) -> bool:
    """True if the viewer described by `info` is up and serving this project's .valet."""
    if not info:
        return False
    try:
        with _no_proxy.open(f"http://127.0.0.1:{info['port']}/api/ping", timeout=timeout) as r:
            data = json.load(r)
        return Path(data.get("home", "")) == home.resolve()
    except Exception:
        return False


def _post(info: dict, path: str, payload: dict, timeout: float = 3.0) -> bool:
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}{path}", data=json.dumps(payload).encode(),
        method="POST", headers={"Content-Type": "application/json", "X-Valet": "1"})
    try:
        with _no_proxy.open(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start(home: Path, port: int, open_browser: bool, idle: float) -> int:
    info = read_info(home)
    if ping(info, home):
        url = f"http://127.0.0.1:{info['port']}/"
        print(f"valet viewer already running on {url} (pid {info['pid']}); stop it with: valet ui --stop")
        if open_browser:
            webbrowser.open(url)
        return 0

    log = home / "ui.log"
    pkg_root = str(Path(__file__).resolve().parent.parent)
    boot = "import sys; sys.path.insert(0, sys.argv[1]); from valet.ui import _serve_main; _serve_main(sys.argv[2:])"
    cmd = [sys.executable, "-c", boot, pkg_root, str(home), str(port), str(idle)]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
    else:
        kwargs["start_new_session"] = True
    with open(log, "ab") as fh:
        proc = subprocess.Popen(cmd, stdout=fh, stderr=fh, **kwargs)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        info = read_info(home)
        if info and info.get("pid") == proc.pid and ping(info, home):
            url = f"http://127.0.0.1:{info['port']}/"
            print(f"valet viewer running on {url} (pid {proc.pid}) in the background.")
            print(f"It stops by itself after {idle:g} idle minutes; stop it now with: valet ui --stop")
            if open_browser:
                webbrowser.open(url)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    print(f"valet: the viewer did not start; see {log}", file=sys.stderr)
    try:
        print("\n".join(log.read_text(errors="replace").splitlines()[-8:]), file=sys.stderr)
    except OSError:
        pass
    return 1


def stop(home: Path) -> int:
    info = read_info(home)
    if not info:
        print("no valet viewer is recorded for this project")
        return 0
    if ping(info, home) and _post(info, "/api/shutdown", {"token": info.get("token")}):
        for _ in range(50):
            if not ping(info, home, timeout=0.3):
                break
            time.sleep(0.1)
        print(f"stopped the valet viewer (pid {info['pid']})")
    elif _pid_alive(info.get("pid")):
        try:
            os.kill(info["pid"], signal.SIGTERM)
            print(f"stopped the valet viewer process {info['pid']}")
        except OSError as e:
            print(f"valet: could not stop process {info['pid']}: {e}", file=sys.stderr)
            return 1
    else:
        print("the recorded valet viewer is not running")
    (home / UI_FILE).unlink(missing_ok=True)
    return 0


def status(home: Path) -> int:
    info = read_info(home)
    if ping(info, home):
        print(f"valet viewer running on http://127.0.0.1:{info['port']}/ (pid {info['pid']})")
        return 0
    print("valet viewer is not running")
    return 1


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---- the server ------------------------------------------------------------------------

def _serve_main(argv: list[str]) -> None:
    """Entry point of the detached background process."""
    home, port, idle = Path(argv[0]), int(argv[1]), float(argv[2])
    sys.exit(serve(Store(home), port, open_browser=False, idle=idle))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server bind a port that is already in use.
    allow_reuse_address = os.name != "nt"


def serve(store: Store, port: int = 8765, open_browser: bool = True,
          idle: float = DEFAULT_IDLE_MINUTES) -> int:
    page = files("valet").joinpath("viewer.html").read_bytes()
    token = secrets.token_urlsafe(24)
    last_seen = [time.monotonic()]

    httpd = None
    for p in range(port, port + 20):  # the default port may be taken, e.g. by an old viewer
        try:
            httpd = _Server(("127.0.0.1", p), None)
            break
        except OSError:
            continue
    if httpd is None:
        print(f"valet: no free port in {port}-{port + 19}", file=sys.stderr)
        return 1
    port = httpd.server_address[1]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status=200):
            self._send(json.dumps(obj).encode(), "application/json", status)

        def _host_ok(self) -> bool:  # blocks DNS-rebinding attacks
            return self.headers.get("Host", "") in hosts

        def do_GET(self):
            last_seen[0] = time.monotonic()
            if not self._host_ok():
                return self._json({"error": "forbidden"}, 403)
            path = urlparse(self.path).path
            if path == "/":
                return self._send(page, "text/html; charset=utf-8")
            if path.startswith("/static/") and path[8:] in STATIC:
                return self._send(files("valet").joinpath("static", path[8:]).read_bytes(), STATIC[path[8:]])
            if path == "/api/ping":
                return self._json({"home": str(store.home.resolve()), "pid": os.getpid()})
            try:
                if path == "/api/runs":
                    return self._json({"runs": store.list_runs(200), "pending": store.pending_notes()})
                if path.startswith("/api/runs/"):
                    try:
                        run = store.get_run(int(path.rsplit("/", 1)[1]))
                    except ValueError:
                        run = None
                    return self._json(run) if run else self._json({"error": "not found"}, 404)
            except Exception as e:  # e.g. the database is briefly busy: report, don't die
                return self._json({"error": f"{type(e).__name__}: {e}"}, 503)
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            last_seen[0] = time.monotonic()
            # Only our own page (or the valet CLI) may post: a custom header can't be sent
            # cross-origin without a CORS preflight, which this server never approves.
            origin = self.headers.get("Origin")
            if not self._host_ok() or self.headers.get("X-Valet") != "1" or (origin and origin not in origins):
                return self._json({"error": "forbidden"}, 403)
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            if path == "/api/shutdown":
                if not secrets.compare_digest(str(data.get("token", "")), token):
                    return self._json({"error": "forbidden"}, 403)
                self._json({"ok": True})
                threading.Thread(target=httpd.shutdown, daemon=True).start()
                return
            try:
                if path == "/api/notes" and str(data.get("text", "")).strip():
                    return self._json({"id": store.add_note(data["text"].strip())})
                if path == "/api/notes/delete" and "id" in data:
                    store.delete_note(int(data["id"]))
                    return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": f"{type(e).__name__}: {e}"}, 503)
            self._json({"error": "bad request"}, 400)

    httpd.RequestHandlerClass = Handler

    info_path = store.home / UI_FILE
    info_path.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token,
                                     "home": str(store.home.resolve()), "started": time.time()}))

    def watchdog():
        limit = idle * 60
        while True:
            time.sleep(max(0.2, min(30.0, limit / 4)))
            if time.monotonic() - last_seen[0] > limit:
                print("valet viewer: idle timeout, shutting down", flush=True)
                httpd.shutdown()
                return

    if idle > 0:
        threading.Thread(target=watchdog, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print(f"valet viewer on {url}  (recording to {store.home})", flush=True)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        info = read_info(store.home)
        if info and info.get("pid") == os.getpid():
            info_path.unlink(missing_ok=True)
    return 0
