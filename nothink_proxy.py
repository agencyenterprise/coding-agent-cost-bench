#!/usr/bin/env python3
"""Tiny reverse proxy that turns GLM's reasoning OFF.

opencode can't add non-standard request-body fields, but GLM-5.2 on SGLang honours
`chat_template_kwargs: {enable_thinking: false}` (verified: it drops the reasoning trace,
~26x fewer output tokens for the same answer). This proxy injects that field into every
`/chat/completions` body and forwards to $MODAL_ENDPOINT — so a 'GLM thinking-off' arm can run
through the normal harness. (Being able to do this at all is a self-host advantage: a closed API
gives you no such knob, and this is where you'd later auto-select effort per task.)

    MODAL_ENDPOINT=... MODAL_KEY=... MODAL_SECRET=... python3 nothink_proxy.py
    # listens on 127.0.0.1:$NOTHINK_PORT (default 8899); point a provider baseURL at http://127.0.0.1:8899/v1

run_bench.sh starts/stops this automatically when a `modal-nothink/...` model is selected.
"""
import http.server
import json
import os
import urllib.error
import urllib.request

MODAL = os.environ["MODAL_ENDPOINT"]
BASE = MODAL[:-3] if MODAL.endswith("/v1") else MODAL   # strip trailing /v1; the path already carries it
PORT = int(os.environ.get("NOTHINK_PORT", "8899"))


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
        if body and "chat/completions" in self.path:      # inject the reasoning-off flag
            try:
                obj = json.loads(body)
                obj.setdefault("chat_template_kwargs", {}).setdefault("enable_thinking", False)
                body = json.dumps(obj).encode()
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
        except Exception as e:                              # upstream unreachable, etc.
            data, code, ct = str(e).encode(), 502, "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"nothink proxy on 127.0.0.1:{PORT} -> {BASE}  (injecting enable_thinking=false)", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
