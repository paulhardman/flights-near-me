---
name: use-local-tmp-folder
description: "Put scratch files/test scripts in ./tmp inside the project, never /tmp"
metadata:
  node_type: memory
  type: feedback
  originSessionId: c7e424df-7e0d-490a-9a0a-97790ae4850a
  modified: 2026-10-06T11:04:50.038Z
---

Use `tmp/` inside the working folder for scratch scripts, screenshots, caches — not system /tmp.

**Why:** writing to /tmp triggers permission prompts the user has to keep approving.
**How to apply:** any throwaway test harness or output file goes in ./tmp; test scripts snap.py/synth.py already live there.
