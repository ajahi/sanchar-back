"""Stand-in for the two outside services the bot calls: Instagram Graph (send DM, profile) and Groq (LLM).

    python -m loadtest.mock_external            # listens on :9000
    LLM_LATENCY=2 GRAPH_LATENCY=0.3 PORT=9000   # env knobs (seconds; each call jitters 0.5x..1.5x)
    LLM_RPM=30 LLM_TPM=8000                     # Groq free-tier limits (0 = unlimited): over either -> 429

GET /stats counts what the app really sent; POST /reset zeroes it. Latency is the point: a mock that
answers instantly hides the held connections and queues that a 1-3s LLM call causes.
"""
import json
import os
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "9000"))
LLM_LATENCY = float(os.getenv("LLM_LATENCY", "2.0"))
GRAPH_LATENCY = float(os.getenv("GRAPH_LATENCY", "0.3"))

LLM_RPM = int(os.getenv("LLM_RPM", "0"))
LLM_TPM = int(os.getenv("LLM_TPM", "0"))

stats = {"dm_sent": 0, "profile_lookups": 0, "llm_calls": 0, "llm_429": 0}
lock = threading.Lock()
llm_window: list[tuple[float, int]] = []  # (time, est. tokens) of accepted calls in the last 60s


def llm_limited(tokens: int) -> bool:
    """Groq-style per-minute caps, rolling window. Over either cap -> caller answers 429."""
    now = time.time()
    with lock:
        llm_window[:] = [(t, n) for t, n in llm_window if now - t < 60]
        if (LLM_RPM and len(llm_window) >= LLM_RPM) or (LLM_TPM and sum(n for _, n in llm_window) + tokens > LLM_TPM):
            stats["llm_429"] += 1
            return True
        llm_window.append((now, tokens))
        return False


def bump(key: str) -> None:
    with lock:
        stats[key] += 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, like the real APIs

    def log_message(self, *_):  # silence per-request logging; it would dominate at load
        pass

    def _send(self, body: dict, status: int = 200) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _sleep(self, base: float) -> None:
        time.sleep(base * random.uniform(0.5, 1.5))

    def do_GET(self):
        if self.path == "/stats":
            with lock:
                return self._send(dict(stats))
        bump("profile_lookups")  # GET /{version}/{igsid}?fields=name,username
        self._sleep(GRAPH_LATENCY)
        self._send({"name": "Load Test Customer", "username": "lt_customer"})

    def do_POST(self):
        body = self._body()
        if self.path == "/reset":
            with lock:
                for k in stats:
                    stats[k] = 0
                llm_window.clear()
            return self._send({"ok": True})
        if self.path.endswith("/chat/completions"):  # Groq
            if llm_limited(len(body) // 4 + 150):  # ~4 chars/token + a short completion
                return self._send({"error": {"message": "rate limit reached", "type": "tokens"}}, 429)
            bump("llm_calls")
            self._sleep(LLM_LATENCY)
            return self._send(
                {
                    "choices": [{"message": {"content": "Simple Plain Chura ko price Rs 300 per set ho."}}],
                    "usage": {"total_tokens": 100},
                }
            )
        bump("dm_sent")  # POST /{version}/{ig_id}/messages
        self._sleep(GRAPH_LATENCY)
        self._send({"recipient_id": "x", "message_id": f"mock_{uuid.uuid4().hex}"})


if __name__ == "__main__":
    print(f"mock Graph+Groq on :{PORT}  (LLM {LLM_LATENCY}s, Graph {GRAPH_LATENCY}s)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
