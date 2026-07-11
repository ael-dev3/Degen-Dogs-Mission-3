#!/usr/bin/env python3
"""Regression tests for the Mission 3 local runner health watchdog."""
from __future__ import annotations

import fcntl
import importlib.util
import os
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("degen_dogs_runner_health.py")
spec = importlib.util.spec_from_file_location("degen_dogs_runner_health", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load {SCRIPT}")
health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = health
spec.loader.exec_module(health)


def test_refresh_lock_detection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "refresh.lock"
        setattr(health, "REFRESH_LOCK_PATH", lock_path)

        # A stale/unlocked lock file must not suppress a real dirty-worktree alert.
        lock_path.touch()
        assert health.refresh_is_active() is False

        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert health.refresh_is_active() is True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert health.refresh_is_active() is False


if __name__ == "__main__":
    test_refresh_lock_detection()
    print("degen dogs runner health tests passed")
