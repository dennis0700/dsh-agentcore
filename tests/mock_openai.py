"""Scripted OpenAI Chat Completions server for local tests. No real model behind it.

- A request whose last message is not a tool result gets a streamed `bash`
  tool call that writes mock.txt in the agent's working directory.
- A request whose last message is a tool result gets a streamed text reply.
- A request without tools (an auxiliary call) gets a short text reply.

Environment:
  MOCK_PORT           listen port (default 9000)
  MOCK_LOG            request log path (default /tmp/mock-requests.jsonl)
  MOCK_DELAY_SECONDS  delay before each reply (default 0), used to test concurrency

Each request body is appended to MOCK_LOG with the bearer token redacted and
the X-E2E-Run header (sent by tests/local_e2e.sh via LLM_HEADERS) recorded.
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_PORT", "9000"))
LOG = os.environ.get("MOCK_LOG", "/tmp/mock-requests.jsonl")
DELAY = float(os.environ.get("MOCK_DELAY_SECONDS", "0"))
MODEL_ID = "mock-model"


def sse(handler, obj):
    handler.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
    handler.wfile.flush()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": MODEL_ID, "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        auth = self.headers.get("Authorization") or ""
        record = {"auth": "Bearer <redacted>" if auth.startswith("Bearer ") else "",
                  "x_e2e_run": self.headers.get("X-E2E-Run"), "body": req}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if DELAY:
            time.sleep(DELAY)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": req.get("model", MODEL_ID)}
        messages = req.get("messages") or [{}]
        if not req.get("tools") or messages[-1].get("role") == "tool":
            text = "Done: mock.txt written." if req.get("tools") else "ok"
            sse(self, {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}]})
            sse(self, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        else:
            args = json.dumps({"command": "echo mock-ok > mock.txt && cat mock.txt", "description": "write mock file"})
            call = {"index": 0, "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                    "function": {"name": "bash", "arguments": args}}
            sse(self, {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}}]})
            sse(self, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        sse(self, {**base, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self.wfile.write(b"data: [DONE]\n\n")


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
