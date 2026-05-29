#!/usr/bin/env python3
"""CLI alias for the precise Mission 3 onchain activity watcher.

The implementation lives in watch_mission3_auction.py to preserve the existing
watch:auction entrypoint. New automation should prefer this file/name.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from watch_mission3_auction import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
