#!/usr/bin/env bash
# Thin shim for muscle-memory parity with the original harness; the real
# orchestrator is run.py (Python 3, stdlib only).
exec python3 "$(dirname "$0")/run.py" "$@"
