#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "refresh_telemetry.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("refresh_telemetry", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def assert_raises_contains(fn: Any, needle: str) -> None:
    try:
        fn()
    except AssertionError as exc:
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"expected AssertionError containing {needle!r}")


TEST_BASE_TIME = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)


def iso(offset_seconds: int = 0) -> str:
    return (TEST_BASE_TIME + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def write_fixture(root: Path) -> None:
    metrics = {
        "latest_block": "46822740",
        "latest_block_time_utc": "2026-06-02 21:13:47",
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": "snapshot_hash,contract_code,current_auction,dog_total_supply,dog_token_uri_bindings,recent_event_logs",
        "onchain_chain_id": "8453",
        "snapshot_block_hash": "0x" + "a" * 64,
        "snapshot_confirmations": "1",
        "rpc_quorum_size": "2",
        "rpc_quorum_agreement": "2/2",
        "rpc_quorum_providers": "provider-one.example|provider-two.example",
        "log_rpc_quorum_providers": "provider-one.example|provider-two.example",
        "auction_house_code_sha256": "b" * 64,
        "dog_nft_code_sha256": "c" * 64,
        "dog_total_supply": "792",
        "dog_id_ceiling": "792",
        "dog_token_uri_verification_status": "hash_pinned_cross_provider_exact_outcome_quorum",
        "dog_base_existence_verification_status": "hash_pinned_cross_provider_exists_token_uri_parity_quorum",
        "dog_token_uri_present_count": "792",
        "dog_token_uri_unavailable_count": "0",
        "dog_base_existing_count": "792",
        "dog_base_unclaimed_count": "0",
        "dog_base_existing_token_ids_sha256": "d" * 64,
        "dog_base_unclaimed_token_ids_sha256": "e" * 64,
        "dog_metadata_verification_status": "complete_onchain_token_uri_verified",
        "dog_metadata_onchain_verified_count": "792",
        "dog_metadata_unavailable_count": "0",
        "dog_metadata_content_verification_status": "verified_token_uri_offchain_content_hash_observed",
        "dog_metadata_content_observed_count": "792",
        "dog_rarity_verification_status": "complete_verified_existing_token_universe",
        "dog_rarity_universe_count": "792",
        "dog_rarity_excluded_nonexistent_count": "0",
        "dog_rarity_incomplete_metadata_count": "0",
        "dog_rarity_scope": "base_existing",
        "dog_rarity_attested_block": "46822740",
        "dog_rarity_attested_block_hash": "0x" + "a" * 64,
        "dog_rarity_continuity_through_block": "46822740",
        "dog_rarity_continuity_through_block_hash": "0x" + "a" * 64,
        "dog_rarity_continuity_verification_status": "full_snapshot_exists_token_uri_content_schema_attested",
        "current_auction_token_id": "732",
        "current_bid_eth": "0.01",
        "current_bidder": "@thec1",
        "current_bidder_wallet": "0xd29c790466675153a50df7860b9efdb689a21cde",
        "current_auction_status": "live",
        "current_auction_end_utc": "2026-06-03 19:37:21",
    }
    rows = [{"metric": key, "value": value} for key, value in metrics.items()]
    write_json(root / "generated" / "mission3_metrics.json", rows)
    write_json(root / "public" / "generated" / "mission3_metrics.json", rows)
    (root / "generated").mkdir(parents=True, exist_ok=True)
    (root / "generated" / "mission3_metrics.csv").write_text(
        "metric,value\n" + "".join(f"{key},{value}\n" for key, value in metrics.items()),
        encoding="utf-8",
    )
    current = {
        "token_id": 732,
        "current_bid_eth": 0.01,
        "bidder": "@thec1",
        "bidder_wallet": "0xd29c790466675153a50df7860b9efdb689a21cde",
        "auction_state": "live",
        "end_time_utc": "2026-06-03 19:37:21",
        "latest_block": 46822740,
        "latest_block_time_utc": "2026-06-02 21:13:47",
    }
    write_json(root / "generated" / "current_auction.json", [current])
    write_json(root / "public" / "generated" / "current_auction.json", [current])


def base_env(root: Path) -> dict[str, str]:
    return {
        "DEGEN_DOGS_REFRESH_TELEMETRY_PATH": str(root / ".local" / "refresh_runs.jsonl"),
        "DEGEN_DOGS_REFRESH_METRICS_PATH": str(root / "logs" / "refresh-metrics.jsonl"),
        "MISSION3_WATCHER_TELEMETRY_PATH": str(root / ".local" / "watcher_checks.jsonl"),
        "DEGEN_DOGS_REFRESH_RUN_ID": "unit-run-1",
        "DEGEN_DOGS_REFRESH_TRIGGER": "watcher",
        "DEGEN_DOGS_REFRESH_REASONS": json.dumps(["auction_bid", "highest_bid_amount_changed"]),
        "DEGEN_DOGS_REFRESH_QUEUED_AT_UTC": iso(0),
        "DEGEN_DOGS_LOCK_ACQUIRED_AT_UTC": iso(2),
        "DEGEN_DOGS_REFRESH_STARTED_AT_UTC": iso(3),
        "DEGEN_DOGS_DETECTED_AT_UTC": iso(-5),
        "DEGEN_DOGS_EVENT_NAME": "AuctionBid",
        "DEGEN_DOGS_EVENT_BLOCK_NUMBER": "46822730",
        "DEGEN_DOGS_EVENT_TX_HASH": "0xabc",
        "DEGEN_DOGS_EVENT_LOG_INDEX": "4",
        "DEGEN_DOGS_DATA_STARTED_AT_UTC": iso(4),
        "DEGEN_DOGS_DATA_COMPLETED_AT_UTC": iso(10),
        "DEGEN_DOGS_BUILD_STARTED_AT_UTC": iso(11),
        "DEGEN_DOGS_BUILD_COMPLETED_AT_UTC": iso(13),
        "DEGEN_DOGS_PUSH_STARTED_AT_UTC": iso(14),
        "DEGEN_DOGS_PUSH_COMPLETED_AT_UTC": iso(17),
        "DEGEN_DOGS_COMMIT_SHA": "a" * 40,
    }


def test_record_refresh_redacts_secrets_and_writes_public_status() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        row = telemetry.record_refresh(env, result="success_pushed", error="api_key=sk-secretsecretsecretsecretsecret and url=https://rpc.quicknode.pro/abc?token=secret", root=root)
        text = json.dumps(row)
        assert "sk-secret" not in text
        assert "quicknode.pro/abc" not in text
        assert row["result"] == "success_pushed"
        assert row["lock_wait_seconds"] == 2
        assert row["push_duration_seconds"] == 3
        assert row["detect_to_push_seconds"] == 22

        status = telemetry.write_refresh_status(env, root=root)
        assert status["kind"] == "refresh_status"
        assert status["latest_generated_block"] == 46822740
        assert status["current_dog_token_id"] == 732
        assert status["current_high_bidder"] == "@thec1"
        assert status["dog_token_uri_present_count"] == 792
        assert status["dog_token_uri_unavailable_count"] == 0
        assert status["dog_metadata_verification_status"] == "complete_onchain_token_uri_verified"
        generated = telemetry.generated_state(root)
        for key in (
            "dog_token_uri_verification_status",
            "dog_token_uri_present_count",
            "dog_token_uri_unavailable_count",
            "dog_metadata_verification_status",
            "dog_metadata_onchain_verified_count",
            "dog_metadata_unavailable_count",
        ):
            assert status[key] == generated[key]
        assert "/Users/" not in json.dumps(status)
        validated = telemetry.validate_refresh_status(root=root)
        assert validated == status


def test_refresh_outcome_rows_cover_no_diff_failure_and_live_timeout() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        no_diff = telemetry.build_refresh_row(env, result="success_no_diff", root=root)
        assert no_diff["result"] == "success_no_diff"
        assert no_diff["reasons"] == ["auction_bid", "highest_bid_amount_changed"]

        superseded = telemetry.build_refresh_row(env, result="success_superseded_by_peer", root=root)
        assert superseded["result"] == "success_superseded_by_peer"
        assert superseded["commit_sha"] == "a" * 40
        assert "success_superseded_by_peer" in telemetry.SUCCESS_RESULTS
        original_read_jsonl = telemetry.read_jsonl
        telemetry.read_jsonl = lambda path, limit=1000: [superseded] if Path(path).name == "refresh_runs.jsonl" else []
        try:
            latest_success = telemetry.latest_successful_refresh_row(env, root=root)
            assert latest_success["result"] == "success_superseded_by_peer"
            assert telemetry.metrics_summary(env, root=root)["last_pushed_commit"] == "a" * 40
        finally:
            telemetry.read_jsonl = original_read_jsonl

        failed = telemetry.build_refresh_row(env, result="failed", error="password=hunter2", root=root)
        assert failed["result"] == "failed"
        assert "hunter2" not in json.dumps(failed)

        timeout_env = dict(env)
        timeout_env.update(
            {
                "DEGEN_DOGS_LIVE_VERIFY_STARTED_AT_UTC": iso(18),
                "DEGEN_DOGS_LIVE_VERIFY_RESULT": "timeout",
                "DEGEN_DOGS_RAW_COMMIT_VERIFIED": "true",
                "DEGEN_DOGS_LIVE_VERIFY_ERROR": "github_pages mismatch fields=latest_generated_block",
                "DEGEN_DOGS_PUSH_TO_LIVE_SECONDS": "300",
                "DEGEN_DOGS_BLOCK_TO_LIVE_SECONDS": "420",
            }
        )
        timeout = telemetry.build_refresh_row(timeout_env, result="success_pushed_live_timeout", root=root)
        assert timeout["result"] == "success_pushed_live_timeout"
        assert timeout["live_verify_result"] == "timeout"
        assert timeout["raw_commit_verified"] is True
        assert timeout["live_verify_error"] == "github_pages mismatch fields=latest_generated_block"
        assert timeout["push_to_live_seconds"] == 300
        telemetry.record_refresh(timeout_env, result="success_pushed_live_timeout", root=root)
        status = telemetry.write_refresh_status(env, root=root)
        assert status["last_refresh_result"] == "success_generated"
        assert "last_pushed_commit" not in status
        assert "last_push_duration_seconds" not in status
        assert telemetry.validate_refresh_status(root=root) == status


def test_verify_live_requires_full_github_pages_status_parity() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        expected = telemetry.write_refresh_status(env, root=root, prefer_current_env=True)
        stale = dict(expected)
        stale["latest_generated_block"] = int(expected["latest_generated_block"]) - 1

        calls = iter([0.0, 0.0, 2.0])
        telemetry.time.monotonic = lambda: next(calls)
        telemetry.time.sleep = lambda _seconds: None
        fetched_urls: list[str] = []

        def fetch_raw_only(url: str) -> Any:
            fetched_urls.append(url)
            return expected if "raw.githubusercontent.com" in url else stale

        telemetry.fetch_json = fetch_raw_only
        raw_only = telemetry.verify_live(
            env,
            root=root,
            timeout_seconds=1,
            interval_seconds=1,
            base_url="https://ael-dev3.github.io/Degen-Dogs-Mission-3/",
        )
        assert raw_only["live_verify_result"] == "timeout"
        assert raw_only["raw_commit_verified"] is True
        assert raw_only["raw_main_verified"] is True
        assert raw_only["live_verify_source"] is None
        assert "latest_generated_block" in raw_only["error"]
        assert any(f"/{env['DEGEN_DOGS_COMMIT_SHA']}/public/generated/refresh_status.json?" in url for url in fetched_urls)
        assert all("/main/public/generated/refresh_status.json" not in url for url in fetched_urls)

        telemetry.time.monotonic = lambda: 0.0
        telemetry.fetch_json = lambda _url: expected
        both = telemetry.verify_live(
            env,
            root=root,
            timeout_seconds=1,
            interval_seconds=1,
            base_url="https://ael-dev3.github.io/Degen-Dogs-Mission-3/",
        )
        assert both["live_verify_result"] == "verified"
        assert both["raw_commit_verified"] is True
        assert both["raw_main_verified"] is True
        assert both["live_verify_source"] == "github_pages"

        # Immutable raw commit evidence remains valid once observed. A later
        # transient raw-host failure must not erase it just as Pages catches up.
        monotonic_calls = iter([0.0, 0.0, 0.5])
        telemetry.time.monotonic = lambda: next(monotonic_calls)
        responses: list[Any] = [expected, stale, RuntimeError("transient raw failure"), expected]

        def fetch_with_transient_raw_failure(_url: str) -> Any:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        telemetry.fetch_json = fetch_with_transient_raw_failure
        latched = telemetry.verify_live(
            env,
            root=root,
            timeout_seconds=1,
            interval_seconds=1,
            base_url="https://ael-dev3.github.io/Degen-Dogs-Mission-3/",
        )
        assert latched["live_verify_result"] == "verified"
        assert latched["raw_commit_verified"] is True
        assert latched["raw_main_verified"] is True
        assert latched["live_verify_source"] == "github_pages"
        assert responses == []

        assert telemetry.snapshot_mismatch(expected, expected) == ""
        unexpected_null = dict(expected)
        unexpected_null["attacker_controlled"] = None
        assert telemetry.snapshot_mismatch(expected, unexpected_null) == "fields=attacker_controlled"
        wrong_json_type = dict(expected)
        wrong_json_type["latest_generated_block"] = str(expected["latest_generated_block"])
        assert telemetry.snapshot_mismatch(expected, wrong_json_type) == "fields=latest_generated_block"


def test_live_verify_rejects_noncanonical_commit_sha_before_network() -> None:
    telemetry = load_module()
    invalid = (
        "",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "main",
        "a" * 39 + "/",
        "a" * 39 + "?",
        "a" * 39 + "\n",
        "a" * 38 + "..",
    )
    for value in invalid:
        try:
            telemetry.immutable_raw_status_url(value)
        except RuntimeError as exc:
            assert "40-hex" in str(exc)
        else:
            raise AssertionError(f"invalid pushed commit SHA was accepted: {value!r}")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        telemetry.write_refresh_status(env, root=root, prefer_current_env=True)
        env["DEGEN_DOGS_COMMIT_SHA"] = "main/../../attacker"
        telemetry.fetch_json = lambda _url: (_ for _ in ()).throw(AssertionError("network should not be used"))
        try:
            telemetry.verify_live(
                env,
                root=root,
                timeout_seconds=1,
                interval_seconds=1,
                base_url="https://ael-dev3.github.io/Degen-Dogs-Mission-3/",
            )
        except RuntimeError as exc:
            assert "40-hex" in str(exc)
        else:
            raise AssertionError("invalid pushed commit SHA reached live verification")


def test_live_verify_network_and_env_file_boundaries() -> None:
    telemetry = load_module()
    try:
        telemetry.fetch_json("https://evil.example/generated/refresh_status.json")
    except RuntimeError as exc:
        assert "allowlist" in str(exc)
    else:
        raise AssertionError("an untrusted live-verification origin was accepted")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env_path = root / "verify.env"
        telemetry.write_env_file(
            env_path,
            {
                "live_verify_result": "verified'$(touch /tmp/nope)",
                "raw_commit_verified": True,
                "error": "github_pages mismatch",
            },
        )
        assert env_path.stat().st_mode & 0o777 == 0o600
        env_text = env_path.read_text(encoding="utf-8")
        assert "'\\''" in env_text
        assert "DEGEN_DOGS_RAW_COMMIT_VERIFIED='True'" in env_text
        assert "DEGEN_DOGS_LIVE_VERIFY_ERROR='github_pages mismatch'" in env_text

        outside = root / "outside"
        outside.write_text("preserve", encoding="utf-8")
        link = root / "link.env"
        link.symlink_to(outside)
        try:
            telemetry.write_env_file(link, {"live_verify_result": "verified"})
        except OSError:
            pass
        else:
            raise AssertionError("live-verification env writer followed a symlink")
        assert outside.read_text(encoding="utf-8") == "preserve"


def test_live_verify_transport_accepts_raw_text_plain_and_pages_json_only() -> None:
    telemetry = load_module()
    original_opener = telemetry.LIVE_STATUS_OPENER

    class Response:
        def __init__(self, body: bytes, content_type: str, *, final_url: str = "", status: int = 200) -> None:
            self.body = body
            self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
            self.final_url = final_url
            self.status = status
            self.read_limit = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return self.final_url

        def read(self, limit: int) -> bytes:
            self.read_limit = limit
            return self.body[:limit]

    class Opener:
        def __init__(self, response) -> None:
            self.response = response

        def open(self, request, *, timeout):  # noqa: ANN001, ANN201, ARG002
            if isinstance(self.response, Exception):
                raise self.response
            if not self.response.final_url:
                self.response.final_url = request.full_url
            return self.response

    commit_sha = "a" * 40
    raw_url = f"https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json?cache_bust=1"
    pages_url = "https://ael-dev3.github.io/Degen-Dogs-Mission-3/generated/refresh_status.json?cache_bust=1"
    payload = {"kind": "refresh_status", "latest_generated_block": 123}
    encoded = json.dumps(payload).encode()
    try:
        raw_response = Response(encoded, "text/plain; charset=utf-8")
        telemetry.LIVE_STATUS_OPENER = Opener(raw_response)
        assert telemetry.fetch_json(raw_url) == payload
        assert raw_response.read_limit == telemetry.LIVE_STATUS_MAX_BYTES + 1

        pages_response = Response(encoded, "application/json; charset=utf-8")
        telemetry.LIVE_STATUS_OPENER = Opener(pages_response)
        assert telemetry.fetch_json(pages_url) == payload

        unsafe_urls = (
            f"http://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json?cache_bust=1",
            f"https://raw.githubusercontent.com./ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json?cache_bust=1",
            f"https://raw.githubusercontent.com/attacker/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json?cache_bust=1",
            "https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/main/public/generated/refresh_status.json?cache_bust=1",
            f"https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/generated/refresh_status.json?cache_bust=1",
            f"https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json/extra?cache_bust=1",
            f"https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}%2fmain/public/generated/refresh_status.json?cache_bust=1",
            f"https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json?cache_bust=1&next=evil",
            f"https://raw.githubusercontent.com:443/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json?cache_bust=1",
        )
        for unsafe_url in unsafe_urls:
            try:
                telemetry.fetch_json(unsafe_url)
            except RuntimeError as exc:
                assert "allowlist" in str(exc)
            else:
                raise AssertionError(f"unsafe live-verification path was accepted: {unsafe_url}")

        cases = [
            (Response(encoded, "application/json"), raw_url, "content type"),
            (Response(encoded, "text/plain", final_url="https://attacker.example/status.json"), raw_url, "URL changed"),
            (Response(encoded, "text/plain", status=206), raw_url, "HTTP status"),
        ]
        for response, url, expected_error in cases:
            telemetry.LIVE_STATUS_OPENER = Opener(response)
            try:
                telemetry.fetch_json(url)
            except RuntimeError as exc:
                assert expected_error in str(exc)
            else:
                raise AssertionError(f"unsafe live-verification response accepted: {expected_error}")

        oversized = Response(b"{}", "text/plain")
        oversized.headers["Content-Length"] = str(telemetry.LIVE_STATUS_MAX_BYTES + 1)
        telemetry.LIVE_STATUS_OPENER = Opener(oversized)
        try:
            telemetry.fetch_json(raw_url)
        except RuntimeError as exc:
            assert "too large" in str(exc)
        else:
            raise AssertionError("oversize live-verification response accepted")
        assert oversized.read_limit is None

        secret_url = "https://provider.example/path-secret?api_key=query-secret"
        error = telemetry.urllib.error.HTTPError(secret_url, 401, "reason-secret", {}, None)
        telemetry.LIVE_STATUS_OPENER = Opener(error)
        try:
            telemetry.fetch_json(raw_url)
        except RuntimeError as exc:
            assert str(exc) == "live verification HTTP 401"
            assert "secret" not in str(exc)
        else:
            raise AssertionError("live-verification HTTP error was accepted")

        redirect = telemetry.urllib.error.HTTPError(
            raw_url,
            302,
            "Found",
            {"Location": pages_url},
            None,
        )
        telemetry.LIVE_STATUS_OPENER = Opener(redirect)
        try:
            telemetry.fetch_json(raw_url)
        except RuntimeError as exc:
            assert str(exc) == "live verification HTTP 302"
        else:
            raise AssertionError("live-verification redirect was followed")
    finally:
        telemetry.LIVE_STATUS_OPENER = original_opener


def test_refresh_status_validation_rejects_stale_required_fields() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        status = telemetry.write_refresh_status(env, root=root, prefer_current_env=True)

        broken = dict(status)
        broken["current_high_bidder_wallet"] = "0x0000000000000000000000000000000000000001"
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "current_high_bidder_wallet")

        broken = dict(status)
        broken["last_refresh_result"] = "failed"
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "last_refresh_result")

        broken = dict(status)
        broken["last_refresh_result"] = "success_pushed_live_timeout"
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "last_refresh_result")

        broken = dict(status)
        broken.pop("refresh_reason")
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "missing required fields")

        broken = dict(status)
        broken["dog_token_uri_unavailable_count"] = 1
        write_json(root / "generated" / "refresh_status.json", broken)
        write_json(root / "public" / "generated" / "refresh_status.json", broken)
        assert_raises_contains(lambda: telemetry.validate_refresh_status(root=root), "tokenURI aggregate counts")


def test_metrics_summary_includes_pending_metadata_and_speed_percentiles() -> None:
    telemetry = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        env = base_env(root)
        telemetry.record_refresh(env, result="success_pushed", root=root)
        watcher_row = {
            "schema_version": 1,
            "kind": "watcher_check",
            "started_at_utc": iso(20),
            "completed_at_utc": iso(21),
            "duration_seconds": 1,
            "result": "cooldown_skip",
            "reasons": ["auction_bid"],
            "pending_refresh": True,
        }
        telemetry.record_watcher_check(watcher_row, env=env, root=root)
        write_json(
            root / ".local" / "mission3_onchain_tracker_state.json",
            {
                "pending_refresh": True,
                "pending_refresh_reasons": ["auction_bid"],
                "next_allowed_refresh_after_utc": iso(300),
            },
        )
        summary = telemetry.metrics_summary(env, root=root)
        assert summary["pending_refresh"] is True
        assert summary["pending_refresh_reasons"] == ["auction_bid"]
        assert summary["watcher_check_average_seconds_24h"] == 1
        assert summary["refresh_p95_seconds_24h"] is not None
        assert summary["last_refresh_result"] == "success_pushed"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"refresh_telemetry_tests=pass count={len(tests)}")
