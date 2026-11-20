"""Serve Bifrost's /metrics to the OTel collector without sharing the admin password.

Bifrost's management plane needs Basic auth. The collector, in another
namespace, scrapes /metrics. Giving the collector the admin password would put
the credential outside namespace `bifrost`, so this proxy holds it and serves
only `GET /metrics`. A NetworkPolicy limits who can reach the proxy.
"""
from __future__ import annotations

import base64
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("BIFROST_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))


def auth_header() -> str:
    raw = f"{os.environ['BIFROST_ADMIN_USERNAME']}:{os.environ['BIFROST_ADMIN_PASSWORD']}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        req = urllib.request.Request(UPSTREAM + "/metrics", headers={"Authorization": auth_header()})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
                ctype = resp.headers.get("Content-Type", "text/plain; version=0.0.4")
        except urllib.error.HTTPError as exc:
            self.send_error(502, f"upstream returned {exc.code}")
            return
        except (urllib.error.URLError, TimeoutError, OSError):
            self.send_error(502, "upstream unreachable")
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_error(405)

    def log_message(self, fmt, *args):  # no request line logging
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
