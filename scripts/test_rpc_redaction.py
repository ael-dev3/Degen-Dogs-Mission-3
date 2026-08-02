#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import urllib.error
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CUSTOM_RPC_URL = (
    "https://user-secret:password-secret@host-secret.rpc.custom.example/"
    "path-secret?token=query-secret#fragment-secret"
)
UPPERCASE_CUSTOM_RPC_URL = CUSTOM_RPC_URL.replace("https://", "HTTPS://")
SECRETS = (
    "user-secret",
    "password-secret",
    "host-secret",
    "path-secret",
    "query-secret",
    "fragment-secret",
    "rpc.custom.example",
)


def load_module(name: str) -> Any:
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_custom_url_redacted(value: Any) -> None:
    text = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
    for secret in SECRETS:
        assert secret not in text
    assert "rpc-host-" in text


def test_mission1_custom_rpc_redaction_covers_url_text_and_errors() -> None:
    mission1 = load_module("archive_mission1_index")
    assert_custom_url_redacted(mission1.redact_url(CUSTOM_RPC_URL))
    assert_custom_url_redacted(mission1.redact_text(f"provider={CUSTOM_RPC_URL}"))
    assert_custom_url_redacted(mission1.redact_text(f"provider={UPPERCASE_CUSTOM_RPC_URL}"))
    assert mission1.redact_url("https://polygon.drpc.org") == "https://polygon.drpc.org"

    original_urlopen = mission1.urlopen
    try:
        for failure in (urllib.error.URLError(CUSTOM_RPC_URL), ValueError(CUSTOM_RPC_URL)):
            mission1.urlopen = lambda *_args, _failure=failure, **_kwargs: (  # noqa: ARG005
                _ for _ in ()
            ).throw(_failure)
            try:
                mission1.rpc_call([CUSTOM_RPC_URL], "eth_chainId", [], timeout=1)
            except RuntimeError as exc:
                assert_custom_url_redacted(str(exc))
            else:
                raise AssertionError("Mission 1 RPC failure unexpectedly succeeded")
    finally:
        mission1.urlopen = original_urlopen


def test_mission2_custom_rpc_redaction_covers_manifests_and_retry_errors() -> None:
    mission2 = load_module("archive_mission2_index")
    assert_custom_url_redacted(mission2.redact_url(CUSTOM_RPC_URL))
    assert_custom_url_redacted(mission2.redact_text(f"provider={CUSTOM_RPC_URL}"))
    assert_custom_url_redacted(mission2.redact_text(f"provider={UPPERCASE_CUSTOM_RPC_URL}"))
    assert mission2.redact_url("https://rpc.degen.tips") == "https://rpc.degen.tips"

    original_rpc_call = mission2.rpc_call
    try:
        for failure in (RuntimeError(CUSTOM_RPC_URL), ValueError(CUSTOM_RPC_URL)):
            mission2.rpc_call = lambda *_args, _failure=failure, **_kwargs: (  # noqa: ARG005
                _ for _ in ()
            ).throw(_failure)
            try:
                mission2.rpc_call_retry(CUSTOM_RPC_URL, "eth_chainId", [], attempts=1)
            except RuntimeError as exc:
                assert_custom_url_redacted(str(exc))
            else:
                raise AssertionError("Mission 2 RPC failure unexpectedly succeeded")
    finally:
        mission2.rpc_call = original_rpc_call


def test_refresh_telemetry_custom_rpc_redaction_covers_returned_and_logged_rows() -> None:
    telemetry = load_module("refresh_telemetry")
    assert_custom_url_redacted(telemetry.redact_url(CUSTOM_RPC_URL))
    assert_custom_url_redacted(telemetry.redact_value({"error": f"provider={CUSTOM_RPC_URL}"}))
    assert_custom_url_redacted(
        telemetry.redact_value({UPPERCASE_CUSTOM_RPC_URL: UPPERCASE_CUSTOM_RPC_URL})
    )
    assert telemetry.redact_url("https://ael-dev3.github.io") == "https://ael-dev3.github.io"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env = {
            "DEGEN_DOGS_REFRESH_TELEMETRY_PATH": str(root / ".local" / "refresh.jsonl"),
            "DEGEN_DOGS_REFRESH_METRICS_PATH": str(root / "logs" / "refresh.jsonl"),
            "DEGEN_DOGS_REFRESH_TRIGGER": f"provider={CUSTOM_RPC_URL}",
            "DEGEN_DOGS_REFRESH_REASONS": json.dumps([CUSTOM_RPC_URL]),
            "DEGEN_DOGS_REMOTE": CUSTOM_RPC_URL,
        }
        row = telemetry.record_refresh(env, result="failed", error=CUSTOM_RPC_URL, root=root)
        logged = (root / "logs" / "refresh.jsonl").read_text(encoding="utf-8")
        assert_custom_url_redacted(row)
        assert_custom_url_redacted(logged)

    original_platform_system = telemetry.platform.system
    original_check_output = telemetry.subprocess.check_output
    telemetry.platform.system = lambda: "Darwin"
    telemetry.subprocess.check_output = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # noqa: E731
        RuntimeError(CUSTOM_RPC_URL)
    )
    try:
        assert_custom_url_redacted(telemetry.detect_launchd("example.service"))
    finally:
        telemetry.platform.system = original_platform_system
        telemetry.subprocess.check_output = original_check_output

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        status_path = root / "public" / "generated" / "refresh_status.json"
        status_path.parent.mkdir(parents=True)
        status_path.write_text('{"status":"expected"}\n', encoding="utf-8")
        monotonic_values = iter((0.0, 0.0, 2.0))
        original_monotonic = telemetry.time.monotonic
        original_sleep = telemetry.time.sleep
        original_fetch_json = telemetry.fetch_json
        telemetry.time.monotonic = lambda: next(monotonic_values)
        telemetry.time.sleep = lambda _seconds: None
        telemetry.fetch_json = lambda _url: (_ for _ in ()).throw(OSError(CUSTOM_RPC_URL))
        try:
            result = telemetry.verify_live(
                {"DEGEN_DOGS_COMMIT_SHA": "a" * 40},
                root=root,
                timeout_seconds=1,
                interval_seconds=1,
                base_url=telemetry.SITE_URL,
            )
        finally:
            telemetry.time.monotonic = original_monotonic
            telemetry.time.sleep = original_sleep
            telemetry.fetch_json = original_fetch_json
        assert result["live_verify_result"] == "timeout"
        assert_custom_url_redacted(result["error"])


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"rpc_redaction_tests=pass count={len(tests)}")
