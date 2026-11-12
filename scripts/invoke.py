#!/usr/bin/env python3
"""Invoke the deployed runtime and print streamed events.

Examples:
  scripts/invoke.py "列出 project 目录" --session my-session-0000000000000001
  scripts/invoke.py "继续" --session my-session-0000000000000001
  scripts/invoke.py --stop --session my-session-0000000000000001
  scripts/invoke.py --smoke          # write file, stop session, resume, read it back
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid

import boto3


def runtime_arn(stack: str) -> str:
    out = subprocess.check_output([
        "aws", "cloudformation", "describe-stacks", "--stack-name", stack,
        "--query", "Stacks[0].Outputs[?OutputKey=='AgentRuntimeArn'].OutputValue", "--output", "text",
    ], text=True).strip()
    if not out or out == "None":
        sys.exit(f"no AgentRuntimeArn output on stack {stack}")
    return out


def iter_sse(body):
    buffer = b""
    for chunk in body.iter_chunks():
        buffer += chunk
        while b"\n\n" in buffer:
            frame, buffer = buffer.split(b"\n\n", 1)
            for line in frame.splitlines():
                if line.startswith(b"data: "):
                    yield json.loads(line[6:])
    if buffer.strip():
        text = buffer.decode()
        yield json.loads(text[6:] if text.startswith("data: ") else text)


def invoke(client, arn: str, session: str, prompt: str, quiet: bool = False) -> dict:
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        runtimeSessionId=session,
        contentType="application/json",
        accept="text/event-stream",
        payload=json.dumps({"prompt": prompt}).encode(),
    )
    final: dict = {}
    for event in iter_sse(resp["response"]):
        kind = event.get("type")
        if not quiet and kind != "heartbeat":
            print(json.dumps(event, ensure_ascii=False))
        if kind in ("final", "error") or "error" in event:
            final = event
    return final


def stop(client, arn: str, session: str) -> None:
    client.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=session)
    print(f"stopped session {session}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("prompt", nargs="?")
    p.add_argument("--session", default=None, help="runtime session id (>= 33 chars recommended)")
    p.add_argument("--stack", default=os.environ.get("RUNTIME_STACK", "dsh-agent-runtime"))
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "cn-north-1"))
    p.add_argument("--stop", action="store_true")
    p.add_argument("--smoke", action="store_true")
    args = p.parse_args()

    arn = runtime_arn(args.stack)
    client = boto3.client("bedrock-agentcore", region_name=args.region)
    session = args.session or f"dsh-{uuid.uuid4().hex}"
    print(f"session: {session}", file=sys.stderr)

    if args.stop:
        stop(client, arn, session)
        return
    if args.smoke:
        marker = uuid.uuid4().hex[:8]
        r1 = invoke(client, arn, session,
                    f"Create a file named smoke.txt in the current directory containing exactly '{marker}'. "
                    f"Then remember the word 'pelican'. Reply 'done'.")
        assert r1.get("type") == "final", r1
        stop(client, arn, session)
        time.sleep(15)
        # Separate turns: final_response only carries the last assistant message.
        r2 = invoke(client, arn, session,
                    "Without calling any tools, reply with exactly one line: code=<the word I asked you to remember>")
        r3 = invoke(client, arn, session, "Run this exact shell command and reply with only its output: cat smoke.txt")
        memory_ok = "pelican" in r2.get("final_response", "").lower()
        file_ok = marker in r3.get("final_response", "")
        ok = memory_ok and file_ok
        print(f"\nSMOKE {'PASS' if ok else 'FAIL'}: file={'ok' if file_ok else 'missing'} "
              f"memory={'ok' if memory_ok else 'missing'}")
        sys.exit(0 if ok else 1)
    if not args.prompt:
        p.error("prompt is required")
    result = invoke(client, arn, session, args.prompt)
    sys.exit(0 if result.get("type") == "final" else 1)


if __name__ == "__main__":
    main()
