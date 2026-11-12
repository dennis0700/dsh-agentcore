"""Scan the agent container for the model API key. Runs inside the container.

Reads the key from the container's own LLM_API_KEY so it never appears on a
command line. Exit 0: not found (or no real key set). Exit 1: found.
"""
import os
import sys

key = os.environ.get("LLM_API_KEY", "")
if len(key) < 8 or key == "unused":
    print("SKIP no API key set")
    sys.exit(0)

needle = key.encode()
hits = []
for root in ("/home/app", "/mnt/workspace", "/app", "/tmp"):
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(directory, name)
            try:
                with open(path, "rb") as f:
                    if needle in f.read():
                        hits.append(path)
            except OSError:
                continue

print("FOUND in " + ", ".join(hits) if hits else "CLEAN")
sys.exit(1 if hits else 0)
