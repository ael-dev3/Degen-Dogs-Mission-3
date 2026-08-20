#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
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
SCHEME_SECRET_RPC_URL = CUSTOM_RPC_URL.replace("https://", "api-key-secret://")
SECRETS = (
    "api-key-secret",
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
    sys.modules[name] = module
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
    assert_custom_url_redacted(mission1.redact_url(SCHEME_SECRET_RPC_URL))
    assert_custom_url_redacted(mission1.redact_text(f"provider={SCHEME_SECRET_RPC_URL}"))
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

    try:
        mission1.rpc_call([SCHEME_SECRET_RPC_URL], "eth_chainId", [], timeout=1)
    except RuntimeError as exc:
        assert_custom_url_redacted(str(exc))
    else:
        raise AssertionError("Mission 1 accepted a non-HTTPS secret-bearing RPC URL")

    original_urlopen = mission1.urlopen
    mission1.urlopen = lambda *_args, **_kwargs: type(  # noqa: ARG005
        "Response",
        (),
        {
            "read": lambda self: json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "invalid path-secret query-secret"},
                }
            ).encode("utf-8")
        },
    )()
    try:
        try:
            mission1.rpc_call(["https://polygon.drpc.org"], "eth_chainId", [], timeout=1)
        except RuntimeError as exc:
            assert "code=-32000" in str(exc)
            for secret in SECRETS:
                assert secret not in str(exc)
        else:
            raise AssertionError("Mission 1 provider error unexpectedly succeeded")
    finally:
        mission1.urlopen = original_urlopen


def test_mission2_custom_rpc_redaction_covers_manifests_and_retry_errors() -> None:
    mission2 = load_module("archive_mission2_index")
    assert_custom_url_redacted(mission2.redact_url(CUSTOM_RPC_URL))
    assert_custom_url_redacted(mission2.redact_text(f"provider={CUSTOM_RPC_URL}"))
    assert_custom_url_redacted(mission2.redact_text(f"provider={UPPERCASE_CUSTOM_RPC_URL}"))
    assert_custom_url_redacted(mission2.redact_url(SCHEME_SECRET_RPC_URL))
    assert_custom_url_redacted(mission2.redact_text(f"provider={SCHEME_SECRET_RPC_URL}"))
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

    try:
        mission2.rpc_call_retry(SCHEME_SECRET_RPC_URL, "eth_chainId", [], attempts=1)
    except RuntimeError as exc:
        assert_custom_url_redacted(str(exc))
    else:
        raise AssertionError("Mission 2 accepted a non-HTTPS secret-bearing RPC URL")

    class Mission2Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "invalid path-secret query-secret"},
                }
            ).encode("utf-8")

    original_urlopen = mission2.urllib.request.urlopen
    mission2.urllib.request.urlopen = lambda *_args, **_kwargs: Mission2Response()  # noqa: ARG005
    try:
        try:
            mission2.rpc_call("https://rpc.degen.tips", "eth_chainId", [], timeout=1)
        except RuntimeError as exc:
            assert "code=-32000" in str(exc)
            for secret in SECRETS:
                assert secret not in str(exc)
        else:
            raise AssertionError("Mission 2 provider error unexpectedly succeeded")
    finally:
        mission2.urllib.request.urlopen = original_urlopen


def test_refresh_telemetry_custom_rpc_redaction_covers_returned_and_logged_rows() -> None:
    telemetry = load_module("refresh_telemetry")
    assert_custom_url_redacted(telemetry.redact_url(CUSTOM_RPC_URL))
    assert_custom_url_redacted(telemetry.redact_value({"error": f"provider={CUSTOM_RPC_URL}"}))
    assert_custom_url_redacted(
        telemetry.redact_value({UPPERCASE_CUSTOM_RPC_URL: UPPERCASE_CUSTOM_RPC_URL})
    )
    assert_custom_url_redacted(telemetry.redact_url(SCHEME_SECRET_RPC_URL))
    assert_custom_url_redacted(
        telemetry.redact_value({"error": f"provider={SCHEME_SECRET_RPC_URL}"})
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
        bundle_module = load_module("build_live_snapshot_bundle")
        bundle_name = f"live_snapshot_1_{'a' * 64}_{'b' * 64}.json"
        bundle_bytes = b"{}\n"
        status_path = root / "public" / "generated" / "refresh_status.json"
        status_path.parent.mkdir(parents=True)
        status_path.write_text(
            json.dumps(
                {
                    "status": "expected",
                    "live_snapshot_bundle": bundle_name,
                    "live_snapshot_bundle_bytes": len(bundle_bytes),
                    "live_snapshot_bundle_sha256": "b" * 64,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (status_path.parent / bundle_name).write_bytes(bundle_bytes)
        clock = {"now": 0.0}
        original_monotonic = telemetry.time.monotonic
        original_sleep = telemetry.time.sleep
        original_fetch_json = telemetry.fetch_json
        original_validate_bundle = bundle_module.validate_live_snapshot_bundle
        telemetry.time.monotonic = lambda: clock["now"]
        telemetry.time.sleep = lambda seconds: clock.__setitem__(
            "now",
            clock["now"] + seconds,
        )
        telemetry.fetch_json = lambda _url: (_ for _ in ()).throw(OSError(CUSTOM_RPC_URL))
        bundle_module.validate_live_snapshot_bundle = lambda **_kwargs: {
            "filename": bundle_name
        }
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
            bundle_module.validate_live_snapshot_bundle = original_validate_bundle
        assert result["live_verify_result"] == "timeout"
        assert_custom_url_redacted(result["error"])


def test_dashboard_and_mission3_archive_never_preserve_secret_uri_schemes() -> None:
    dashboard = load_module("build_dashboard")
    mission3 = load_module("archive_mission3_index")
    assert_custom_url_redacted(dashboard._redact_rpc_url(SCHEME_SECRET_RPC_URL))
    assert_custom_url_redacted(mission3.redact_url(SCHEME_SECRET_RPC_URL))


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"rpc_redaction_tests=pass count={len(tests)}")
