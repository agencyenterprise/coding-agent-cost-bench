#!/usr/bin/env python3
"""Tiny reverse proxy that sets GLM's reasoning tier, then forwards to the GLM endpoint.

opencode can't add non-standard request-body fields, but GLM-5.2 on SGLang honours
`chat_template_kwargs`. This proxy injects the tier for one config and forwards. Run ONE instance
per tier (own port) so max/high/off can run side by side against the same endpoint:

    python3 reasoning_proxy.py --reasoning off  --port 8899   # enable_thinking=false
    python3 reasoning_proxy.py --reasoning high --port 8898   # reasoning_effort=high
    # max = default; hit the endpoint directly (no proxy)

Upstream defaults to $MODAL_ENDPOINT. Measured on a trivial prompt: default ~446 output tokens,
high ~248 (~45% fewer), off ~5 — same answer. bench.sh starts/stops these for modal-nothink/ and
modal-high/ arms.
"""
import argparse
import http.server
import json
import os
import urllib.error
import urllib.request

ap = argparse.ArgumentParser(description="Inject a GLM reasoning tier and forward to the endpoint.")
ap.add_argument("--reasoning", default="off", choices=["off", "high", "max"],
                help="off = enable_thinking:false; high = reasoning_effort:high; max = passthrough")
ap.add_argument("--port", type=int, default=8899, help="localhost port to listen on")
ap.add_argument("--upstream", default=os.environ.get("MODAL_ENDPOINT", ""),
                help="GLM endpoint base URL (defaults to $MODAL_ENDPOINT)")
args = ap.parse_args()
if not args.upstream:
    raise SystemExit("no upstream: pass --upstream or set MODAL_ENDPOINT")

MODE = args.reasoning
PORT = args.port
BASE = args.upstream[:-3] if args.upstream.endswith("/v1") else args.upstream   # path already carries /v1


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
                print(f"[{MODE}] injected reasoning tier -> {self.path}", flush=True)
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
    # 0.0.0.0 (not 127.0.0.1): as a sibling container on the per-run bridge, this must be reachable
    # by NAME from the task containers (Q11), not just the loopback of its own netns.
    print(f"reasoning proxy [{MODE}] on 0.0.0.0:{PORT} -> {BASE}", flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
