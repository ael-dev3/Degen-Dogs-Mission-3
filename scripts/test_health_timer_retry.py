#!/usr/bin/env python3
"""Opt-in, isolated systemd proof: failed health probes cannot exhaust retries.

Creates one randomly named transient timer/service and temporary evidence.
Never operates on a production unit. Requires root and systemd.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import uuid


def main() -> int:
    if sys.argv[1:] != ["--isolated-systemd"]:
        print("Use --isolated-systemd in a disposable Linux test environment.")
        return 64
    if os.name != "posix" or os.geteuid() != 0:
        raise RuntimeError("root in a systemd Linux test environment is required")
    unit = "degen-health-retry-test-" + uuid.uuid4().hex
    service = unit + ".service"
    timer = unit + ".timer"
    with tempfile.TemporaryDirectory(prefix="degen-health-retry-test-") as raw:
        probe = Path(raw) / "probe.py"
        evidence = Path(raw) / "attempts"
        probe.write_text(
            "import pathlib,sys,time\n"
            "p=pathlib.Path(sys.argv[1])\n"
            "n=int(p.read_text())+1 if p.exists() else 1\n"
            "p.write_text(str(n))\n"
            "time.sleep(0.5)\n"
            "sys.exit(1 if n<=6 else 0)\n",
            encoding="utf-8",
        )
        try:
            subprocess.run([
                "systemd-run", "--quiet", "--unit=" + unit,
                "--on-active=1s", "--on-unit-inactive=1s",
                "--timer-property=AccuracySec=100ms",
                "--property=Type=oneshot", "--property=Restart=no",
                "--property=StartLimitIntervalSec=0",
                "--property=TimeoutStartSec=3s",
                sys.executable, str(probe), str(evidence),
            ], check=True, timeout=10)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                count = int(evidence.read_text()) if evidence.exists() else 0
                if count >= 8:
                    break
                time.sleep(0.25)
            else:
                raise AssertionError("timer did not continue through six failures and recovery")
            properties = subprocess.check_output([
                "systemctl", "show", service, "--property=NRestarts",
                "--property=Result", "--property=StartLimitIntervalUSec",
            ], text=True, timeout=10)
            assert "NRestarts=0" in properties, properties
            assert "Result=success" in properties, properties
            assert "StartLimitIntervalUSec=0" in properties, properties
            print("health_timer_retry=pass six_failures_then_success_then_next_probe")
        finally:
            subprocess.run(["systemctl", "stop", timer, service], check=False, timeout=10)
            subprocess.run(["systemctl", "reset-failed", service], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
