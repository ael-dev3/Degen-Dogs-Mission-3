#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("check_remote_freshness.py")
WORKFLOW_PATH = MODULE_PATH.parent.parent / ".github" / "workflows" / "freshness-watchdog.yml"
PAGES_WORKFLOW_PATH = MODULE_PATH.parent.parent / ".github" / "workflows" / "deploy-pages.yml"


def load_module():
    spec = importlib.util.spec_from_file_location("check_remote_freshness", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def status(block: int, timestamp: str) -> dict:
    return {
        "kind": "refresh_status",
        "last_refresh_result": "success_generated",
        "last_successful_refresh_time_utc": timestamp,
        "latest_generated_block": block,
        "onchain_chain_id": "8453",
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": "snapshot_hash,contract_code,current_auction,dog_total_supply,recent_event_logs",
        "snapshot_block_hash": "0x" + "a" * 64,
        "rpc_quorum_size": 2,
        "rpc_quorum_agreement": "2/3",
        "rpc_quorum_providers": "alchemy,publicnode.com",
        "log_rpc_quorum_providers": "alchemy,base.org",
    }


class FakeResponse:
    def __init__(self, body: bytes, *, content_type: str, status_code: int = 200, final_url: str = "") -> None:
        self.body = body
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.status_code = status_code
        self.final_url = final_url
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self.final_url

    def getcode(self) -> int:
        return self.status_code

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body[:limit]


class FakeOpener:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.called = False

    def open(self, request, *, timeout):  # noqa: ANN001, ANN201, ARG002
        self.called = True
        if not self.response.final_url:
            self.response.final_url = request.full_url
        return self.response


def test_fetch_json_accepts_only_bounded_exact_fixed_target_response() -> None:
    monitor = load_module()
    payload = status(101, "2026-08-02T12:50:00Z")
    response = FakeResponse(json.dumps(payload).encode(), content_type="text/plain; charset=utf-8")
    opener = FakeOpener(response)
    monitor.STATUS_OPENER = opener

    assert monitor.fetch_json(
        monitor.DEFAULT_RAW_URL,
        3,
        expected_url=monitor.DEFAULT_RAW_URL,
    ) == payload
    assert opener.called is True
    assert response.read_limit == monitor.STATUS_RESPONSE_LIMIT_BYTES + 1


def test_fetch_json_rejects_unapproved_override_before_network() -> None:
    monitor = load_module()
    response = FakeResponse(b"{}", content_type="application/json")
    opener = FakeOpener(response)
    monitor.STATUS_OPENER = opener

    try:
        monitor.fetch_json("https://attacker.example/status.json", 3, expected_url=monitor.DEFAULT_RAW_URL)
    except RuntimeError as exc:
        assert "approved fixed target" in str(exc)
    else:
        raise AssertionError("unapproved watchdog target was fetched")
    assert opener.called is False


def test_fetch_json_rejects_changed_final_url_status_mime_and_oversize() -> None:
    monitor = load_module()
    cases = [
        (FakeResponse(b"{}", content_type="text/plain", final_url="https://attacker.example/status.json"), "changed origin"),
        (FakeResponse(b"{}", content_type="text/plain", status_code=206), "non-success status"),
        (FakeResponse(b"{}", content_type="application/json"), "content type"),
    ]
    for response, expected_error in cases:
        monitor.STATUS_OPENER = FakeOpener(response)
        try:
            monitor.fetch_json(monitor.DEFAULT_RAW_URL, 3, expected_url=monitor.DEFAULT_RAW_URL)
        except RuntimeError as exc:
            assert expected_error in str(exc)
        else:
            raise AssertionError(f"unsafe watchdog response accepted: {expected_error}")

    response = FakeResponse(b"{}", content_type="text/plain")
    response.headers["Content-Length"] = str(monitor.STATUS_RESPONSE_LIMIT_BYTES + 1)
    monitor.STATUS_OPENER = FakeOpener(response)
    try:
        monitor.fetch_json(monitor.DEFAULT_RAW_URL, 3, expected_url=monitor.DEFAULT_RAW_URL)
    except RuntimeError as exc:
        assert "size limit" in str(exc)
    else:
        raise AssertionError("oversize declared watchdog response was accepted")
    assert response.read_limit is None


def test_fetch_json_enforces_stream_cap_and_strict_utf8_json() -> None:
    monitor = load_module()
    oversized = FakeResponse(b" " * (monitor.STATUS_RESPONSE_LIMIT_BYTES + 1), content_type="text/plain")
    oversized.headers.pop("Content-Length")
    monitor.STATUS_OPENER = FakeOpener(oversized)
    try:
        monitor.fetch_json(monitor.DEFAULT_RAW_URL, 3, expected_url=monitor.DEFAULT_RAW_URL)
    except RuntimeError as exc:
        assert "size limit" in str(exc)
    else:
        raise AssertionError("oversize streamed watchdog response was accepted")

    invalid = FakeResponse(b"\xff", content_type="text/plain")
    monitor.STATUS_OPENER = FakeOpener(invalid)
    try:
        monitor.fetch_json(monitor.DEFAULT_RAW_URL, 3, expected_url=monitor.DEFAULT_RAW_URL)
    except RuntimeError as exc:
        assert "invalid UTF-8 JSON" in str(exc)
    else:
        raise AssertionError("invalid watchdog payload was accepted")


def test_healthy_equal_status() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    report = monitor.assess_freshness(
        status(100, "2026-08-02T12:30:00Z"),
        status(100, "2026-08-02T12:30:00Z"),
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["status"] == "healthy"
    assert report["incident"] is False


def test_stale_raw_opens_incident_without_pages_redeploy() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    report = monitor.assess_freshness(
        status(100, "2026-08-02T10:00:00Z"),
        status(100, "2026-08-02T10:00:00Z"),
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["raw_stale"] is True
    assert report["pages_needs_deploy"] is False


def test_pages_lag_redeploys_only_after_grace() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    raw = status(101, "2026-08-02T12:50:00Z")
    pages = status(100, "2026-08-02T12:30:00Z")
    report = monitor.assess_freshness(
        raw,
        pages,
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["pages_needs_deploy"] is True
    assert report["incident"] is True


def test_invalid_onchain_verification_is_stale() -> None:
    monitor = load_module()
    now = datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc)
    raw = status(101, "2026-08-02T12:50:00Z")
    raw["onchain_verification_status"] = "single_provider"
    report = monitor.assess_freshness(
        raw,
        status(101, "2026-08-02T12:50:00Z"),
        now=now,
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["raw_stale"] is True
    assert "cross-provider" in report["raw_problem"]


def test_status_rejects_incomplete_scope() -> None:
    monitor = load_module()
    payload = status(101, "2026-08-02T12:50:00Z")
    payload["onchain_verification_scope"] = "snapshot_hash,current_auction"
    problem = monitor.status_problem(payload)
    assert "scope is incomplete" in problem
    assert "contract_code" in problem


def test_status_rejects_malformed_or_zero_snapshot_hash() -> None:
    monitor = load_module()
    payload = status(101, "2026-08-02T12:50:00Z")
    payload["snapshot_block_hash"] = "0x1234"
    assert monitor.status_problem(payload) == "snapshot block hash is invalid"
    payload["snapshot_block_hash"] = "0x" + "0" * 64
    assert monitor.status_problem(payload) == "snapshot block hash is invalid"


def test_status_rejects_unsubstantiated_quorum() -> None:
    monitor = load_module()
    payload = status(101, "2026-08-02T12:50:00Z")
    payload["rpc_quorum_agreement"] = "1/3"
    assert "below the required quorum" in monitor.status_problem(payload)
    payload["rpc_quorum_agreement"] = "2/3"
    payload["rpc_quorum_providers"] = "same-provider,same-provider"
    assert "provider set" in monitor.status_problem(payload)
    payload["rpc_quorum_providers"] = "alchemy,publicnode.com"
    payload["log_rpc_quorum_providers"] = "alchemy"
    assert "log RPC provider set" in monitor.status_problem(payload)


def test_same_block_payload_mismatch_becomes_incident_after_grace() -> None:
    monitor = load_module()
    raw = status(101, "2026-08-02T12:30:00Z")
    pages = status(101, "2026-08-02T12:30:00Z")
    pages["current_bid_eth"] = "0.001"
    pages["snapshot_block_hash"] = "0x" + "b" * 64
    report = monitor.assess_freshness(
        raw,
        pages,
        now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["incident"] is True
    assert report["pages_needs_deploy"] is False
    assert report["payload_relation"] == "same_block_mismatch"
    assert "snapshot_block_hash" in report["payload_mismatch_fields"]
    assert "same_block_mismatch" in report["pages_problem"]


def test_same_block_payload_mismatch_is_tolerated_during_grace() -> None:
    monitor = load_module()
    raw = status(101, "2026-08-02T12:55:00Z")
    pages = status(101, "2026-08-02T12:55:00Z")
    pages["current_bid_eth"] = "0.001"
    report = monitor.assess_freshness(
        raw,
        pages,
        now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["incident"] is False
    assert report["payload_relation"] == "same_block_mismatch"


def test_missing_null_field_is_still_a_payload_mismatch() -> None:
    monitor = load_module()
    raw = status(101, "2026-08-02T12:30:00Z")
    pages = status(101, "2026-08-02T12:30:00Z")
    raw["optional_field"] = None
    report = monitor.assess_freshness(
        raw,
        pages,
        now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["incident"] is True
    assert "optional_field" in report["payload_mismatch_fields"]


def test_newer_same_block_raw_payload_requests_deploy_after_grace() -> None:
    monitor = load_module()
    raw = status(101, "2026-08-02T12:50:00Z")
    pages = status(101, "2026-08-02T12:30:00Z")
    raw["current_bid_eth"] = "0.002"
    report = monitor.assess_freshness(
        raw,
        pages,
        now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["incident"] is True
    assert report["pages_needs_deploy"] is True
    assert report["payload_relation"] == "same_block_mismatch"


def test_pages_ahead_is_incident_but_does_not_roll_back_via_deploy() -> None:
    monitor = load_module()
    report = monitor.assess_freshness(
        status(101, "2026-08-02T12:30:00Z"),
        status(102, "2026-08-02T12:50:00Z"),
        now=datetime(2026, 8, 2, 13, 0, tzinfo=timezone.utc),
        max_raw_age_seconds=5400,
        propagation_grace_seconds=900,
    )
    assert report["incident"] is True
    assert report["pages_needs_deploy"] is False
    assert report["payload_relation"] == "pages_ahead"


def test_watchdog_issue_matching_requires_bot_and_marker_in_both_paths() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert workflow.count('.user.login == "github-actions[bot]"') == 2
    assert workflow.count("contains($marker)") == 2
    assert "<!-- degen-dogs-freshness-watchdog:v1 -->" in workflow
    assert "gh issue list" not in workflow


def test_pages_recovery_is_latest_wins_bounded_and_deduplicated() -> None:
    pages_workflow = PAGES_WORKFLOW_PATH.read_text(encoding="utf-8")
    watchdog_workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "group: pages" in pages_workflow
    assert "cancel-in-progress: ${{ github.event_name == 'workflow_dispatch' }}" in pages_workflow
    assert "--json databaseId,status,headSha,createdAt" in watchdog_workflow
    assert "--limit 1000" in watchdog_workflow
    assert "^[0-9a-f]{40}$" in watchdog_workflow
    assert "fromdateiso8601" in watchdog_workflow
    assert "1800" in watchdog_workflow
    assert "stale_run_ids" in watchdog_workflow
    assert "gh run cancel" in watchdog_workflow
    assert "completed during the cancellation race" in watchdog_workflow
    assert "recent_same_sha_runs" in watchdog_workflow
    assert watchdog_workflow.count("gh workflow run deploy-pages.yml") == 1


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"remote_freshness_tests=pass count={len(tests)}")
