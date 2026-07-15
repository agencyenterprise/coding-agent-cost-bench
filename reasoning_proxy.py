#!/usr/bin/env python3
"""Reasoning-tier sidecar: injects GLM's `chat_template_kwargs`, then forwards to the GLM endpoint.

opencode can't add non-standard request-body fields (opencode #27462), but GLM-5.2 on SGLang honours
`chat_template_kwargs`. This proxy adds the tier and forwards. It fronts pier's egress: a task
container's opencode -> Squid (host allowlisted, port 80) -> this sidecar -> the Modal endpoint.

Two ways to pick the tier:

  * --router (the benchmark default): ONE instance serves every tier by URL path prefix, so all
    GLM setups share a single host:80 (pier's Squid only allows ports 80/443, so we can't give each
    tier its own port). opencode baseURL per setup:
        high     -> http://<host>/high/v1
        nothink  -> http://<host>/nothink/v1
    The prefix is stripped before forwarding; `default` (max) skips the proxy and hits the endpoint
    directly, so it needs no prefix here.

  * --reasoning {off,high,max}: a single fixed tier (one instance per tier, own port). Used for
    ad-hoc one-tier tests.

Upstream defaults to $MODAL_ENDPOINT. Responses are streamed through unbuffered so opencode's SSE
parser sees tokens incrementally. Measured on a trivial prompt: default ~446 output tokens, high
~248 (~45% fewer), off ~5 — same answer.

    python3 reasoning_proxy.py --router --port 80 --bind 0.0.0.0      # image sidecar (all tiers)
    python3 reasoning_proxy.py --reasoning high --port 8898           # single tier
"""
import argparse
import http.server
import json
import os
import sys
import urllib.error
import urllib.request

ap = argparse.ArgumentParser(description="Inject a GLM reasoning tier and forward to the endpoint.")
ap.add_argument("--router", action="store_true",
                help="pick the tier from the URL path prefix (/high, /nothink, /off); serves all tiers")
ap.add_argument("--reasoning", default="off", choices=["off", "high", "max"],
                help="fixed tier when --router is off: off=enable_thinking:false, high=reasoning_effort:high, max=passthrough")
ap.add_argument("--port", type=int, default=8899, help="port to listen on")
ap.add_argument("--bind", default="0.0.0.0", help="address to bind [%(default)s]")
ap.add_argument("--upstream", default=os.environ.get("MODAL_ENDPOINT", ""),
                help="GLM endpoint base URL (defaults to $MODAL_ENDPOINT)")
args = ap.parse_args()
if not args.upstream:
    raise SystemExit("no upstream: pass --upstream or set MODAL_ENDPOINT")

# BASE carries no trailing /v1 — the incoming request path already does (e.g. /v1/chat/completions).
BASE = args.upstream[:-3] if args.upstream.endswith("/v1") else args.upstream.rstrip("/")

# tier -> the chat_template_kwargs to merge in. "max"/None = passthrough (no injection).
_TIERS = {
    "off": {"enable_thinking": False},
    "nothink": {"enable_thinking": False},
    "high": {"reasoning_effort": "high"},
    "max": {},
    "default": {},
}


def _split_tier(path):
    """(tier, forward_path). In router mode the first path segment names the tier and is stripped;
    otherwise the fixed --reasoning tier applies and the path is forwarded unchanged."""
    if not args.router:
        return args.reasoning, path
    parts = path.split("/", 2)                      # "", "<tier>", "<rest>"
    if len(parts) >= 2 and parts[1] in _TIERS:
        rest = "/" + (parts[2] if len(parts) > 2 else "")
        return parts[1], rest
    return "max", path                              # unknown/absent prefix -> passthrough


def _inject(obj, tier):
    kw = _TIERS.get(tier) or {}
    if kw:
        ck = obj.setdefault("chat_template_kwargs", {})
        for k, v in kw.items():
            ck.setdefault(k, v)
    return obj


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def _forward(self):
        tier, fwd_path = _split_tier(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else None
        injected = False
        if body and "chat/completions" in fwd_path and tier in _TIERS and _TIERS[tier]:
            try:
                body = json.dumps(_inject(json.loads(body), tier)).encode()
                injected = True
            except Exception:
                pass
        sys.stdout.write(f"[proxy] {self.command} {self.path} tier={tier} injected={injected}\n")
        sys.stdout.flush()

        req = urllib.request.Request(BASE + fwd_path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "connection", "proxy-connection"):
                req.add_header(k, v)
        if body is not None:
            req.add_header("Content-Length", str(len(body)))

        try:
            r = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            r = e
        except Exception as e:
            msg = str(e).encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(msg)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(msg)
            return

        self.send_response(r.getcode() if hasattr(r, "getcode") else r.code)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/json"))
        self.send_header("Connection", "close")   # no Content-Length: stream until upstream closes
        self.end_headers()
        while True:
            chunk = r.read(8192)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
                self.wfile.flush()
            except Exception:
                break


if __name__ == "__main__":
    mode = "router" if args.router else f"fixed:{args.reasoning}"
    print(f"reasoning proxy [{mode}] on {args.bind}:{args.port} -> {BASE}", flush=True)
    http.server.ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()
