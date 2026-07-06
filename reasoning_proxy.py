#!/usr/bin/env python3
"""Tiny reverse proxy that sets GLM's reasoning tier, then forwards to $MODAL_ENDPOINT.

opencode can't add non-standard request-body fields, but GLM-5.2 on SGLang honours
`chat_template_kwargs`. This proxy injects the tier for one config and forwards. Run ONE instance
per tier (own port) so max/high/off can run concurrently against the same endpoint:

    REASONING=off  NOTHINK_PORT=8899 python3 reasoning_proxy.py   # enable_thinking=false
    REASONING=high NOTHINK_PORT=8898 python3 reasoning_proxy.py   # reasoning_effort=high
    # (max = default; hit the endpoint directly, no proxy)

Measured on a trivial prompt: default ~446 output tokens, high ~248 (~45% fewer), off ~5 — same
answer. Being able to dial this per request at all is a self-host advantage; run_bench starts/stops
these automatically for modal-nothink/ and modal-high/ arms.
"""
import http.server
import json
import os
import urllib.error
import urllib.request

MODAL = os.environ["MODAL_ENDPOINT"]
BASE = MODAL[:-3] if MODAL.endswith("/v1") else MODAL   # strip trailing /v1; the path already carries it
PORT = int(os.environ.get("NOTHINK_PORT", "8899"))
MODE = os.environ.get("REASONING", "off").lower()       # off | high (| anything else = passthrough)


def _inject(obj):
    ck = obj.setdefault("chat_template_kwargs", {})
    if MODE == "off":
        ck.setdefault("enable_thinking", False)
    elif MODE == "high":
        ck.setdefault("reasoning_effort", "high")
    return obj


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else None
        if body and "chat/completions" in self.path and MODE in ("off", "high"):
            try:
                body = json.dumps(_inject(json.loads(body))).encode()
            except Exception:
                pass
        req = urllib.request.Request(BASE + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                req.add_header(k, v)
        if body is not None:
            req.add_header("Content-Length", str(len(body)))
        try:
            r = urllib.request.urlopen(req, timeout=900)
            data, code, ct = r.read(), r.getcode(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            data, code, ct = e.read(), e.code, e.headers.get("Content-Type", "application/json")
        except Exception as e:
            data, code, ct = str(e).encode(), 502, "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"reasoning proxy [{MODE}] on 127.0.0.1:{PORT} -> {BASE}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
