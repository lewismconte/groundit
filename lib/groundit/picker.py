"""The map picker: a tiny local web server that hands a site request back.

The picker UI runs in the user's own browser rather than inside Revit. The
alternative is embedding a map in WPF, which means either the IE11-era
WebBrowser control (where a modern map is painful) or shipping WebView2 DLLs
and loading them from IronPython. Neither is worth it when every machine
already has a good browser sitting right there. Same reasoning as Blendit:
bridge, do not embed.

Because the page is served by this server, the result POST is same-origin and
there is no CORS dance. The server binds to 127.0.0.1 only and requires a
random per-run token, so nothing else on the machine can drive it.

Runtime-agnostic on purpose - no Revit imports - so the handoff can be tested
headless. The waiting dialog lives in ui.py.
"""

import json
import os
import random
import socket
import threading
import time

try:                                    # Python 3
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:                     # Python 2 / IronPython
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer


def map_page_path():
    """Absolute path to map/index.html, relative to this file."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(here)), "map", "index.html")


class _Handler(BaseHTTPRequestHandler):

    # Silence the default stderr logging; a Revit user has no console to read.
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass                        # browser hung up; nothing to do

    def _token_ok(self):
        return self.server.token in self.path

    def do_GET(self):
        route = self.path.split("?")[0]

        if route in ("/", "/index.html"):
            try:
                with open(map_page_path(), "rb") as handle:
                    page = handle.read()
            except IOError:
                self._send(500, "map/index.html is missing from the extension.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, page, "text/html; charset=utf-8")
            return

        if route == "/api/config":
            self._send(200, json.dumps(self.server.config))
            return

        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        route = self.path.split("?")[0]
        if not self._token_ok():
            self._send(403, json.dumps({"error": "bad token"}))
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            self._send(400, json.dumps({"error": "unreadable body"}))
            return

        if route == "/api/submit":
            self.server.result = payload
        elif route == "/api/cancel":
            self.server.cancelled = True
        else:
            self._send(404, json.dumps({"error": "not found"}))
            return

        self._send(200, json.dumps({"ok": True}))
        self.server.done.set()


class _Server(HTTPServer):
    """Single-threaded on purpose.

    ThreadingMixIn would spawn a thread per request. Inside Revit that means
    IronPython threads doing socket work alongside the WPF dispatcher, which
    is a needless risk for a server that handles about four requests in its
    whole life. Requests are served one at a time, and the handler speaks
    HTTP/1.0 so connections close rather than being held open.
    """
    allow_reuse_address = True


class Picker(object):
    """A local map picker.

    Two ways to run it:

      pump()  - serve one request per call, on the caller's own thread. This
                is what Revit uses: no background thread at all.
      start() - serve on a background thread, for headless use and tests.
    """

    def __init__(self, config=None):
        self.token = "%016x" % random.getrandbits(64)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.token = self.token
        self._server.config = config or {}
        self._server.result = None
        self._server.cancelled = False
        self._server.done = threading.Event()
        self.port = self._server.server_address[1]
        self._thread = None

    @property
    def url(self):
        return "http://127.0.0.1:%d/?t=%s" % (self.port, self.token)

    def pump(self, timeout=0.2):
        """Serve at most one request, then return. -> poll status.

        The listening socket is bound from __init__, so a browser sent at the
        URL before the first pump() lands in the accept backlog rather than
        being refused. Nothing is lost by not having started yet.
        """
        self._server.timeout = timeout
        try:
            self._server.handle_request()
        except Exception:
            pass                        # a dropped connection is not our problem
        return self.poll()

    def start(self, verify_timeout=5.0):
        """Serve on a background thread. Headless and test use only.

        Does not return until the port actually answers: a successful bind is
        not proof the thread is serving, and handing a browser a URL that is
        not yet listening gives an error page and no second chance.
        """
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True      # never keep the host alive on exit
        self._thread.start()

        deadline = time.time() + verify_timeout
        while time.time() < deadline:
            try:
                probe = socket.create_connection(("127.0.0.1", self.port), 0.25)
                probe.close()
                return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError(
            "The Groundit map server did not start listening on port %d."
            % self.port)

    def poll(self):
        """-> "waiting" | "submitted" | "cancelled"."""
        if self._server.result is not None:
            return "submitted"
        if self._server.cancelled:
            return "cancelled"
        return "waiting"

    @property
    def result(self):
        return self._server.result

    def wait(self, timeout=None):
        """Block until the browser answers. -> request dict, or None."""
        self._server.done.wait(timeout)
        return self._server.result

    def stop(self):
        """Close the listener. Safe to call whichever way it was run.

        shutdown() may ONLY be called when serve_forever() is running: it
        blocks on an event that only serve_forever sets, so calling it after
        a pump()-only session hangs forever. In Revit that hang lands in the
        caller's finally block, freezing Revit immediately after a successful
        pick - the worst possible moment.
        """
        if self._thread is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._thread = None
        try:
            self._server.server_close()
        except Exception:
            pass


def open_in_browser(url):
    """Open the picker page. -> True on success.

    Uses the .NET-8-safe shell open: on Revit 2025+ a bare Process.Start with
    a URL throws, because UseShellExecute defaults to False on .NET Core.
    """
    from .shell import open_path
    return open_path(url)
