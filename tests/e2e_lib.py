#!/usr/bin/env python3
"""Host-side helpers for tests/local_e2e.sh. Python 3.8+, standard library only.

  payload PROMPT [true|false]      print an /invocations request body
  expect FILE key=value...         the final/error event matches every pair
  has FILE key=value...            some event in FILE matches every pair
  contains FILE KEY TEXT           the final/error event's KEY contains TEXT
  request-shape FILE               mock request log matches what compatible gateways accept
  header FILE VALUE                every tool-bearing mock request carried X-E2E-Run: VALUE
  last-request-contains FILE TEXT  the last mock request's messages mention TEXT

FILE is an /invocations response: one JSON object, or an SSE stream of them.
Non-string values compare by their JSON spelling, e.g. is_error=false.
"""
import json
import sys


def events(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("data: "):
                line = line[6:]
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def final(path):
    for event in reversed(events(path)):
        if event.get("type") in ("final", "error"):
            return event
    return {}


def spelled(value):
    return value if isinstance(value, str) else json.dumps(value)


def matches(event, pairs):
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key not in event or spelled(event[key]) != value:
            return False
    return True


def requests(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def request_shape(path):
    tool_requests = [r for r in requests(path) if r["body"].get("tools")]
    if not tool_requests:
        print("no tool-bearing requests were recorded")
        return 1
    for record in tool_requests:
        body = record["body"]
        problems = []
        if "store" in body:
            problems.append("sends 'store'")
        if "max_completion_tokens" in body:
            problems.append("sends 'max_completion_tokens'")
        if any(m.get("role") == "developer" for m in body.get("messages", [])):
            problems.append("uses the 'developer' role")
        if record.get("auth") != "Bearer <redacted>":
            problems.append("no bearer token")
        if not body.get("stream"):
            problems.append("not streaming")
        if problems:
            print("; ".join(problems))
            return 1
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "payload":
        body = {"prompt": args[0]}
        if len(args) > 1:
            body["stream"] = args[1] == "true"
        print(json.dumps(body, ensure_ascii=False))
        return 0
    if cmd == "expect":
        return 0 if matches(final(args[0]), args[1:]) else 1
    if cmd == "has":
        return 0 if any(matches(e, args[1:]) for e in events(args[0])) else 1
    if cmd == "contains":
        return 0 if args[2] in spelled(final(args[0]).get(args[1], "")) else 1
    if cmd == "request-shape":
        return request_shape(args[0])
    if cmd == "header":
        tool_requests = [r for r in requests(args[0]) if r["body"].get("tools")]
        return 0 if tool_requests and all(r.get("x_e2e_run") == args[1] for r in tool_requests) else 1
    if cmd == "last-request-contains":
        recorded = requests(args[0])
        last = json.dumps(recorded[-1]["body"].get("messages", []), ensure_ascii=False) if recorded else ""
        return 0 if args[1] in last else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
