"""A logging forwarder for OpenAI-compatible chat endpoints.

    python -m tools.llm_tap --upstream https://host/v1 --port 8731 --log runs/tap.jsonl

Point every role at http://127.0.0.1:8731/v1 and each request/reply pair is
appended to the log as one JSON line: who asked (guessed from the system
prompt's `# component:` header or the model name), the model, the messages,
the reply content, the usage block, the HTTP status and the latency. The
upstream's own `Authorization` header is passed through untouched.

WHY A PROXY AND NOT A LOG LINE. The memory-side calls go through the frozen
`memory_system/llm.py` from inside episode child processes; it cannot be
given a logger and it swallows every HTTP error into a cached None. Watching
the wire is the only way to see what those calls asked and what came back,
including the 429s that would otherwise read as silent fallbacks. Nothing
here changes a request or a reply.

Stdlib only; threaded so concurrent callers do not serialise on the tap.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_COMPONENT = re.compile(r"#\s*component:\s*([a-z_]+)", re.IGNORECASE)
_LOCK = threading.Lock()

# Rate limits and transient server errors are retried; everything else is
# returned as-is. The ladder spans tens of seconds so a retry lands in a
# later rate-limit window rather than the same one.
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 599})
_RETRIES = 5
_BACKOFF_BASE_S = 2.0
_BACKOFF_MAX_S = 30.0


def _role_of(payload: dict) -> str:
    """The caller, as far as the request shows it."""
    # The judge sends one user message and no system message, so every
    # message's opening is checked, not only the system one.
    for m in payload.get("messages") or []:
        content = str(m.get("content") or "")
        hit = _COMPONENT.search(content[:400])
        if hit:
            return f"memory:{hit.group(1).lower()}"
        head = content[:160].lower()
        for needle, role in (("impartial evaluator", "judge"), ("multi-party assistant", "answerer"),
                             ("you are the reflector", "reflector"), ("you are the curator", "curator")):
            if needle in head:
                return role
    return "other"


class Tap(BaseHTTPRequestHandler):
    upstream = ""
    log_path = ""
    counters = {"requests": 0, "errors": 0, "status": {}}

    def log_message(self, *_: object) -> None:  # quiet: the JSONL is the log
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server's name
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            payload = {}
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() in ("authorization", "content-type", "accept")}
        req = urllib.request.Request(self.upstream.rstrip("/") + self.path.removeprefix("/v1"),
                                     data=body, method="POST", headers=headers)
        t0 = time.time()
        status, reply_body, attempts = 0, b"", 0
        # The frozen memory_system/llm.py has no retry: a 429 from the shared
        # endpoint becomes a cached None and a silent fallback. Measured on
        # the first full office run at three episode workers: 43 of 1,659
        # requests. Absorbing the rate limit here, with full-jitter backoff,
        # is the one place it can be done without touching the frozen file.
        for attempt in range(_RETRIES + 1):
            attempts = attempt + 1
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:
                    status, reply_body = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, reply_body = exc.code, exc.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                status, reply_body = 599, json.dumps({"tap_error": str(exc)}).encode()
            if status not in _RETRY_STATUS or attempt == _RETRIES:
                break
            time.sleep(random.uniform(0, min(_BACKOFF_MAX_S, _BACKOFF_BASE_S * 2 ** attempt)))
        latency = time.time() - t0
        try:
            reply = json.loads(reply_body or b"{}")
        except json.JSONDecodeError:
            reply = {"raw": reply_body[:2000].decode("utf-8", "replace")}
        choice = ((reply.get("choices") or [{}])[0].get("message") or {}) if isinstance(reply, dict) else {}
        row = {
            "ts": round(t0, 3), "latency_s": round(latency, 2), "status": status, "attempts": attempts,
            "role": _role_of(payload), "model": payload.get("model"),
            "reported_model": reply.get("model") if isinstance(reply, dict) else None,
            "thinking_kw": (payload.get("chat_template_kwargs") or {}).get("enable_thinking"),
            "max_tokens": payload.get("max_tokens"),
            "messages": payload.get("messages"),
            "content": choice.get("content"),
            "reasoning_chars": len(str(choice.get("reasoning_content") or choice.get("reasoning") or "")),
            "finish_reason": ((reply.get("choices") or [{}])[0].get("finish_reason")) if isinstance(reply, dict) else None,
            "usage": reply.get("usage") if isinstance(reply, dict) else None,
            "error": reply.get("error") if isinstance(reply, dict) and status >= 400 else None,
        }
        with _LOCK:
            self.counters["requests"] += 1
            self.counters["status"][str(status)] = self.counters["status"].get(str(status), 0) + 1
            if status >= 400:
                self.counters["errors"] += 1
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.send_response(status if status else 502)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply_body)))
        self.end_headers()
        self.wfile.write(reply_body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            req = urllib.request.Request(self.upstream.rstrip("/") + "/models",
                                         headers={"Authorization": self.headers.get("Authorization", "")})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body, status = resp.read(), resp.status
            except urllib.error.HTTPError as exc:
                body, status = exc.read(), exc.code
        else:
            body, status = json.dumps(self.counters).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--upstream", required=True, help="e.g. https://soclaas-api.comp.nus.edu.sg/v1")
    p.add_argument("--port", type=int, default=8731)
    p.add_argument("--log", required=True, help="JSONL file to append to")
    args = p.parse_args()
    Tap.upstream, Tap.log_path = args.upstream, args.log
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    print(f"llm_tap: 127.0.0.1:{args.port}/v1 -> {args.upstream}; log {args.log}; GET / for counters", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
