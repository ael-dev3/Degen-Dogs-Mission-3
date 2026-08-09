#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "watch_mission3_auction.py"
ARCHIVE_MODULE_PATH = ROOT / "scripts" / "archive_mission3_index.py"
ARCHIVE_HEALTH_MODULE_PATH = ROOT / "scripts" / "check_mission3_archive.py"
TELEMETRY_MODULE_PATH = ROOT / "scripts" / "refresh_telemetry.py"


def load_module():
    spec = importlib.util.spec_from_file_location("watch_mission3_auction", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Individual watcher tests should never append to the repository's live
    # telemetry stream. The dedicated telemetry test enables a temporary sink.
    module.refresh_telemetry = None
    return module


def load_archive_module():
    spec = importlib.util.spec_from_file_location("archive_mission3_index", ARCHIVE_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_archive_health_module():
    spec = importlib.util.spec_from_file_location("check_mission3_archive", ARCHIVE_HEALTH_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_watcher_state_reader_rejects_symlinks_and_broad_permissions():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        broad = root / "broad.json"
        broad.write_text("{}", encoding="utf-8")
        broad.chmod(0o644)
        try:
            watcher.load_state(broad)
        except SystemExit as exc:
            assert "mode 600" in str(exc)
        else:
            raise AssertionError("broad watcher state permissions were accepted")

        target = root / "target.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o600)
        linked = root / "linked.json"
        linked.symlink_to(target)
        try:
            watcher.load_state(linked)
        except SystemExit as exc:
            assert "securely open watcher state" in str(exc)
        else:
            raise AssertionError("symlinked watcher state was accepted")


def load_telemetry_module():
    spec = importlib.util.spec_from_file_location("refresh_telemetry", TELEMETRY_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def word(value: int) -> str:
    return f"{value:064x}"


class FakeRpcResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "application/json",
        status_code: int = 200,
        final_url: str = "",
    ) -> None:
        self.body = body
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.status_code = status_code
        self.final_url = final_url
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status_code

    def geturl(self) -> str:
        return self.final_url

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body[:limit]


def install_fake_rpc_response(watcher, response: FakeRpcResponse) -> None:
    def fake_open(request, timeout):  # noqa: ANN001, ANN202, ARG001
        if not response.final_url:
            response.final_url = request.full_url
        return response

    watcher.open_rpc_request = fake_open


def test_watcher_rpc_transport_accepts_exact_bounded_json_response():
    watcher = load_module()
    url = "https://provider.example/v2/provider-key?network=base"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x1"}).encode()
    response = FakeRpcResponse(body, content_type="application/json; charset=utf-8")
    install_fake_rpc_response(watcher, response)

    assert watcher.post_json(url, {"jsonrpc": "2.0"})["result"] == "0x1"
    assert response.read_limit == watcher.RPC_MAX_RESPONSE_BYTES + 1


def test_watcher_rpc_transport_rejects_unsafe_urls_before_network():
    watcher = load_module()

    def should_not_open(*_args, **_kwargs):
        raise AssertionError("unsafe RPC URL reached the network")

    watcher.open_rpc_request = should_not_open
    urls = (
        "http://provider.example/rpc",
        "https://user:super-secret@provider.example/rpc",
        "https://provider.example:8443/rpc",
        "https://provider.example/rpc#fragment",
    )
    for url in urls:
        try:
            watcher.post_json(url, {})
        except RuntimeError as exc:
            assert "super-secret" not in str(exc)
        else:
            raise AssertionError(f"unsafe RPC URL was accepted: {url}")


def test_watcher_rpc_transport_rejects_redirect_status_mime_and_oversize():
    watcher = load_module()
    url = "https://provider.example/rpc"
    assert watcher.NoRedirectHandler().redirect_request(None, None, 302, "", {}, "https://attacker.example") is None
    cases = [
        (FakeRpcResponse(b"{}", final_url="https://attacker.example/rpc"), "URL changed"),
        (FakeRpcResponse(b"{}", status_code=206), "HTTP status"),
        (FakeRpcResponse(b"{}", content_type="text/html"), "Content-Type"),
    ]
    for response, expected_error in cases:
        install_fake_rpc_response(watcher, response)
        try:
            watcher.post_json(url, {})
        except RuntimeError as exc:
            assert expected_error in str(exc)
        else:
            raise AssertionError(f"unsafe RPC response was accepted: {expected_error}")

    declared = FakeRpcResponse(b"{}")
    declared.headers["Content-Length"] = str(watcher.RPC_MAX_RESPONSE_BYTES + 1)
    install_fake_rpc_response(watcher, declared)
    try:
        watcher.post_json(url, {})
    except RuntimeError as exc:
        assert "byte limit" in str(exc)
    else:
        raise AssertionError("oversize declared RPC response was accepted")
    assert declared.read_limit is None

    streamed = FakeRpcResponse(b" " * (watcher.RPC_MAX_RESPONSE_BYTES + 1))
    streamed.headers.pop("Content-Length")
    install_fake_rpc_response(watcher, streamed)
    try:
        watcher.post_json(url, {})
    except RuntimeError as exc:
        assert "byte limit" in str(exc)
    else:
        raise AssertionError("oversize streamed RPC response was accepted")


def test_watcher_rpc_transport_and_envelope_errors_do_not_leak_provider_secrets():
    watcher = load_module()
    secret_url = "https://provider.example/v2/path-secret?api_key=query-secret"

    def fail_open(_request, _timeout):
        raise watcher.urllib.error.HTTPError(secret_url, 401, "reason-secret", {}, None)

    watcher.open_rpc_request = fail_open
    try:
        watcher.post_json(secret_url, {})
    except RuntimeError as exc:
        message = str(exc)
        assert message == "RPC HTTP 401"
        assert "secret" not in message
    else:
        raise AssertionError("RPC HTTP failure was accepted")

    original = watcher.post_json
    try:
        watcher.post_json = lambda *_args, **_kwargs: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "body-secret", "data": "data-secret"},
        }
        try:
            watcher.rpc_call("eth_test", [], urls=[secret_url])
        except RuntimeError as exc:
            message = str(exc)
            assert "code=-32601" in message
            assert "body-secret" not in message
            assert "data-secret" not in message
            assert "path-secret" not in message
            assert "query-secret" not in message
        else:
            raise AssertionError("JSON-RPC error response was accepted")

        malformed_envelopes = (
            [],
            {"jsonrpc": "2.0", "id": True, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": "0x1", "error": None},
        )
        for envelope in malformed_envelopes:
            watcher.post_json = lambda *_args, _envelope=envelope, **_kwargs: _envelope
            try:
                watcher.rpc_call("eth_test", [], urls=["https://provider.example/rpc"])
            except RuntimeError as exc:
                assert "envelope" in str(exc)
            else:
                raise AssertionError(f"malformed JSON-RPC envelope was accepted: {envelope!r}")
    finally:
        watcher.post_json = original


def test_rpc_quorum_rejects_two_by_two_tie():
    watcher = load_module()
    original = watcher.rpc_call_with_retry
    answers = {
        "https://one.example": "0x1",
        "https://two.example": "0x1",
        "https://three.example": "0x2",
        "https://four.example": "0x2",
    }

    def fake_call(_method, _params, *, url, timeout=30):  # noqa: ARG001
        return answers[url], url

    try:
        watcher.rpc_call_with_retry = fake_call
        try:
            watcher.rpc_quorum_call("eth_call", [], urls=list(answers), required=2)
        except RuntimeError as exc:
            assert "votes=[2, 2]" in str(exc)
        else:
            raise AssertionError("watcher accepted a 2-2 provider tie")
    finally:
        watcher.rpc_call_with_retry = original


def test_block_quorum_ignores_optional_provider_fields_but_enforces_header():
    watcher = load_module()
    original = watcher.rpc_call_with_retry
    urls = ["https://one.example", "https://two.example"]
    canonical = {
        "number": "0x64",
        "hash": "0x" + "a" * 64,
        "parentHash": "0x" + "b" * 64,
        "timestamp": "0x123",
        "stateRoot": "0x" + "c" * 64,
        "transactionsRoot": "0x" + "d" * 64,
        "receiptsRoot": "0x" + "e" * 64,
        "uncles": [],
        "withdrawals": [],
    }
    provider_variant = dict(canonical)
    provider_variant.pop("uncles")
    provider_variant.pop("withdrawals")
    answers = {urls[0]: canonical, urls[1]: provider_variant}

    def fake_call(_method, _params, *, url, timeout=30):  # noqa: ARG001
        return answers[url], url

    try:
        watcher.rpc_call_with_retry = fake_call
        value, agreeing = watcher.rpc_quorum_call(
            "eth_getBlockByNumber", ["0x64", False], urls=urls, required=2
        )
        assert value["hash"] == canonical["hash"]
        assert len(agreeing) == 2

        answers[urls[1]] = {**provider_variant, "stateRoot": "0x" + "f" * 64}
        try:
            watcher.rpc_quorum_call(
                "eth_getBlockByNumber", ["0x64", False], urls=urls, required=2
            )
        except RuntimeError as exc:
            assert "votes=[1, 1]" in str(exc)
        else:
            raise AssertionError("watcher accepted providers that disagreed on the block header")
    finally:
        watcher.rpc_call_with_retry = original


def test_rpc_quorum_returns_without_waiting_for_decisive_straggler():
    watcher = load_module()
    original = watcher.rpc_call_with_retry
    original_deadline = watcher.RPC_QUORUM_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]

    def fake_call(_method, _params, *, url, timeout=30):  # noqa: ARG001
        if url == urls[-1]:
            time.sleep(0.6)
        return "0xcanonical", url

    try:
        watcher.rpc_call_with_retry = fake_call
        watcher.RPC_QUORUM_DEADLINE_SECONDS = 1.0
        started = time.monotonic()
        value, agreeing = watcher.rpc_quorum_call("eth_call", [], urls=urls, required=2)
        elapsed = time.monotonic() - started
    finally:
        watcher.rpc_call_with_retry = original
        watcher.RPC_QUORUM_DEADLINE_SECONDS = original_deadline
        watcher.RPC_SLOW_UNTIL.clear()

    assert value == "0xcanonical"
    assert len(agreeing) == 2
    assert elapsed < 0.25


def test_head_probe_returns_after_minimum_quorum_and_grace():
    watcher = load_module()
    original_grace = watcher.RPC_HEAD_PROBE_GRACE_SECONDS
    original_deadline = watcher.RPC_HEAD_PROBE_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]

    def probe(url):
        if url == urls[-1]:
            time.sleep(0.6)
        return url, 100

    try:
        watcher.RPC_HEAD_PROBE_GRACE_SECONDS = 0.0
        watcher.RPC_HEAD_PROBE_DEADLINE_SECONDS = 1.0
        started = time.monotonic()
        results, _errors = watcher.collect_rpc_probes(urls, required=2, probe=probe, label="test-head")
        elapsed = time.monotonic() - started
    finally:
        watcher.RPC_HEAD_PROBE_GRACE_SECONDS = original_grace
        watcher.RPC_HEAD_PROBE_DEADLINE_SECONDS = original_deadline
        watcher.RPC_SLOW_UNTIL.clear()

    assert len(results) == 2
    assert elapsed < 0.25


def address_word(address: str) -> str:
    return f"{int(address, 16):064x}"


def auction_raw(token_id: int, amount_wei: int, start_ts: int, end_ts: int, bidder: str, settled: int) -> str:
    return "0x" + "".join([
        word(token_id),
        word(amount_wei),
        word(start_ts),
        word(end_ts),
        address_word(bidder),
        word(settled),
    ])


def event_log(
    watcher,
    event_name: str,
    *,
    block: int,
    tx: str,
    index: int,
    token_id: int,
    bidder: str | None = None,
    amount: int | None = None,
    end_time: int | None = None,
    extended: bool = False,
):
    topics = [watcher.TOPIC_BY_EVENT[event_name], "0x" + word(token_id)]
    if event_name == "AuctionCreated":
        data_words = [word(1), word(end_time or 2)]
    elif event_name == "AuctionBid":
        data_words = [
            address_word(bidder or "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
            word(amount or 0),
            word(1 if extended else 0),
        ]
    elif event_name == "AuctionExtended":
        data_words = [word(end_time or 2)]
    elif event_name == "AuctionSettled":
        data_words = [address_word(bidder or "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"), word(amount or 0)]
    else:
        raise AssertionError(event_name)
    return {
        "blockNumber": hex(block),
        "transactionHash": tx,
        "logIndex": hex(index),
        "topics": topics,
        "data": "0x" + "".join(data_words),
    }


def iso(seconds_offset: int = 0) -> str:
    return (
        datetime(2026, 5, 29, tzinfo=timezone.utc) + timedelta(seconds=seconds_offset)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_decode_auction_result_matches_auction_house_struct():
    watcher = load_module()
    bidder = "0x1234567890abcdef1234567890abcdef12345678"
    decoded = watcher.decode_auction_result(auction_raw(727, 11_000_000_000_000_000, 100, 200, bidder, 0), latest_block=123)
    assert decoded["token_id"] == 727
    assert decoded["amount_wei"] == "11000000000000000"
    assert decoded["high_bidder"] == bidder.lower()
    assert decoded["settled"] is False
    assert decoded["latest_block"] == 123


def test_verified_mission3_metadata_is_loaded_and_includes_extended_event():
    watcher = load_module()
    assert watcher.CHAIN_ID == 8453
    assert watcher.AUCTION_HOUSE.lower() == "0x8f34fe11ce28893dea6a802c8d0b3d0ffc7f5cea"
    assert watcher.TOPIC_BY_EVENT["AuctionBid"] == "0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3"
    assert watcher.TOPIC_BY_EVENT["AuctionExtended"] == "0x6e912a3a9105bdd2af817ba5adc14e6c127c1035b5b648faa29ca0d58ab8ff4e"
    assert "AuctionExtended" in watcher.WATCHED_EVENT_NAMES


def test_compact_event_log_decodes_bid_and_extended_payloads():
    watcher = load_module()
    bid = watcher.compact_event_log(
        event_log(
            watcher,
            "AuctionBid",
            block=100,
            tx="0xbid",
            index=3,
            token_id=728,
            bidder="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            amount=200,
            extended=True,
        )
    )
    assert bid["event_name"] == "AuctionBid"
    assert bid["token_id"] == 728
    assert bid["bidder"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert bid["amount_wei"] == "200"
    assert bid["extended"] is True

    extended = watcher.compact_event_log(
        event_log(watcher, "AuctionExtended", block=101, tx="0xext", index=4, token_id=728, end_time=999)
    )
    assert extended["event_name"] == "AuctionExtended"
    assert extended["token_id"] == 728
    assert extended["end_time_unix"] == 999


def test_change_detection_initializes_without_refresh_then_detects_bidder_amount_and_token_changes():
    watcher = load_module()
    snapshot = {
        "latest_block": 100,
        "token_id": 727,
        "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_wei": "100",
        "settled": False,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "91:0xbid:1", "tx_hash": "0xbid"},
        "extended_log": None,
        "settled_log": None,
    }
    state = {}
    decision = watcher.decide_refresh(state, snapshot, now_utc=iso(), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is False
    assert decision.reasons == ["initialize_state"]

    changed = dict(snapshot)
    changed.update({
        "latest_block": 110,
        "token_id": 728,
        "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "amount_wei": "200",
        "created_log": {"id": "109:0xcreated2:2", "tx_hash": "0xcreated2"},
        "bid_log": {"id": "110:0xbid2:3", "tx_hash": "0xbid2"},
    })
    previous = watcher.state_from_snapshot(snapshot, now_utc=iso(), previous_state={})
    decision = watcher.decide_refresh(previous, changed, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert "auction_created" in decision.reasons
    assert "auction_bid" in decision.reasons
    assert "current_auction_token_changed" in decision.reasons
    assert "highest_bidder_changed" in decision.reasons
    assert "highest_bid_amount_changed" in decision.reasons


def test_new_bid_log_event_triggers_refresh_even_when_contract_snapshot_is_unchanged():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 728,
        "last_seen_high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "last_seen_amount_wei": "200",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xoldbid:2",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
    }
    snapshot = {
        "latest_block": 110,
        "token_id": 728,
        "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "amount_wei": "200",
        "settled": False,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "110:0xnewbid:4", "tx_hash": "0xnewbid", "log_index": 4, "token_id": 728},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert decision.reasons == ["auction_bid"]


def test_already_seen_bid_log_does_not_duplicate_refresh():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 728,
        "last_seen_high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "last_seen_amount_wei": "200",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "110:0xnewbid:4",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
    }
    snapshot = {
        "latest_block": 111,
        "token_id": 728,
        "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "amount_wei": "200",
        "settled": False,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "110:0xnewbid:4", "tx_hash": "0xnewbid", "log_index": 4, "token_id": 728},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is False
    assert decision.reasons == []


def test_new_extended_log_triggers_after_cooldown_and_is_deferred_inside_cooldown():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 728,
        "last_seen_high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "last_seen_amount_wei": "200",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:2",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
    }
    snapshot = {
        "latest_block": 120,
        "token_id": 728,
        "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "amount_wei": "200",
        "settled": False,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "100:0xbid:2", "tx_hash": "0xbid"},
        "extended_log": {"id": "119:0xextended:5", "tx_hash": "0xextended", "log_index": 5, "token_id": 728},
        "settled_log": None,
    }
    early = watcher.decide_refresh(previous, snapshot, now_utc=iso(120), cooldown_seconds=300, force_after_seconds=0)
    assert early.should_refresh is False
    assert early.cooldown_skip is True
    assert early.pending_refresh is True
    assert early.reasons == ["auction_extended"]

    later = watcher.decide_refresh(previous, snapshot, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
    assert later.should_refresh is True
    assert later.reasons == ["auction_extended"]


def test_same_dog_bid_changes_can_use_short_bid_cooldown():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 728,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
        "last_seen_end_time_unix": 200,
    }
    snapshot = {
        "latest_block": 130,
        "token_id": 728,
        "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "amount_wei": "200",
        "settled": False,
        "end_time_unix": 200,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "130:0xbid2:2", "tx_hash": "0xbid2"},
        "extended_log": None,
        "settled_log": None,
    }
    early = watcher.decide_refresh(
        previous,
        snapshot,
        now_utc=iso(30),
        cooldown_seconds=300,
        bid_cooldown_seconds=60,
        force_after_seconds=0,
    )
    assert early.should_refresh is False
    assert early.pending_refresh is True
    later = watcher.decide_refresh(
        previous,
        snapshot,
        now_utc=iso(75),
        cooldown_seconds=300,
        bid_cooldown_seconds=60,
        force_after_seconds=0,
    )
    assert later.should_refresh is True
    assert "auction_bid" in later.reasons
    assert "highest_bidder_changed" in later.reasons
    assert "highest_bid_amount_changed" in later.reasons


def test_auction_start_time_change_triggers_refresh_when_direct_state_changes():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 728,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
        "last_seen_start_time_unix": 100,
        "last_seen_end_time_unix": 200,
    }
    snapshot = {
        "latest_block": 140,
        "token_id": 728,
        "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_wei": "100",
        "settled": False,
        "start_time_unix": 120,
        "end_time_unix": 200,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "100:0xbid:1", "tx_hash": "0xbid"},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert decision.reasons == ["auction_start_time_changed"]


def test_auction_end_time_change_triggers_refresh_when_extension_log_is_missed():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 728,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
        "last_seen_end_time_unix": 200,
    }
    snapshot = {
        "latest_block": 140,
        "token_id": 728,
        "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_wei": "100",
        "settled": False,
        "end_time_unix": 260,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "100:0xbid:1", "tx_hash": "0xbid"},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert decision.reasons == ["auction_end_time_changed"]


def test_new_created_log_bypasses_cooldown():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 727,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
        "last_seen_end_time_unix": 200,
    }
    snapshot = {
        "latest_block": 141,
        "token_id": 728,
        "high_bidder": "0x0000000000000000000000000000000000000000",
        "amount_wei": "0",
        "settled": False,
        "end_time_unix": 500,
        "created_log": {"id": "141:0xcreated2:1", "tx_hash": "0xcreated2"},
        "bid_log": {"id": "100:0xbid:1", "tx_hash": "0xbid"},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(30), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert decision.bypassed_cooldown is True
    assert "auction_created" in decision.reasons
    assert "current_auction_token_changed" in decision.reasons


def test_bid_change_inside_cooldown_is_deferred_not_lost():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 727,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
    }
    snapshot = {
        "latest_block": 101,
        "token_id": 727,
        "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_wei": "200",
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "101:0xbid2:2", "tx_hash": "0xbid2"},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(120), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is False
    assert decision.cooldown_skip is True
    assert decision.pending_refresh is True

    deferred_state = watcher.state_from_snapshot(snapshot, now_utc=iso(120), previous_state=previous, decision=decision)
    decision2 = watcher.decide_refresh(deferred_state, snapshot, now_utc=iso(360), cooldown_seconds=300, force_after_seconds=0)
    assert decision2.should_refresh is True
    assert "pending_refresh_after_cooldown" in decision2.reasons


def test_cooldown_defer_keeps_unpublished_bid_pending_without_advancing_seen_cursor():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        original_state = {
            "last_seen_token_id": 737,
            "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "last_seen_amount_wei": "12900000000000000",
            "last_seen_bid_log_id": "100:0xoldbid:1",
            "last_seen_bid_tx": "0xoldbid",
            "last_seen_auction_created_log_id": "90:0xcreated:1",
            "last_seen_auction_settled_log_id": "",
            "last_seen_auction_extended_log_id": "",
            "last_refresh_at_utc": watcher.utc_now(),
        }
        state_path.write_text(json.dumps(original_state, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        snapshot = {
            "latest_block": 130,
            "checked_from_block": 100,
            "checked_to_block": 130,
            "token_id": 737,
            "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "amount_wei": "30000000000000000",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated", "block_number": 90, "log_index": 1, "event_name": "AuctionCreated"},
            "bid_log": {"id": "130:0xnewbid:4", "tx_hash": "0xnewbid", "block_number": 130, "log_index": 4, "event_name": "AuctionBid", "token_id": 737, "amount_wei": "30000000000000000", "bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            "extended_log": None,
            "settled_log": None,
        }
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(Path(tmp) / "watcher.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_COOLDOWN_SECONDS": "300",
            "MISSION3_WATCHER_BID_COOLDOWN_SECONDS": "60",
        })
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        assert watcher.run_once(config) == 0
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["pending_refresh"] is True
        assert saved["pending_bid_log_id"] == "130:0xnewbid:4"
        assert saved["pending_amount_wei"] == "30000000000000000"
        assert saved["pending_high_bidder"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        assert saved["last_observed_amount_wei"] == "30000000000000000"
        assert saved["last_observed_high_bidder"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        assert saved["last_seen_amount_wei"] == original_state["last_seen_amount_wei"]
        assert saved["last_seen_high_bidder"] == original_state["last_seen_high_bidder"]
        assert saved["last_seen_bid_log_id"] == original_state["last_seen_bid_log_id"]


def test_refresh_failure_keeps_unpublished_bid_pending_without_advancing_seen_cursor():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        original_state = {
            "last_seen_token_id": 737,
            "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "last_seen_amount_wei": "12900000000000000",
            "last_seen_bid_log_id": "100:0xoldbid:1",
            "last_seen_bid_tx": "0xoldbid",
            "last_seen_auction_created_log_id": "90:0xcreated:1",
            "last_seen_auction_settled_log_id": "",
            "last_seen_auction_extended_log_id": "",
            "last_refresh_at_utc": iso(0),
        }
        state_path.write_text(json.dumps(original_state, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        snapshot = {
            "latest_block": 130,
            "checked_from_block": 100,
            "checked_to_block": 130,
            "token_id": 737,
            "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "amount_wei": "30000000000000000",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated", "block_number": 90, "log_index": 1, "event_name": "AuctionCreated"},
            "bid_log": {"id": "130:0xnewbid:4", "tx_hash": "0xnewbid", "block_number": 130, "log_index": 4, "event_name": "AuctionBid", "token_id": 737, "amount_wei": "30000000000000000", "bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            "extended_log": None,
            "settled_log": None,
        }
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(Path(tmp) / "watcher.lock"),
            "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_COOLDOWN_SECONDS": "300",
            "MISSION3_WATCHER_BID_COOLDOWN_SECONDS": "60",
        })
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        original_run_refresh = watcher.run_refresh
        watcher.run_refresh = lambda _config, _reasons, dry_run, event=None: ("failure", 1)
        try:
            assert watcher.run_once(config) == 2
        finally:
            watcher.run_refresh = original_run_refresh
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["pending_refresh"] is True
        assert saved["pending_bid_log_id"] == "130:0xnewbid:4"
        assert saved["pending_amount_wei"] == "30000000000000000"
        assert saved["pending_event_tx_hash"] == "0xnewbid"
        assert saved["last_observed_amount_wei"] == "30000000000000000"
        assert saved["last_seen_amount_wei"] == original_state["last_seen_amount_wei"]
        assert saved["last_seen_high_bidder"] == original_state["last_seen_high_bidder"]
        assert saved["last_seen_bid_log_id"] == original_state["last_seen_bid_log_id"]


def test_guarded_dirty_tree_refresh_refusal_keeps_unpublished_bid_pending_without_advancing_seen_cursor():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        original_state = {
            "last_seen_token_id": 737,
            "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "last_seen_amount_wei": "12900000000000000",
            "last_seen_bid_log_id": "100:0xoldbid:1",
            "last_seen_bid_tx": "0xoldbid",
            "last_seen_auction_created_log_id": "90:0xcreated:1",
            "last_seen_auction_settled_log_id": "",
            "last_seen_auction_extended_log_id": "",
            "last_refresh_at_utc": iso(0),
        }
        state_path.write_text(json.dumps(original_state, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        snapshot = {
            "latest_block": 130,
            "checked_from_block": 100,
            "checked_to_block": 130,
            "token_id": 737,
            "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "amount_wei": "30000000000000000",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated", "block_number": 90, "log_index": 1, "event_name": "AuctionCreated"},
            "bid_log": {"id": "130:0xnewbid:4", "tx_hash": "0xnewbid", "block_number": 130, "log_index": 4, "event_name": "AuctionBid", "token_id": 737, "amount_wei": "30000000000000000", "bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            "extended_log": None,
            "settled_log": None,
        }
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(Path(tmp) / "watcher.lock"),
            "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_REQUIRE_CLEAN_TREE": "1",
            "MISSION3_WATCHER_COOLDOWN_SECONDS": "300",
            "MISSION3_WATCHER_BID_COOLDOWN_SECONDS": "60",
        })
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        setattr(watcher, "git_status_tracked", lambda: " M generated/current_auction.json\n")
        assert watcher.run_once(config) == 2
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["pending_refresh"] is True
        assert saved["pending_bid_log_id"] == "130:0xnewbid:4"
        assert saved["pending_amount_wei"] == "30000000000000000"
        assert saved["last_refresh_status"] == "failure"
        assert "tracked working tree changes exist" in saved["last_refresh_error"]
        assert saved["last_observed_amount_wei"] == "30000000000000000"
        assert saved["last_seen_amount_wei"] == original_state["last_seen_amount_wei"]
        assert saved["last_seen_high_bidder"] == original_state["last_seen_high_bidder"]
        assert saved["last_seen_bid_log_id"] == original_state["last_seen_bid_log_id"]
        reacquired = watcher.acquire_refresh_lock(config)
        assert reacquired is not None
        watcher.release_run_lock(reacquired)


def test_refresh_success_acknowledges_pending_bid_and_clears_pending_identity():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        state_path.write_text(json.dumps({
            "last_seen_token_id": 737,
            "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "last_seen_amount_wei": "12900000000000000",
            "last_seen_bid_log_id": "100:0xoldbid:1",
            "last_seen_auction_created_log_id": "90:0xcreated:1",
            "last_seen_auction_settled_log_id": "",
            "last_seen_auction_extended_log_id": "",
            "last_refresh_at_utc": iso(0),
            "pending_refresh": True,
            "pending_bid_log_id": "130:0xnewbid:4",
        }, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        snapshot = {
            "latest_block": 130,
            "checked_from_block": 100,
            "checked_to_block": 130,
            "token_id": 737,
            "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "amount_wei": "30000000000000000",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated", "block_number": 90, "log_index": 1, "event_name": "AuctionCreated"},
            "bid_log": {"id": "130:0xnewbid:4", "tx_hash": "0xnewbid", "block_number": 130, "log_index": 4, "event_name": "AuctionBid", "token_id": 737, "amount_wei": "30000000000000000", "bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
            "extended_log": None,
            "settled_log": None,
        }
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(Path(tmp) / "watcher.lock"),
            "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_COOLDOWN_SECONDS": "300",
            "MISSION3_WATCHER_BID_COOLDOWN_SECONDS": "60",
        })
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        original_run_refresh = watcher.run_refresh
        watcher.run_refresh = lambda _config, _reasons, dry_run, event=None: ("success", 0)
        try:
            assert watcher.run_once(config) == 0
        finally:
            watcher.run_refresh = original_run_refresh
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["last_seen_amount_wei"] == "30000000000000000"
        assert saved["last_seen_high_bidder"] == "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        assert saved["last_seen_bid_log_id"] == "130:0xnewbid:4"
        assert "pending_refresh" not in saved
        assert "pending_bid_log_id" not in saved


def test_new_settlement_bypasses_cooldown():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 727,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
    }
    snapshot = {
        "latest_block": 120,
        "token_id": 727,
        "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_wei": "100",
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "100:0xbid:1", "tx_hash": "0xbid"},
        "extended_log": None,
        "settled_log": {"id": "119:0xsettled:3", "tx_hash": "0xsettled"},
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(120), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert decision.cooldown_skip is False
    assert decision.bypassed_cooldown is True
    assert decision.reasons == ["auction_settled"]


def test_settlement_state_change_bypasses_cooldown_even_without_log_scan_hit():
    watcher = load_module()
    previous = {
        "last_seen_token_id": 727,
        "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "last_seen_amount_wei": "100",
        "last_seen_settled": False,
        "last_refresh_at_utc": iso(0),
        "last_seen_bid_log_id": "100:0xbid:1",
        "last_seen_auction_created_log_id": "90:0xcreated:1",
        "last_seen_auction_settled_log_id": "",
        "last_seen_auction_extended_log_id": "",
    }
    snapshot = {
        "latest_block": 120,
        "token_id": 727,
        "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "amount_wei": "100",
        "settled": True,
        "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
        "bid_log": {"id": "100:0xbid:1", "tx_hash": "0xbid"},
        "extended_log": None,
        "settled_log": None,
    }
    decision = watcher.decide_refresh(previous, snapshot, now_utc=iso(120), cooldown_seconds=300, force_after_seconds=0)
    assert decision.should_refresh is True
    assert decision.cooldown_skip is False
    assert decision.bypassed_cooldown is True
    assert decision.reasons == ["auction_settled_state_changed"]


def test_redact_url_masks_path_based_rpc_keys():
    watcher = load_module()
    redacted = watcher.redact_url("https://base-mainnet.g.alchemy.com/v2/super-secret-key?apikey=also-secret")
    assert "super-secret-key" not in redacted
    assert "also-secret" not in redacted
    assert redacted == "https://base-mainnet.g.alchemy.com/<redacted-path>?redacted=1"

    infura = watcher.redact_url("https://mainnet.infura.io/v3/infura-secret")
    assert "infura-secret" not in infura
    assert infura.startswith("https://rpc-host-")
    assert infura.endswith("/<redacted-path>")

    public = watcher.redact_url("https://mainnet.base.org")
    assert public == "https://mainnet.base.org"


def test_watcher_custom_rpc_credentials_never_enter_errors_logs_or_state():
    watcher = load_module()
    custom_url = (
        "https://user-secret:password-secret@host-secret.rpc.custom.example/"
        "path-secret?token=query-secret#fragment-secret"
    )
    redacted = watcher.redact_url(custom_url)
    provider = watcher.rpc_provider_key(custom_url)
    assert redacted.startswith("https://rpc-host-")
    assert provider.startswith("rpc-host-")
    uppercase_text = watcher.redact_rpc_text(custom_url.replace("https://", "HTTPS://"))
    assert "host-secret" not in uppercase_text
    assert "path-secret" not in uppercase_text
    scheme_secret_url = custom_url.replace("https://", "api-key-secret://")
    scheme_redacted = watcher.redact_url(scheme_secret_url)
    assert scheme_redacted.startswith("https://rpc-host-")
    scheme_error = watcher.redact_rpc_text(f"provider failure at {scheme_secret_url}")
    assert "api-key-secret" not in scheme_error
    assert "host-secret" not in scheme_error

    original_post_json = watcher.post_json
    watcher.post_json = lambda _url, _payload, timeout=30: (_ for _ in ()).throw(  # noqa: ARG005
        RuntimeError(f"provider failure at {custom_url}")
    )
    try:
        try:
            watcher.rpc_call("eth_chainId", [], urls=[custom_url])
        except RuntimeError as exc:
            error = exc
        else:
            raise AssertionError("custom RPC failure unexpectedly succeeded")
    finally:
        watcher.post_json = original_post_json

    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        log_path = Path(tmp) / "watcher.log"
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": str(log_path),
        })
        watcher.record_rpc_error(state_path, {}, error, watcher.utc_now())
        watcher.log(config, f"rpc_error: {watcher.redact_rpc_text(error)}")
        output = "\n".join(
            [str(error), log_path.read_text(encoding="utf-8"), state_path.read_text(encoding="utf-8")]
        )
    for secret in (
        "user-secret",
        "password-secret",
        "host-secret",
        "path-secret",
        "query-secret",
        "fragment-secret",
        "rpc.custom.example",
    ):
        assert secret not in output


def test_archive_redact_url_masks_path_based_rpc_keys():
    archive = load_archive_module()
    redacted = archive.redact_url(
        "https://base-mainnet.g.alchemy.com/v2/archive-super-secret?apikey=query-secret"
    )
    assert "archive-super-secret" not in redacted
    assert "query-secret" not in redacted
    assert redacted.startswith("https://rpc-host-")
    assert redacted.endswith("/<redacted-path>?redacted=1")

    public = archive.redact_url("https://mainnet.base.org")
    assert "mainnet.base.org" not in public
    assert public.startswith("https://rpc-host-")

    custom = archive.redact_url("https://host-secret.rpc.custom.example/key-secret?token=query-secret")
    assert "host-secret" not in custom
    assert "key-secret" not in custom
    assert "query-secret" not in custom


def test_archive_overlap_purge_removes_orphaned_raw_and_decoded_events():
    archive = load_archive_module()
    conn = sqlite3.connect(":memory:")
    conn.executescript((ROOT / "archive" / "mission3" / "sql" / "schema.sql").read_text(encoding="utf-8"))
    for block in (99, 100, 101):
        tx_hash = "0x" + f"{block:064x}"
        conn.execute(
            """INSERT INTO mission3_raw_logs
            (chain_id,address,block_number,block_hash,transaction_hash,transaction_index,log_index,removed,topic0,data,fetched_at_utc,source_rpc)
            VALUES (8453,'0xabc',?,'0xhash',?,0,0,0,'0xtopic','0x','2026-01-01T00:00:00Z','unit')""",
            (block, tx_hash),
        )
        conn.execute(
            """INSERT INTO mission3_auction_created
            (token_id,start_time,end_time,block_number,transaction_hash,log_index,block_time_utc)
            VALUES (?,1,2,?,?,0,'2026-01-01T00:00:00Z')""",
            (block, block, tx_hash),
        )
        conn.execute(
            """INSERT INTO mission3_current_auction_snapshots
            (snapshot_at_utc,latest_block,token_id,start_time,end_time,highest_bidder,amount_raw,amount_eth,settled,source,confidence)
            VALUES (?,?,?,?,?,'0x0000000000000000000000000000000000000000','0','0',0,'unit','unit')""",
            (f"2026-01-01T00:00:{block - 90:02d}Z", block, block, 1, 2),
        )
    archive.purge_indexed_range(conn, 8453, 100, 101)
    assert [row[0] for row in conn.execute("SELECT block_number FROM mission3_raw_logs ORDER BY block_number")] == [99]
    assert [row[0] for row in conn.execute("SELECT block_number FROM mission3_auction_created ORDER BY block_number")] == [99]
    assert [row[0] for row in conn.execute("SELECT latest_block FROM mission3_current_auction_snapshots ORDER BY latest_block")] == [99]
    conn.close()


def test_archive_log_reads_require_independent_canonical_agreement():
    archive = load_archive_module()
    urls = ["https://one.example", "https://two.example", "https://three.example"]
    canonical = [{
        "address": "0x" + "a" * 40,
        "blockHash": "0x" + "b" * 64,
        "blockNumber": "0x64",
        "data": "0x",
        "logIndex": "0x0",
        "removed": False,
        "topics": ["0x" + "c" * 64],
        "transactionHash": "0x" + "d" * 64,
        "transactionIndex": "0x0",
    }]
    differing = [{**canonical[0], "transactionHash": "0x" + "e" * 64}]
    responses = {urls[0]: canonical, urls[1]: canonical, urls[2]: differing}
    original = archive.rpc_call
    archive.rpc_call = lambda _method, _params, *, urls, timeout=60: (responses[urls[0]], urls[0])
    try:
        result, agreeing = archive.rpc_consensus(
            "eth_getLogs", [{}], urls=urls, normalizer=archive.canonical_logs
        )
        assert result == canonical
        assert set(agreeing) == set(urls[:2])
        responses[urls[1]] = [{**canonical[0], "transactionHash": "0x" + "f" * 64}]
        try:
            archive.rpc_consensus("eth_getLogs", [{}], urls=urls, normalizer=archive.canonical_logs)
        except RuntimeError as exc:
            assert "failed independent quorum" in str(exc)
        else:
            raise AssertionError("archive accepted three disagreeing log responses")
    finally:
        archive.rpc_call = original


def test_archive_safe_head_ignores_a_stale_outlier_and_pins_hash_quorum():
    archive = load_archive_module()
    urls = ["https://one.example", "https://two.example", "https://stale.example"]
    heads = {urls[0]: 1000, urls[1]: 999, urls[2]: 100}
    original_urls = archive.rpc_urls
    original_call = archive.rpc_call
    original_consensus = archive.rpc_consensus
    old_confirmations = os.environ.get("BASE_SNAPSHOT_CONFIRMATIONS")
    old_spread = os.environ.get("BASE_RPC_MAX_HEAD_SPREAD_BLOCKS")
    os.environ["BASE_SNAPSHOT_CONFIRMATIONS"] = "1"
    os.environ["BASE_RPC_MAX_HEAD_SPREAD_BLOCKS"] = "20"
    archive.rpc_urls = lambda: urls
    archive.rpc_call = lambda _method, _params, *, urls, timeout=30: (hex(heads[urls[0]]), urls[0])
    block = {
        "number": hex(998),
        "hash": "0x" + "a" * 64,
        "parentHash": "0x" + "b" * 64,
        "timestamp": hex(int(datetime.now(timezone.utc).timestamp())),
    }
    consensus_urls: list[list[str]] = []

    def fake_consensus(*_args, **kwargs):
        consensus_urls.append(list(kwargs["urls"]))
        return block, list(kwargs["urls"])

    archive.rpc_consensus = fake_consensus
    try:
        assert archive.verified_safe_head() == 998
        assert len(consensus_urls) == 1
        assert set(consensus_urls[0]) == set(urls[:2])
    finally:
        archive.rpc_urls = original_urls
        archive.rpc_call = original_call
        archive.rpc_consensus = original_consensus
        if old_confirmations is None:
            os.environ.pop("BASE_SNAPSHOT_CONFIRMATIONS", None)
        else:
            os.environ["BASE_SNAPSHOT_CONFIRMATIONS"] = old_confirmations
        if old_spread is None:
            os.environ.pop("BASE_RPC_MAX_HEAD_SPREAD_BLOCKS", None)
        else:
            os.environ["BASE_RPC_MAX_HEAD_SPREAD_BLOCKS"] = old_spread


def test_archive_explicit_rpc_lists_only_add_public_fallbacks_when_opted_in():
    archive = load_archive_module()
    keys = ("BASE_RPC_URL", "BASE_RPC_URLS", "BASE_LOG_RPC_URLS", "BASE_INCLUDE_PUBLIC_FALLBACKS")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        os.environ.pop("BASE_RPC_URL", None)
        os.environ["BASE_RPC_URLS"] = "https://paid-one.example,https://paid-two.example"
        os.environ["BASE_LOG_RPC_URLS"] = "https://logs-one.example,https://logs-two.example"
        os.environ["BASE_INCLUDE_PUBLIC_FALLBACKS"] = "0"
        assert archive.rpc_urls() == ["https://paid-one.example", "https://paid-two.example"]
        assert archive.log_rpc_urls() == ["https://logs-one.example", "https://logs-two.example"]

        os.environ["BASE_INCLUDE_PUBLIC_FALLBACKS"] = "1"
        assert archive.rpc_urls()[:2] == ["https://paid-one.example", "https://paid-two.example"]
        assert archive.rpc_urls()[2:] == archive.DEFAULT_RPC_URLS
        assert archive.log_rpc_urls()[:2] == ["https://logs-one.example", "https://logs-two.example"]
        assert archive.log_rpc_urls()[2:] == archive.DEFAULT_LOG_RPC_URLS
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_archive_provider_aliases_cannot_double_vote():
    archive = load_archive_module()
    selected = archive.independent_rpc_urls([
        "https://base.gateway.tenderly.co",
        "https://alternate.tenderly.co/key",
        "https://base.lava.build",
        "https://backup.lava.build/key",
        "https://mainnet.base.org",
        "https://developer-access-mainnet.base.org",
    ])
    assert selected == [
        "https://base.gateway.tenderly.co",
        "https://base.lava.build",
        "https://mainnet.base.org",
    ]


def test_archive_rpc_quorum_rejects_two_by_two_tie():
    archive = load_archive_module()
    urls = [f"https://provider-{index}.example" for index in range(4)]
    responses = {urls[0]: "A", urls[1]: "A", urls[2]: "B", urls[3]: "B"}
    original = archive.rpc_call
    old_attempts = os.environ.get("BASE_RPC_ATTEMPTS")
    os.environ["BASE_RPC_ATTEMPTS"] = "1"
    archive.rpc_call = lambda _method, _params, *, urls, timeout=60: (responses[urls[0]], urls[0])
    try:
        try:
            archive.rpc_consensus("unit", [], urls=urls)
        except RuntimeError as exc:
            assert "ambiguous_or_incomplete_top_vote" in str(exc)
        else:
            raise AssertionError("archive accepted an ambiguous two-by-two provider split")
    finally:
        archive.rpc_call = original
        if old_attempts is None:
            os.environ.pop("BASE_RPC_ATTEMPTS", None)
        else:
            os.environ["BASE_RPC_ATTEMPTS"] = old_attempts


def test_archive_rpc_quorum_returns_before_non_decisive_straggler():
    archive = load_archive_module()
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]
    original = archive.rpc_call
    old_attempts = os.environ.get("BASE_RPC_ATTEMPTS")
    old_deadline = os.environ.get("BASE_RPC_QUORUM_DEADLINE_SECONDS")
    os.environ["BASE_RPC_ATTEMPTS"] = "1"
    os.environ["BASE_RPC_QUORUM_DEADLINE_SECONDS"] = "2"

    def fake_call(_method, _params, *, urls, timeout=60):  # noqa: ARG001
        if urls[0] == "https://slow.example":
            time.sleep(0.6)
            return "different", urls[0]
        return "canonical", urls[0]

    archive.rpc_call = fake_call
    started = time.monotonic()
    try:
        result, agreeing = archive.rpc_consensus("unit", [], urls=urls)
        elapsed = time.monotonic() - started
        assert result == "canonical"
        assert set(agreeing) == set(urls[:2])
        assert elapsed < 0.4, elapsed
    finally:
        archive.rpc_call = original
        if old_attempts is None:
            os.environ.pop("BASE_RPC_ATTEMPTS", None)
        else:
            os.environ["BASE_RPC_ATTEMPTS"] = old_attempts
        if old_deadline is None:
            os.environ.pop("BASE_RPC_QUORUM_DEADLINE_SECONDS", None)
        else:
            os.environ["BASE_RPC_QUORUM_DEADLINE_SECONDS"] = old_deadline


def test_archive_log_scheduler_cancels_pending_ranges_on_first_failure():
    archive = load_archive_module()
    original = archive.fetch_log_range
    started: list[int] = []

    def fake_fetch(_address, _topics, lo, hi, _urls):
        started.append(lo)
        if lo == 0:
            raise RuntimeError("forced chunk failure")
        time.sleep(0.25)
        return (lo, hi), [], "unit"

    archive.fetch_log_range = fake_fetch
    try:
        try:
            archive.fetch_logs(
                "0x" + "a" * 40,
                ["0x" + "b" * 64],
                0,
                99,
                chunk_size=10,
                workers=2,
                urls=["https://one.example", "https://two.example"],
            )
        except RuntimeError as exc:
            assert "forced chunk failure" in str(exc)
        else:
            raise AssertionError("archive continued after a failed log chunk")
        assert set(started).issubset({0, 10})
    finally:
        archive.fetch_log_range = original


def test_archive_log_normalizer_requires_complete_transaction_identity():
    archive = load_archive_module()
    row = {
        "address": "0x" + "a" * 40,
        "blockHash": "0x" + "b" * 64,
        "blockNumber": "0x64",
        "data": "0x",
        "logIndex": "0x0",
        "removed": False,
        "topics": ["0x" + "c" * 64],
        "transactionHash": "0x" + "d" * 64,
    }
    try:
        archive.canonical_logs([row])
    except RuntimeError as exc:
        assert "transaction index" in str(exc)
    else:
        raise AssertionError("archive accepted a log without transactionIndex")


def test_archive_decoder_rejects_log_hash_from_another_fork():
    archive = load_archive_module()
    conn = sqlite3.connect(":memory:")
    conn.executescript((ROOT / "archive" / "mission3" / "sql" / "schema.sql").read_text(encoding="utf-8"))
    original_fetch = archive.fetch_canonical_blocks
    archive.fetch_canonical_blocks = lambda _blocks, **_kwargs: {
        100: {
            "number": "0x64",
            "hash": "0x" + "a" * 64,
            "parentHash": "0x" + "b" * 64,
            "timestamp": "0x1",
        }
    }
    logs = [{
        "blockNumber": "0x64",
        "blockHash": "0x" + "c" * 64,
        "transactionHash": "0x" + "d" * 64,
        "logIndex": "0x0",
        "topics": ["0x" + "e" * 64, "0x" + "0" * 63 + "1"],
        "data": "0x" + "0" * 128,
    }]
    try:
        try:
            archive.decode_and_insert(conn, logs, {"AuctionCreated": logs[0]["topics"][0]})
        except RuntimeError as exc:
            assert "block hash disagrees" in str(exc)
        else:
            raise AssertionError("archive decoded a log from a non-canonical block hash")
    finally:
        archive.fetch_canonical_blocks = original_fetch
        conn.close()


def test_archive_incremental_window_purges_tail_after_safe_head_regression():
    archive = load_archive_module()
    assert archive.incremental_reorg_window(400, 1000, 950, 100) == (851, 1000)
    assert archive.incremental_reorg_window(400, 1000, 1100, 100) == (901, 1100)
    assert archive.incremental_reorg_window(400, None, 950, 100) == (400, 950)


def test_archive_log_chunk_honors_documented_bounded_override():
    archive = load_archive_module()
    original = os.environ.get("MISSION3_LOG_CHUNK")
    try:
        os.environ.pop("MISSION3_LOG_CHUNK", None)
        assert archive.configured_log_chunk_size() == 2000
        os.environ["MISSION3_LOG_CHUNK"] = "10000"
        assert archive.configured_log_chunk_size() == 10000
        os.environ["MISSION3_LOG_CHUNK"] = "999999"
        assert archive.configured_log_chunk_size() == 10000
        os.environ["MISSION3_LOG_CHUNK"] = "0"
        assert archive.configured_log_chunk_size() == 1
    finally:
        if original is None:
            os.environ.pop("MISSION3_LOG_CHUNK", None)
        else:
            os.environ["MISSION3_LOG_CHUNK"] = original


def test_archive_full_refresh_failure_preserves_last_known_good_database():
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "archive.sqlite"
        original_db = sqlite3.connect(db_path)
        original_db.execute("CREATE TABLE last_known_good (value TEXT NOT NULL)")
        original_db.execute("INSERT INTO last_known_good VALUES ('preserved')")
        original_db.commit()
        original_db.close()

        block_number = int(archive.load_configs()["blocks"]["indexing"]["verified_from_block"])
        snapshot = {
            "number": hex(block_number),
            "hash": "0x" + "a" * 64,
            "parentHash": "0x" + "b" * 64,
            "timestamp": hex(int(datetime.now(timezone.utc).timestamp())),
        }
        originals = {
            "verify_config": archive.verify_config,
            "verified_block_snapshot": archive.verified_block_snapshot,
            "fetch_logs": archive.fetch_logs,
        }
        archive.verify_config = lambda **_kwargs: {"status": "ok"}
        archive.verified_block_snapshot = lambda _block, **_kwargs: (
            snapshot,
            ["https://one.example", "https://two.example"],
        )
        archive.fetch_logs = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced fetch failure"))
        args = archive.argparse.Namespace(
            verify_only=False,
            full_refresh=True,
            incremental=False,
            from_block=block_number,
            to_block=str(block_number),
            db_path=str(db_path),
            output_dir=str(tmp_path / "generated"),
            write_public=False,
            skip_rpc_check=False,
        )
        try:
            try:
                archive.run_index(args)
            except RuntimeError as exc:
                assert "forced fetch failure" in str(exc)
            else:
                raise AssertionError("forced archive full refresh unexpectedly succeeded")
        finally:
            for name, value in originals.items():
                setattr(archive, name, value)

        preserved = sqlite3.connect(db_path)
        try:
            assert preserved.execute("SELECT value FROM last_known_good").fetchone()[0] == "preserved"
        finally:
            preserved.close()
        assert not list(tmp_path.glob(".archive.sqlite.refresh-*.tmp"))


def test_archive_full_refresh_atomically_installs_validated_database():
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "archive.sqlite"
        old_db = sqlite3.connect(db_path)
        old_db.execute("CREATE TABLE obsolete (value TEXT)")
        old_db.commit()
        old_db.close()

        block_number = int(archive.load_configs()["blocks"]["indexing"]["verified_from_block"])
        snapshot = {
            "number": hex(block_number),
            "hash": "0x" + "a" * 64,
            "parentHash": "0x" + "b" * 64,
            "timestamp": hex(int(datetime.now(timezone.utc).timestamp())),
        }
        names = (
            "verify_config",
            "verified_block_snapshot",
            "fetch_logs",
            "fetch_current_auction",
            "assert_snapshot_unchanged",
        )
        originals = {name: getattr(archive, name) for name in names}
        archive.verify_config = lambda **_kwargs: {"status": "ok"}
        archive.verified_block_snapshot = lambda _block, **_kwargs: (
            snapshot,
            ["https://one.example", "https://two.example"],
        )
        archive.fetch_logs = lambda *_args, **_kwargs: []
        archive.fetch_current_auction = lambda *_args, **_kwargs: None
        archive.assert_snapshot_unchanged = lambda *_args, **_kwargs: None
        args = archive.argparse.Namespace(
            verify_only=False,
            full_refresh=True,
            incremental=False,
            from_block=block_number,
            to_block=str(block_number),
            db_path=str(db_path),
            output_dir=str(tmp_path / "generated"),
            write_public=False,
            skip_rpc_check=False,
        )
        try:
            archive.run_index(args)
        finally:
            for name, value in originals.items():
                setattr(archive, name, value)

        installed = sqlite3.connect(db_path)
        try:
            assert installed.execute(
                "SELECT status FROM mission3_index_state WHERE id='mission3'"
            ).fetchone()[0] == "success"
            assert installed.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='obsolete'"
            ).fetchone()[0] == 0
            assert installed.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            installed.close()
        assert (db_path.stat().st_mode & 0o777) == 0o600
        assert (tmp_path / "generated" / "manifest.json").is_file()
        assert (tmp_path / "raw" / "mission3_raw_logs.ndjson").is_file()
        assert not list(tmp_path.glob(".archive.sqlite.refresh-*.tmp"))


def _tree_bytes(path: Path) -> dict[str, bytes]:
    return {
        str(item.relative_to(path)): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def _assert_failed_archive_run_preserves_last_known_good(*, full_refresh: bool, failure: str) -> None:
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "archive.sqlite"
        generated = tmp_path / "generated"
        raw = tmp_path / "raw"
        public = tmp_path / "public"
        for directory in (generated, raw, public):
            directory.mkdir()
        raw.chmod(0o700)
        (generated / "manifest.json").write_text('{"generation":"old"}\n', encoding="utf-8")
        (generated / "artifact.json").write_text('{"value":"old"}\n', encoding="utf-8")
        (raw / ".gitkeep").write_text("", encoding="utf-8")
        (raw / "mission3_raw_logs.ndjson").write_text('{"old":true}\n', encoding="utf-8")
        (public / "archive_manifest.json").write_text('{"generation":"old"}\n', encoding="utf-8")
        (public / "mission3_dog_search_index.json").write_text("[]\n", encoding="utf-8")
        (public / "mission3_archive_metrics.json").write_text('{"old":true}\n', encoding="utf-8")

        block_number = int(archive.load_configs()["blocks"]["indexing"]["verified_from_block"])
        old_conn = archive.init_db(db_path, full_refresh=False)
        old_conn.execute("CREATE TABLE last_known_good (value TEXT NOT NULL)")
        old_conn.execute("INSERT INTO last_known_good VALUES ('preserved')")
        archive.record_state(
            old_conn,
            chain_id=8453,
            auction_house="0x" + "1" * 40,
            from_block=block_number,
            latest_indexed_block=block_number - 1,
            latest_indexed_block_time_utc="2026-08-02T00:00:00Z",
            status="success",
        )
        archive.apply_marts(old_conn)
        old_conn.close()

        old_db = db_path.read_bytes()
        old_generated = _tree_bytes(generated)
        old_raw = _tree_bytes(raw)
        old_public = _tree_bytes(public)
        snapshot = {
            "number": hex(block_number),
            "hash": "0x" + "a" * 64,
            "parentHash": "0x" + "b" * 64,
            "timestamp": hex(int(datetime.now(timezone.utc).timestamp())),
        }
        names = (
            "verify_config",
            "verified_block_snapshot",
            "fetch_logs",
            "fetch_current_auction",
            "assert_snapshot_unchanged",
            "PUBLIC_OUTPUT_DIR",
        )
        originals = {name: getattr(archive, name) for name in names}
        original_copyfile = archive.shutil.copyfile
        copy_count = 0

        def fail_during_public_copy(source, target):
            nonlocal copy_count
            copy_count += 1
            if copy_count == 2:
                raise RuntimeError("forced staged public export failure")
            return original_copyfile(source, target)

        archive.verify_config = lambda **_kwargs: {"status": "ok"}
        archive.verified_block_snapshot = lambda _block, **_kwargs: (
            snapshot,
            ["https://one.example", "https://two.example"],
        )
        archive.fetch_logs = lambda *_args, **_kwargs: []
        archive.fetch_current_auction = lambda *_args, **_kwargs: None
        if failure == "canonical_recheck":
            archive.assert_snapshot_unchanged = lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(RuntimeError("forced post-insert canonical recheck failure"))
        else:
            archive.assert_snapshot_unchanged = lambda *_args, **_kwargs: None
        archive.PUBLIC_OUTPUT_DIR = public
        if failure == "export":
            archive.shutil.copyfile = fail_during_public_copy
        args = archive.argparse.Namespace(
            verify_only=False,
            full_refresh=full_refresh,
            incremental=not full_refresh,
            from_block=block_number,
            to_block=str(block_number),
            db_path=str(db_path),
            output_dir=str(generated),
            write_public=True,
            skip_rpc_check=False,
        )
        try:
            try:
                archive.run_index(args)
            except RuntimeError as exc:
                expected = (
                    "forced staged public export failure"
                    if failure == "export"
                    else "forced post-insert canonical recheck failure"
                )
                assert expected in str(exc)
            else:
                raise AssertionError("failed staged archive export unexpectedly committed")
        finally:
            archive.shutil.copyfile = original_copyfile
            for name, value in originals.items():
                setattr(archive, name, value)

        assert db_path.read_bytes() == old_db
        assert _tree_bytes(generated) == old_generated
        assert _tree_bytes(raw) == old_raw
        assert _tree_bytes(public) == old_public
        preserved = sqlite3.connect(db_path)
        try:
            assert preserved.execute("SELECT value FROM last_known_good").fetchone()[0] == "preserved"
            assert preserved.execute(
                "SELECT status FROM mission3_index_state WHERE id='mission3'"
            ).fetchone()[0] == "success"
        finally:
            preserved.close()
        assert not list(tmp_path.glob(".*.refresh-*.tmp"))
        assert not list(tmp_path.glob(".*.publish-*"))


def test_archive_full_refresh_export_failure_preserves_old_database_and_artifacts():
    _assert_failed_archive_run_preserves_last_known_good(full_refresh=True, failure="export")


def test_archive_incremental_export_failure_preserves_old_database_and_artifacts():
    _assert_failed_archive_run_preserves_last_known_good(full_refresh=False, failure="export")


def test_archive_incremental_canonical_recheck_failure_preserves_last_known_good():
    _assert_failed_archive_run_preserves_last_known_good(
        full_refresh=False,
        failure="canonical_recheck",
    )


def test_archive_atomic_publication_rolls_back_all_targets_after_replace_failure():
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        generated = root / "generated"
        raw = root / "raw"
        public = root / "public"
        database = root / "archive.sqlite"
        for directory in (generated, raw, public):
            directory.mkdir()
        raw.chmod(0o700)
        (generated / "manifest.json").write_text("old-generated\n", encoding="utf-8")
        (generated / "artifact.json").write_text("old-artifact\n", encoding="utf-8")
        (raw / "mission3_raw_logs.ndjson").write_text("old-raw\n", encoding="utf-8")
        (public / "archive_manifest.json").write_text("old-public\n", encoding="utf-8")
        (public / "artifact.json").write_text("old-public-artifact\n", encoding="utf-8")
        database.write_bytes(b"old-database")
        database.chmod(0o600)
        before = {
            "generated": _tree_bytes(generated),
            "raw": _tree_bytes(raw),
            "public": _tree_bytes(public),
            "database": database.read_bytes(),
        }

        generated_stage = archive.create_staging_directory(generated)
        raw_stage = archive.create_staging_directory(raw)
        public_stage = archive.create_staging_directory(public)
        database_stage = root / ".archive.sqlite.refresh-test.tmp"
        database_stage.write_bytes(b"new-database")
        database_stage.chmod(0o600)
        (generated_stage / "manifest.json").write_text("new-generated\n", encoding="utf-8")
        (generated_stage / "artifact.json").write_text("new-artifact\n", encoding="utf-8")
        (raw_stage / "mission3_raw_logs.ndjson").write_text("new-raw\n", encoding="utf-8")
        (public_stage / "archive_manifest.json").write_text("new-public\n", encoding="utf-8")
        (public_stage / "artifact.json").write_text("new-public-artifact\n", encoding="utf-8")
        archive.sync_publication_tree(generated_stage, private=False)
        archive.sync_publication_tree(raw_stage, private=True)
        archive.sync_publication_tree(public_stage, private=False)
        entries = [
            archive.PublicationEntry(raw_stage, raw, directory=True, private=True),
            archive.PublicationEntry(database_stage, database, directory=False, private=True),
            archive.PublicationEntry(generated_stage, generated, directory=True, private=False),
            archive.PublicationEntry(public_stage, public, directory=True, private=False),
        ]
        original_replace = archive.os.replace
        replace_count = 0

        def fail_once(source, target):
            nonlocal replace_count
            replace_count += 1
            if replace_count == 8:
                raise OSError("forced atomic publication replace failure")
            return original_replace(source, target)

        archive.os.replace = fail_once
        try:
            try:
                archive.atomic_publish(entries)
            except OSError as exc:
                assert "forced atomic publication replace failure" in str(exc)
            else:
                raise AssertionError("injected publication failure unexpectedly committed")
        finally:
            archive.os.replace = original_replace

        assert _tree_bytes(generated) == before["generated"]
        assert _tree_bytes(raw) == before["raw"]
        assert _tree_bytes(public) == before["public"]
        assert database.read_bytes() == before["database"]
        assert not list(root.glob(".*.backup-*"))
        for entry in entries:
            archive.remove_owned_path(entry.source)


def test_archive_publication_keeps_public_manifest_and_artifacts_coherent():
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        db_path = root / "archive.sqlite"
        generated = root / "generated"
        public = root / "public"
        old_public_dir = archive.PUBLIC_OUTPUT_DIR
        archive.PUBLIC_OUTPUT_DIR = public
        conn = archive.init_db(db_path, full_refresh=False)
        try:
            archive.record_state(
                conn,
                chain_id=8453,
                auction_house="0x" + "1" * 40,
                from_block=40500000,
                latest_indexed_block=40500000,
                latest_indexed_block_time_utc="2026-08-02T00:00:00Z",
                status="success",
            )
            archive.apply_marts(conn)
            stage = archive.stage_outputs(conn, generated, db_path=db_path, write_public=True)
            try:
                archive.atomic_publish([stage.entries[1], stage.entries[0], *stage.entries[2:]])
            finally:
                stage.cleanup()
        finally:
            conn.close()
            archive.PUBLIC_OUTPUT_DIR = old_public_dir

        private_manifest = json.loads((generated / "manifest.json").read_text(encoding="utf-8"))
        public_manifest = json.loads((public / "archive_manifest.json").read_text(encoding="utf-8"))
        assert public_manifest["generated_at_utc"] == private_manifest["generated_at_utc"]
        assert public_manifest["index_state"] == private_manifest["index_state"]
        for item in public_manifest["files"]:
            filename = Path(item["path"]).name
            assert archive.sha256_file(public / filename) == item["sha256"]
            assert archive.sha256_file(generated / filename) == item["sha256"]
        assert (db_path.stat().st_mode & 0o777) == 0o600
        assert ((root / "raw").stat().st_mode & 0o777) == 0o700
        assert ((root / "raw" / "mission3_raw_logs.ndjson").stat().st_mode & 0o777) == 0o600


def test_archive_publication_rejects_symlinked_targets():
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        real_directory = root / "real-generated"
        real_directory.mkdir()
        linked_directory = root / "generated"
        linked_directory.symlink_to(real_directory, target_is_directory=True)
        try:
            archive.validate_publication_target(linked_directory, directory=True)
        except RuntimeError as exc:
            assert "symlinked publication target" in str(exc)
        else:
            raise AssertionError("archive accepted a symlinked output directory")

        database = root / "real.sqlite"
        database.write_bytes(b"database")
        linked_database = root / "archive.sqlite"
        linked_database.symlink_to(database)
        try:
            archive.validate_publication_target(linked_database, directory=False, private=True)
        except RuntimeError as exc:
            assert "symlinked publication target" in str(exc)
        else:
            raise AssertionError("archive accepted a symlinked database target")


def test_archive_directory_creation_rejects_nested_ancestor_symlink():
    archive = load_archive_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        safe_parent = root / "safe-parent"
        outside = root / "outside"
        safe_parent.mkdir()
        outside.mkdir()
        linked_ancestor = safe_parent / "linked-ancestor"
        linked_ancestor.symlink_to(outside, target_is_directory=True)
        requested = linked_ancestor / "nested" / "generated"
        try:
            archive.secure_directory(requested, create=True)
        except RuntimeError as exc:
            assert "symlink ancestor" in str(exc)
        else:
            raise AssertionError("archive created directories through a nested ancestor symlink")
        assert not (outside / "nested").exists()


def test_archive_mart_uses_latest_extension_as_effective_end_time():
    archive = load_archive_module()
    conn = sqlite3.connect(":memory:")
    conn.executescript((ROOT / "archive" / "mission3" / "sql" / "schema.sql").read_text(encoding="utf-8"))
    conn.execute(
        """INSERT INTO mission3_auction_created
        (token_id,start_time,end_time,block_number,transaction_hash,log_index,block_time_utc)
        VALUES (590,50,100,10,'0xcreated',1,'2026-01-01T00:00:00Z')"""
    )
    conn.executemany(
        """INSERT INTO mission3_auction_extended
        (token_id,end_time,block_number,transaction_hash,log_index,block_time_utc)
        VALUES (590,?,?,?,?,?)""",
        [
            (120, 11, "0xextension1", 2, "2026-01-01T00:00:01Z"),
            (140, 12, "0xextension2", 3, "2026-01-01T00:00:02Z"),
        ],
    )
    archive.apply_marts(conn)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM mission3_auction_timeline WHERE token_id = 590").fetchone()
    assert row is not None
    assert row["initial_end_time"] == 100
    assert row["end_time"] == 140
    assert row["extension_count"] == 2
    assert row["latest_extension_tx"] == "0xextension2"
    conn.close()


def test_archive_health_uses_configured_paths_and_rejects_stale_state():
    health = load_archive_health_module()
    db_path, generated_dir, public_dir = health.configured_paths({
        "MISSION3_ARCHIVE_DB": "/tmp/custom-archive.sqlite",
        "MISSION3_OUTPUT_DIR": "/tmp/custom-generated",
    })
    assert db_path == Path("/tmp/custom-archive.sqlite")
    assert generated_dir == Path("/tmp/custom-generated")
    assert public_dir == ROOT / "public" / "generated" / "mission3"

    errors: list[str] = []
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)
    health.check_timestamp_freshness(
        errors,
        "archive test timestamp",
        "2026-05-28T00:00:00Z",
        max_age_seconds=10_800,
        now=now,
    )
    assert errors and "stale" in errors[0]
    lag_errors: list[str] = []
    health.check_head_lag(lag_errors, 1000, 8000, max_lag_blocks=6000)
    assert lag_errors and "lag_blocks=7000" in lag_errors[0]


def test_archive_response_reader_rejects_oversized_body_before_reading():
    archive = load_archive_module()
    original = archive.open_rpc_request
    old_limit = os.environ.get("BASE_RPC_MAX_RESPONSE_BYTES")
    os.environ["BASE_RPC_MAX_RESPONSE_BYTES"] = str(1024 * 1024)

    class OversizedResponse:
        status = 200
        headers = {
            "Content-Length": str(1024 * 1024 + 1),
            "Content-Type": "application/json",
        }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            raise AssertionError("oversized response body should not be read")

        def geturl(self):
            return "https://one.example"

    archive.open_rpc_request = lambda *_args, **_kwargs: OversizedResponse()
    try:
        try:
            archive.post_json("https://one.example", {"jsonrpc": "2.0"})
        except RuntimeError as exc:
            assert "exceeds" in str(exc)
        else:
            raise AssertionError("archive accepted an oversized RPC response")
    finally:
        archive.open_rpc_request = original
        if old_limit is None:
            os.environ.pop("BASE_RPC_MAX_RESPONSE_BYTES", None)
        else:
            os.environ["BASE_RPC_MAX_RESPONSE_BYTES"] = old_limit


def test_archive_rpc_response_rejects_redirected_or_non_json_responses():
    archive = load_archive_module()
    original = archive.open_rpc_request

    class Response:
        status = 200

        def __init__(self, url, content_type):
            self.url = url
            self.headers = {"Content-Type": content_type}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return self.url

        def read(self, *_args):
            return b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'

    try:
        archive.open_rpc_request = lambda *_args, **_kwargs: Response(
            "https://redirected.example",
            "application/json",
        )
        try:
            archive.post_json("https://one.example", {"jsonrpc": "2.0"})
        except RuntimeError as exc:
            assert "URL changed" in str(exc)
        else:
            raise AssertionError("archive accepted a response from a redirected URL")

        archive.open_rpc_request = lambda *_args, **_kwargs: Response(
            "https://one.example",
            "text/html",
        )
        try:
            archive.post_json("https://one.example", {"jsonrpc": "2.0"})
        except RuntimeError as exc:
            assert "non-JSON" in str(exc)
        else:
            raise AssertionError("archive accepted a non-JSON RPC response")
    finally:
        archive.open_rpc_request = original


def test_archive_rpc_transport_rejects_unsafe_urls_and_sanitizes_http_errors():
    archive = load_archive_module()
    original = archive.open_rpc_request
    secret_url = "https://host-secret.rpc.custom.example/v2/path-secret?api_key=query-secret"
    try:
        archive.open_rpc_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe RPC URL reached the network")
        )
        for unsafe in (
            "http://provider.example/rpc",
            "https://user:password-secret@provider.example/rpc",
            "https://provider.example:8443/rpc",
            "https://provider.example/rpc#fragment",
        ):
            try:
                archive.post_json(unsafe, {})
            except RuntimeError as exc:
                assert "password-secret" not in str(exc)
            else:
                raise AssertionError(f"archive accepted unsafe RPC URL: {unsafe}")

        error = archive.urllib.error.HTTPError(secret_url, 401, "body-secret", {}, None)
        archive.open_rpc_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
        try:
            archive.post_json(secret_url, {})
        except RuntimeError as exc:
            assert str(exc) == "HTTP 401"
            assert "secret" not in str(exc)
        else:
            raise AssertionError("archive accepted an RPC HTTP failure")
    finally:
        archive.open_rpc_request = original


def test_archive_rpc_single_and_batch_envelopes_are_exact_and_secret_safe():
    archive = load_archive_module()
    original = archive.post_json
    url = "https://host-secret.rpc.custom.example/v2/path-secret?api_key=query-secret"
    try:
        archive.post_json = lambda *_args, **_kwargs: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "body-secret", "data": "data-secret"},
        }
        try:
            archive.rpc_call("eth_test", [], urls=[url])
        except RuntimeError as exc:
            message = str(exc)
            assert "code=-32601" in message
            assert "host-secret" not in message
            assert "path-secret" not in message
            assert "query-secret" not in message
            assert "body-secret" not in message
            assert "data-secret" not in message
        else:
            raise AssertionError("archive accepted JSON-RPC error response")

        malformed_single = (
            {"jsonrpc": "2.0", "id": True, "result": "0x1"},
            {"jsonrpc": "1.0", "id": 1, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": "0x1", "error": None},
            {"jsonrpc": "2.0", "id": 1, "error": {"code": True, "message": "bad"}},
        )
        for envelope in malformed_single:
            archive.post_json = lambda *_args, _envelope=envelope, **_kwargs: _envelope
            try:
                archive.rpc_call("eth_test", [], urls=["https://provider.example/rpc"])
            except RuntimeError as exc:
                assert "envelope" in str(exc)
            else:
                raise AssertionError(f"archive accepted malformed JSON-RPC envelope: {envelope!r}")
    finally:
        archive.post_json = original

    valid = [
        {"jsonrpc": "2.0", "id": 1, "result": "second"},
        {"jsonrpc": "2.0", "id": 0, "result": "first"},
    ]
    assert sorted(archive.validated_rpc_batch_items(valid, 2)) == [0, 1]
    malformed_batches = (
        [{"jsonrpc": "2.0", "id": True, "result": "first"}],
        [
            {"jsonrpc": "2.0", "id": 0, "result": "first"},
            {"jsonrpc": "2.0", "id": 0, "result": "duplicate"},
        ],
        [{"jsonrpc": "1.0", "id": 0, "result": "first"}],
        [{"jsonrpc": "2.0", "id": 0, "result": "first", "error": None}],
        [{"jsonrpc": "2.0", "id": 0, "error": {"code": True, "message": "bad"}}],
        [{"jsonrpc": "2.0", "id": 0, "result": "first"}],
    )
    call_counts = (1, 2, 1, 1, 1, 2)
    for envelope, call_count in zip(malformed_batches, call_counts):
        try:
            archive.validated_rpc_batch_items(envelope, call_count)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"archive accepted malformed JSON-RPC batch envelope: {envelope!r}")


def test_config_uses_shared_log_dir_for_watcher_log():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        config = watcher.config_from_env({"DEGEN_DOGS_LOG_DIR": tmp})
        assert config.log_path == Path(tmp) / "watch-onchain.log"


def test_quicknode_hostname_variants_cannot_double_vote():
    watcher = load_module()
    urls = watcher.independent_rpc_urls(
        [
            "https://alpha.quiknode.pro/key-a",
            "https://beta.quiknode.pro/key-b",
            "https://mainnet.base.org",
        ]
    )
    assert urls == ["https://alpha.quiknode.pro/key-a", "https://mainnet.base.org"]
    assert watcher.rpc_provider_key("https://base-mainnet.g.alchemy.com/public") == "alchemy"
    assert watcher.rpc_provider_key("https://base-mainnet.public.blastapi.io") == "alchemy"


def test_refresh_command_exact_allowlist_and_auto_push_guard(monkeypatch=None):
    watcher = load_module()
    env = {}
    config = watcher.config_from_env(env)
    assert config.auto_push is False
    assert config.refresh_command == "npm run refresh:current"
    assert watcher.validate_refresh_command(config) == ("npm", "run", "refresh:current")
    assert config.state_path.name == "mission3_onchain_tracker_state.json"
    assert config.interval_seconds == 15
    assert config.cooldown_seconds == 30
    assert config.bid_cooldown_seconds == 15
    assert config.quorum_size == 2
    assert len(config.rpc_urls) >= 2
    assert len({watcher.rpc_provider_key(url) for url in config.rpc_urls}) == len(config.rpc_urls)
    assert config.force_after_seconds == 0
    assert config.refresh_lock_path and config.refresh_lock_path.name == "refresh.lock"

    config = watcher.config_from_env({
        "DEGEN_DOGS_REFRESH_LOCK_PATH": "/tmp/degen-refresh.lock",
        "MISSION3_REFRESH_LOCK_PATH": "/tmp/mission-refresh.lock",
    })
    assert config.refresh_lock_path == Path("/tmp/degen-refresh.lock")

    env = {"MISSION3_WATCHER_AUTO_PUSH": "1"}
    config = watcher.config_from_env(env)
    assert config.auto_push is True
    assert config.require_clean_tree is True
    assert config.refresh_command == "npm run refresh:publish"
    assert watcher.validate_refresh_command(config) == ("npm", "run", "refresh:publish")

    try:
        watcher.config_from_env({
            "MISSION3_WATCHER_AUTO_PUSH": "1",
            "MISSION3_WATCHER_REQUIRE_CLEAN_TREE": "0",
        })
    except SystemExit as exc:
        assert "clean_tree" in str(exc).lower()
    else:
        raise AssertionError("auto-push accepted a disabled clean-tree safety gate")

    try:
        watcher.config_from_env({
            "MISSION3_WATCHER_AUTO_PUSH": "1",
            "MISSION3_REFRESH_LOCK_PATH": "-",
        })
    except SystemExit as exc:
        assert "cannot be disabled" in str(exc)
    else:
        raise AssertionError("auto-push accepted a disabled shared refresh lock")

    try:
        watcher.config_from_env({"MISSION3_REFRESH_COMMAND": "npm run refresh:publish"})
    except SystemExit as exc:
        assert "auto_push=1" in str(exc).lower()
    else:
        raise AssertionError("publish command should require MISSION3_WATCHER_AUTO_PUSH=1")

    unsupported = (
        "true",
        "git push origin main",
        "npm run refresh:archive",
        "npm run refresh:local",
        "bash scripts/refresh_and_publish.sh",
        "/usr/local/bin/npm run refresh:current",
        "npm run refresh:current -- --force",
        "npm run refresh:current; touch /tmp/watcher-injection",
        "npm run refresh:current && id",
        "npm run refresh:current $(id)",
        " npm run refresh:current",
        "npm run refresh:current ",
        "npm\trun refresh:current",
        "npm run refresh:current\nid",
    )
    for command in unsupported:
        try:
            watcher.config_from_env({
                "MISSION3_WATCHER_AUTO_PUSH": "1",
                "MISSION3_REFRESH_COMMAND": command,
            })
        except SystemExit as exc:
            assert "exactly match" in str(exc).lower()
        else:
            raise AssertionError(f"unsupported refresh command was accepted: {command!r}")

    with tempfile.TemporaryDirectory() as tmp:
        sentinel = Path(tmp) / "must-not-exist"
        injected = f"npm run refresh:current; touch {sentinel}"
        try:
            watcher.config_from_env({"MISSION3_REFRESH_COMMAND": injected})
        except SystemExit:
            pass
        else:
            raise AssertionError("command injection payload was accepted")
        assert not sentinel.exists()


def test_run_refresh_executes_fixed_argv_without_a_shell():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        captured: dict[str, object] = {}

        class FakeProcess:
            pid = 12345
            returncode = 0

            def __init__(self, args):
                self.args = args

            def communicate(self, timeout=None):  # noqa: ANN001, ANN201
                captured["timeout"] = timeout
                return "", ""

        def fake_popen(args, **kwargs):  # noqa: ANN001, ANN202
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess(args)

        config = watcher.config_from_env({
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_REQUIRE_CLEAN_TREE": "1",
        })
        original_popen = watcher.subprocess.Popen
        original_git_status = watcher.git_status_tracked
        watcher.subprocess.Popen = fake_popen
        watcher.git_status_tracked = lambda: ""
        try:
            assert watcher.run_refresh(config, ["auction_bid"], dry_run=False) == ("success", 0)
        finally:
            watcher.subprocess.Popen = original_popen
            watcher.git_status_tracked = original_git_status

        assert captured["args"] == ["npm", "run", "refresh:current"]
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        assert len(kwargs["pass_fds"]) == 1
        assert captured["timeout"] == config.timeout_seconds


def test_publish_refresh_bypasses_npm_to_preserve_lock_descriptor():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        captured: dict[str, object] = {}

        class FakeProcess:
            pid = 12346
            returncode = 0

            def __init__(self, args):
                self.args = args

            def communicate(self, timeout=None):  # noqa: ANN001, ANN201
                captured["timeout"] = timeout
                return "", ""

        def fake_popen(args, **kwargs):  # noqa: ANN001, ANN202
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess(args)

        config = watcher.config_from_env({
            "MISSION3_WATCHER_AUTO_PUSH": "1",
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:publish",
            "MISSION3_WATCHER_REQUIRE_CLEAN_TREE": "1",
        })
        original_popen = watcher.subprocess.Popen
        original_git_status = watcher.git_status_tracked
        watcher.subprocess.Popen = fake_popen
        watcher.git_status_tracked = lambda: ""
        try:
            assert watcher.run_refresh(config, ["auction_bid"], dry_run=False) == ("success", 0)
        finally:
            watcher.subprocess.Popen = original_popen
            watcher.git_status_tracked = original_git_status

        assert captured["args"] == [
            "/bin/bash",
            "-p",
            str(watcher.PUBLISH_REFRESH_SCRIPT),
        ]
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
        assert len(kwargs["pass_fds"]) == 1
        assert kwargs["env"]["DEGEN_DOGS_LOCK_HELD"] == "1"
        assert kwargs["env"]["DEGEN_DOGS_LOCK_FD"] == str(kwargs["pass_fds"][0])


def test_run_lock_prevents_overlapping_one_shot_runs():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = Path(tmp) / "watcher.lock"
        config = watcher.config_from_env({"MISSION3_WATCHER_LOCK_PATH": str(lock_path)})
        first = watcher.acquire_run_lock(config)
        assert first is not None
        second = watcher.acquire_run_lock(config)
        assert second is None
        watcher.release_run_lock(first)
        third = watcher.acquire_run_lock(config)
        assert third is not None
        watcher.release_run_lock(third)


def test_explicit_rpc_quorum_does_not_append_public_fallbacks_unless_opted_in():
    watcher = load_module()
    explicit = {
        "BASE_RPC_URLS": "https://paid-a.example/rpc,https://paid-b.example/rpc",
        "BASE_LOG_RPC_URLS": "https://paid-a.example/logs,https://paid-b.example/logs",
    }
    config = watcher.config_from_env(explicit)
    assert config.rpc_urls == ["https://paid-a.example/rpc", "https://paid-b.example/rpc"]
    assert config.log_rpc_urls == ["https://paid-a.example/logs", "https://paid-b.example/logs"]
    opted_in = watcher.config_from_env({**explicit, "BASE_INCLUDE_PUBLIC_FALLBACKS": "1"})
    assert any("base.org" in url or "publicnode.com" in url for url in opted_in.rpc_urls)


def test_blank_rpc_values_keep_working_public_defaults_even_when_fallback_flag_is_zero():
    watcher = load_module()
    config = watcher.config_from_env({
        "BASE_RPC_URL": "",
        "BASE_RPC_URLS": "",
        "BASE_LOG_RPC_URLS": "",
        "BASE_INCLUDE_PUBLIC_FALLBACKS": "0",
        "MISSION3_WATCHER_LOG_PATH": "-",
    })
    assert len(config.rpc_urls) >= 2
    assert len({watcher.rpc_provider_key(url) for url in config.rpc_urls}) >= 2


def test_watcher_snapshot_rejects_split_fresh_and_stale_heads():
    watcher = load_module()
    config = watcher.config_from_env({
        "BASE_RPC_URLS": "https://fresh.example,https://stale.example",
        "BASE_LOG_RPC_URLS": "https://fresh.example,https://stale.example",
        "MISSION3_WATCHER_LOG_PATH": "-",
    })
    old_collect = watcher.collect_rpc_probes
    old_spread = watcher.RPC_MAX_HEAD_SPREAD_BLOCKS
    old_quorum = watcher.rpc_quorum_call
    try:
        watcher.collect_rpc_probes = lambda *_args, **_kwargs: (
            [("https://fresh.example", 1000), ("https://stale.example", 100)],
            [],
        )
        watcher.RPC_MAX_HEAD_SPREAD_BLOCKS = 20
        watcher.rpc_quorum_call = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("block quorum must not run for split heads")
        )
        try:
            watcher.verified_snapshot_head(config)
        except RuntimeError as exc:
            assert "cannot form a recent quorum" in str(exc)
        else:
            raise AssertionError("watcher accepted a split fresh/stale head pair")
    finally:
        watcher.collect_rpc_probes = old_collect
        watcher.RPC_MAX_HEAD_SPREAD_BLOCKS = old_spread
        watcher.rpc_quorum_call = old_quorum


def test_watcher_snapshot_rejects_old_hash_agreed_block_timestamp():
    watcher = load_module()
    config = watcher.config_from_env({
        "BASE_RPC_URLS": "https://one.example,https://two.example",
        "BASE_LOG_RPC_URLS": "https://one.example,https://two.example",
        "MISSION3_WATCHER_LOG_PATH": "-",
    })
    old_collect = watcher.collect_rpc_probes
    old_quorum = watcher.rpc_quorum_call
    old_max_age = watcher.RPC_MAX_BLOCK_AGE_SECONDS
    try:
        watcher.collect_rpc_probes = lambda *_args, **_kwargs: (
            [("https://one.example", 100), ("https://two.example", 100)],
            [],
        )
        watcher.rpc_quorum_call = lambda *_args, **_kwargs: (
            {
                "number": "0x63",
                "hash": "0x" + "a" * 64,
                "timestamp": hex(int(time.time()) - 3600),
            },
            ["https://one.example", "https://two.example"],
        )
        watcher.RPC_MAX_BLOCK_AGE_SECONDS = 600
        try:
            watcher.verified_snapshot_head(config)
        except RuntimeError as exc:
            assert "outside the freshness window" in str(exc)
        else:
            raise AssertionError("watcher accepted an old hash-agreed block")
    finally:
        watcher.collect_rpc_probes = old_collect
        watcher.rpc_quorum_call = old_quorum
        watcher.RPC_MAX_BLOCK_AGE_SECONDS = old_max_age


def test_refresh_lock_defers_overlapping_refresh_commands():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        refresh_lock_path = Path(tmp) / "refresh.lock"
        git_status_calls = 0

        def fail_if_git_status_runs():
            nonlocal git_status_calls
            git_status_calls += 1
            raise AssertionError("git status must not run while another publisher owns the refresh lock")

        config = watcher.config_from_env({
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_REFRESH_LOCK_PATH": str(refresh_lock_path),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_REQUIRE_CLEAN_TREE": "1",
        })
        held = watcher.acquire_refresh_lock(config)
        assert held is not None
        original_git_status = watcher.git_status_tracked
        watcher.git_status_tracked = fail_if_git_status_runs
        try:
            try:
                watcher.run_refresh(config, ["auction_bid"], dry_run=False)
            except watcher.RefreshAlreadyRunning as exc:
                assert "another refresh" in str(exc)
            else:
                raise AssertionError("overlapping refresh should be deferred")
        finally:
            watcher.git_status_tracked = original_git_status
            watcher.release_run_lock(held)
        assert git_status_calls == 0


def test_run_once_defers_before_rpc_scan_while_publisher_lock_is_active():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        refresh_lock_path = tmp_path / "refresh.lock"
        state_path = tmp_path / "state.json"
        original_state = {
            "last_seen_token_id": 727,
            "pending_refresh": True,
            "pending_refresh_reasons": ["auction_bid"],
        }
        state_path.write_text(json.dumps(original_state, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(tmp_path / "watcher.lock"),
            "MISSION3_REFRESH_LOCK_PATH": str(refresh_lock_path),
            "MISSION3_WATCHER_AUTO_PUSH": "1",
            "MISSION3_REFRESH_COMMAND": "npm run refresh:publish",
        })
        held = watcher.acquire_refresh_lock(config)
        assert held is not None
        watcher.fetch_snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("RPC/log scan must be deferred while the publisher owns refresh.lock")
        )
        try:
            assert watcher.run_once(config) == 0
        finally:
            watcher.release_run_lock(held)
        assert json.loads(state_path.read_text(encoding="utf-8")) == original_state


def test_run_once_records_lock_contention_as_healthy_deferred_refresh():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        state_path = tmp_path / "state.json"
        refresh_lock_path = tmp_path / "refresh.lock"
        state_path.write_text(
            json.dumps(
                {
                    "last_seen_token_id": 727,
                    "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "last_seen_amount_wei": "100",
                    "last_refresh_at_utc": iso(0),
                    "last_seen_auction_created_log_id": "90:0xcreated:1",
                    "last_seen_bid_log_id": "100:0xoldbid:1",
                    "last_seen_auction_settled_log_id": "",
                    "last_seen_auction_extended_log_id": "",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        state_path.chmod(0o600)
        snapshot = {
            "latest_block": 101,
            "checked_from_block": 100,
            "checked_to_block": 101,
            "token_id": 727,
            "high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "amount_wei": "200",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
            "bid_log": {"id": "101:0xnewbid:4", "tx_hash": "0xnewbid"},
            "extended_log": None,
            "settled_log": None,
        }
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(tmp_path / "watcher.lock"),
            "MISSION3_REFRESH_LOCK_PATH": str(refresh_lock_path),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            "MISSION3_WATCHER_REQUIRE_CLEAN_TREE": "1",
        })
        held = watcher.acquire_refresh_lock(config)
        assert held is not None
        original_git_status = watcher.git_status_tracked
        watcher.git_status_tracked = lambda: (_ for _ in ()).throw(
            AssertionError("git status must not run while another publisher owns the refresh lock")
        )
        watcher.fetch_snapshot = lambda _config, _state: snapshot
        original_probe = watcher.refresh_lock_is_active
        # Simulate the narrow race where the publisher takes the lock after the
        # early probe but before the watcher starts its refresh command.
        watcher.refresh_lock_is_active = lambda _config: False
        try:
            assert watcher.run_once(config) == 0
        finally:
            watcher.refresh_lock_is_active = original_probe
            watcher.git_status_tracked = original_git_status
            watcher.release_run_lock(held)

        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["last_refresh_status"] == "deferred_refresh_lock"
        assert saved["pending_refresh"] is True
        assert saved["pending_bid_log_id"] == "101:0xnewbid:4"
        assert saved.get("consecutive_refresh_failures", 0) == 0
        assert saved["last_seen_bid_log_id"] == "100:0xoldbid:1"


def test_watcher_log_catchup_starts_large_and_adaptively_shrinks_ranges():
    watcher = load_module()
    config = watcher.config_from_env({
        "BASE_RPC_URLS": "https://one.example,https://two.example",
        "BASE_LOG_RPC_URLS": "https://one.example,https://two.example",
        "MISSION3_WATCHER_LOG_PATH": "-",
    })
    assert config.log_chunk == 2000
    observed_spans: list[int] = []
    original_quorum_call = watcher.rpc_quorum_call

    def bounded_log_quorum(method, params, **_kwargs):  # noqa: ANN001, ANN202
        assert method == "eth_getLogs"
        request_filter = params[0]
        start = int(request_filter["fromBlock"], 16)
        end = int(request_filter["toBlock"], 16)
        span = end - start + 1
        observed_spans.append(span)
        if span > 250:
            raise watcher.RpcLogRangeLimit("provider range limit")
        return [], ["https://one.example", "https://two.example"]

    watcher.rpc_quorum_call = bounded_log_quorum
    try:
        assert watcher.fetch_logs(config, 1, 1000) == []
    finally:
        watcher.rpc_quorum_call = original_quorum_call
    assert observed_spans[:2] == [1000, 500]
    assert observed_spans[2:] == [250, 250, 250, 250]


def test_watcher_log_catchup_does_not_amplify_generic_quorum_failures():
    watcher = load_module()
    config = watcher.config_from_env({
        "BASE_RPC_URLS": "https://one.example,https://two.example",
        "BASE_LOG_RPC_URLS": "https://one.example,https://two.example",
        "MISSION3_WATCHER_LOG_PATH": "-",
    })
    calls = 0
    original_quorum_call = watcher.rpc_quorum_call

    def failed_quorum(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("transport/quorum unavailable")

    watcher.rpc_quorum_call = failed_quorum
    try:
        try:
            watcher.fetch_logs(config, 1, 1000)
        except RuntimeError as exc:
            assert "transport/quorum unavailable" in str(exc)
        else:
            raise AssertionError("generic quorum failure was incorrectly treated as a range limit")
    finally:
        watcher.rpc_quorum_call = original_quorum_call
    assert calls == 1


def test_log_quorum_shrinks_only_when_explicit_range_failures_make_quorum_impossible():
    watcher = load_module()
    urls = ["https://one.example", "https://two.example", "https://three.example"]
    original_call = watcher.rpc_call_with_retry
    original_deadline = watcher.RPC_QUORUM_DEADLINE_SECONDS

    def two_range_one_stall(_method, _params, *, url, timeout):  # noqa: ANN001, ANN202, ARG001
        if url != urls[-1]:
            raise watcher.RpcLogRangeLimit("range")
        time.sleep(0.5)
        return [], url

    watcher.RPC_QUORUM_DEADLINE_SECONDS = 0.05
    watcher.rpc_call_with_retry = two_range_one_stall
    try:
        try:
            watcher.rpc_quorum_call("eth_getLogs", [], urls=urls, required=2)
        except watcher.RpcLogRangeLimit:
            pass
        else:
            raise AssertionError("impossible range-limited quorum was not classified for adaptive shrink")

        watcher.RPC_SLOW_UNTIL.clear()

        def two_range_one_generic(_method, _params, *, url, timeout):  # noqa: ANN001, ANN202, ARG001
            if url != urls[-1]:
                raise watcher.RpcLogRangeLimit("range")
            raise RuntimeError("transport unavailable")

        watcher.rpc_call_with_retry = two_range_one_generic
        try:
            watcher.rpc_quorum_call("eth_getLogs", [], urls=urls, required=2)
        except watcher.RpcLogRangeLimit:
            pass
        else:
            raise AssertionError("two range failures plus a generic failure did not trigger safe adaptive shrink")

        watcher.RPC_SLOW_UNTIL.clear()

        def one_range_one_generic_one_success(_method, _params, *, url, timeout):  # noqa: ANN001, ANN202, ARG001
            if url == urls[0]:
                raise watcher.RpcLogRangeLimit("range")
            if url == urls[1]:
                raise RuntimeError("transport unavailable")
            return [], url

        watcher.rpc_call_with_retry = one_range_one_generic_one_success
        try:
            watcher.rpc_quorum_call("eth_getLogs", [], urls=urls, required=2)
        except watcher.RpcLogRangeLimit:
            pass
        else:
            raise AssertionError("a good vote plus a range-limited provider did not trigger safe adaptive shrink")

        watcher.RPC_SLOW_UNTIL.clear()

        def one_range_two_stalls(_method, _params, *, url, timeout):  # noqa: ANN001, ANN202, ARG001
            if url == urls[0]:
                raise watcher.RpcLogRangeLimit("range")
            time.sleep(0.5)
            return [], url

        watcher.rpc_call_with_retry = one_range_two_stalls
        try:
            watcher.rpc_quorum_call("eth_getLogs", [], urls=urls, required=2)
        except watcher.RpcLogRangeLimit as exc:
            raise AssertionError("one range failure with two possible providers was over-classified") from exc
        except RuntimeError as exc:
            assert "deadline exceeded" in str(exc)
        else:
            raise AssertionError("stalled generic quorum unexpectedly succeeded")
    finally:
        watcher.rpc_call_with_retry = original_call
        watcher.RPC_QUORUM_DEADLINE_SECONDS = original_deadline


def test_watcher_timeout_teardown_does_not_wait_for_escaped_pipe_holder():
    watcher = load_module()
    child = "import time; time.sleep(2)"
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True); "
        "time.sleep(5)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    started = time.monotonic()
    time.sleep(0.05)
    stdout, stderr = watcher.terminate_process_group_bounded(process, grace_seconds=0.2)
    assert stdout == ""
    assert stderr == ""
    assert time.monotonic() - started < 1.5


def test_watcher_timeout_kills_same_group_grandchild_before_lock_release():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ready = root / "ready"
        survived = root / "survived"
        child = (
            "import pathlib,signal,time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"pathlib.Path({str(ready)!r}).write_text('ready'); "
            "time.sleep(0.7); "
            f"pathlib.Path({str(survived)!r}).write_text('survived')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(5)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", parent],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        deadline = time.monotonic() + 1
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "same-group grandchild did not initialize"
        watcher.terminate_process_group_bounded(process, grace_seconds=0.1)
        time.sleep(0.75)
        assert not survived.exists(), "same-group grandchild outlived watcher teardown"


def test_log_scan_start_uses_last_checked_block_safety_overlap_or_recent_lookback():
    watcher = load_module()
    assert watcher.choose_log_from_block({}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_501
    assert watcher.choose_log_from_block({"last_checked_block": 9_900}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_851
    assert watcher.choose_log_from_block({"last_checked_block": 1}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 4_000
    assert watcher.choose_log_from_block({"last_seen_block": 9_900}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_851
    assert watcher.choose_log_from_block({"last_checked_block": 10_100}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_501


def test_generated_dashboard_baseline_prevents_false_initial_refresh_but_detects_stale_bid():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        generated = root / "generated"
        generated.mkdir()
        (generated / "current_auction.csv").write_text(
            "token_id,bidder_wallet,current_bid_eth,settled,latest_block,latest_block_time_utc\n"
            "727,0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,0.011,0,100,2026-05-29 00:00:00\n",
            encoding="utf-8",
        )
        snapshot = {
            "latest_block": 110,
            "token_id": 727,
            "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "amount_wei": "11000000000000000",
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
            "settled_log": None,
            "extended_log": None,
            "bid_log": {"id": "105:0xbid:2", "tx_hash": "0xbid"},
        }
        baseline = watcher.state_from_generated_dashboard(snapshot, now_utc=iso(), root=root)
        assert baseline["chain_id"] == 8453
        assert baseline["auction_house"].lower() == watcher.AUCTION_HOUSE.lower()
        assert baseline["last_checked_block"] == 100
        assert baseline["last_seen_bid_tx"] == "0xbid"
        decision = watcher.decide_refresh(baseline, snapshot, now_utc=iso(60), cooldown_seconds=300, force_after_seconds=0)
        assert decision.should_refresh is False
        assert decision.reasons == []

        changed = dict(snapshot)
        changed["amount_wei"] = "12000000000000000"
        decision = watcher.decide_refresh(baseline, changed, now_utc=iso(600), cooldown_seconds=300, force_after_seconds=0)
        assert decision.should_refresh is True
        assert decision.reasons == ["highest_bid_amount_changed"]


def test_dry_run_does_not_write_state_and_reports_refresh_intent():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "state.json"
        original_state = {
            "last_seen_token_id": 727,
            "last_seen_high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "last_seen_amount_wei": "100",
            "last_refresh_at_utc": iso(0),
            "last_seen_auction_created_log_id": "90:0xcreated:1",
            "last_seen_auction_settled_log_id": "",
        }
        state_path.write_text(json.dumps(original_state, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_WATCHER_LOCK_PATH": str(Path(tmp) / "watcher.lock"),
            "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
        })
        snapshot = {
            "latest_block": 101,
            "checked_from_block": 100,
            "token_id": 727,
            "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "amount_wei": "200",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated"},
            "bid_log": {"id": "101:0xbid:4", "tx_hash": "0xbid"},
            "extended_log": None,
            "settled_log": None,
            "rpc_url": "https://mainnet.base.org",
        }
        called = {"refresh": False}
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        setattr(watcher, "run_refresh", lambda _config, _reasons, dry_run, event=None: called.update(refresh=True) or ("dry_run", 0))
        assert watcher.run_once(config, dry_run=True) == 0
        assert called["refresh"] is True
        assert json.loads(state_path.read_text(encoding="utf-8")) == original_state


def test_run_refresh_exports_structured_event_environment():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "env.json"
        capture_script = Path(tmp) / "npm"
        capture_script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "keys = ['DEGEN_DOGS_REFRESH_TRIGGER', 'DEGEN_DOGS_REFRESH_REASONS', 'DEGEN_DOGS_DETECTED_AT_UTC', 'DEGEN_DOGS_EVENT_NAME', 'DEGEN_DOGS_EVENT_BLOCK_NUMBER', 'DEGEN_DOGS_EVENT_TX_HASH', 'DEGEN_DOGS_EVENT_LOG_INDEX', 'DEGEN_DOGS_LOCK_HELD', 'DEGEN_DOGS_LOCK_FD', 'DEGEN_DOGS_REFRESH_LOCK_PATH']\n"
            "captured = {key: os.environ.get(key) for key in keys}\n"
            "lock_fd = int(captured['DEGEN_DOGS_LOCK_FD'])\n"
            "lock_stat = os.fstat(lock_fd)\n"
            "path_stat = Path(captured['DEGEN_DOGS_REFRESH_LOCK_PATH']).stat()\n"
            "captured['lock_fd_matches_path'] = (lock_stat.st_dev, lock_stat.st_ino) == (path_stat.st_dev, path_stat.st_ino)\n"
            "captured['argv'] = sys.argv[1:]\n"
            "Path(os.environ['WATCH_TEST_OUT']).write_text(json.dumps(captured, sort_keys=True), encoding='utf-8')\n",
            encoding="utf-8",
        )
        capture_script.chmod(0o700)
        old_out = os.environ.get("WATCH_TEST_OUT")
        old_path = os.environ.get("PATH", "")
        os.environ["WATCH_TEST_OUT"] = str(out_path)
        os.environ["PATH"] = f"{tmp}:{old_path}"
        try:
            config = watcher.config_from_env({
                "MISSION3_WATCHER_LOG_PATH": "-",
                "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
                "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            })
            status, exit_code = watcher.run_refresh(
                config,
                ["auction_bid", "highest_bid_amount_changed"],
                dry_run=False,
                event={"event_name": "AuctionBid", "block_number": 123, "tx_hash": "0xabc", "log_index": 7},
            )
        finally:
            if old_out is None:
                os.environ.pop("WATCH_TEST_OUT", None)
            else:
                os.environ["WATCH_TEST_OUT"] = old_out
            os.environ["PATH"] = old_path
        assert (status, exit_code) == ("success", 0)
        captured = json.loads(out_path.read_text(encoding="utf-8"))
        assert captured["DEGEN_DOGS_REFRESH_TRIGGER"] == "watcher"
        assert json.loads(captured["DEGEN_DOGS_REFRESH_REASONS"]) == ["auction_bid", "highest_bid_amount_changed"]
        assert captured["DEGEN_DOGS_DETECTED_AT_UTC"]
        assert captured["DEGEN_DOGS_EVENT_NAME"] == "AuctionBid"
        assert captured["DEGEN_DOGS_EVENT_BLOCK_NUMBER"] == "123"
        assert captured["DEGEN_DOGS_EVENT_TX_HASH"] == "0xabc"
        assert captured["DEGEN_DOGS_EVENT_LOG_INDEX"] == "7"
        assert captured["DEGEN_DOGS_LOCK_HELD"] == "1"
        assert captured["DEGEN_DOGS_REFRESH_LOCK_PATH"].endswith("/refresh.lock")
        assert captured["lock_fd_matches_path"] is True
        assert captured["argv"] == ["run", "refresh:current"]


def test_run_once_writes_structured_watcher_telemetry():
    watcher = load_module()
    setattr(watcher, "refresh_telemetry", load_telemetry_module())
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        state_path = tmp_path / "state.json"
        telemetry_path = tmp_path / "watcher_checks.jsonl"
        now = watcher.utc_now()
        snapshot = {
            "latest_block": 101,
            "checked_from_block": 100,
            "checked_to_block": 101,
            "token_id": 727,
            "high_bidder": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "amount_wei": "200",
            "settled": False,
            "start_time_unix": 1,
            "end_time_unix": 2,
            "checked_log_count": 1,
            "created_log": {"id": "90:0xcreated:1", "tx_hash": "0xcreated", "block_number": 90, "log_index": 1, "event_name": "AuctionCreated"},
            "bid_log": {"id": "101:0xbid:4", "tx_hash": "0xbid", "block_number": 101, "log_index": 4, "event_name": "AuctionBid"},
            "extended_log": None,
            "settled_log": None,
        }
        state = watcher.state_from_snapshot(snapshot, now_utc=now, previous_state={})
        state["last_refresh_at_utc"] = now
        state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        state_path.chmod(0o600)
        old_path = os.environ.get("MISSION3_WATCHER_TELEMETRY_PATH")
        os.environ["MISSION3_WATCHER_TELEMETRY_PATH"] = str(telemetry_path)
        try:
            config = watcher.config_from_env({
                "MISSION3_WATCHER_STATE_PATH": str(state_path),
                "MISSION3_WATCHER_LOG_PATH": "-",
                "MISSION3_WATCHER_LOCK_PATH": str(tmp_path / "watcher.lock"),
                "MISSION3_REFRESH_COMMAND": "npm run refresh:current",
            })
            setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
            assert watcher.run_once(config) == 0
        finally:
            if old_path is None:
                os.environ.pop("MISSION3_WATCHER_TELEMETRY_PATH", None)
            else:
                os.environ["MISSION3_WATCHER_TELEMETRY_PATH"] = old_path
        rows = [json.loads(line) for line in telemetry_path.read_text(encoding="utf-8").splitlines() if line]
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "watcher_check"
        assert row["result"] == "no_refresh"
        assert row["token_id"] == 727
        assert row["checked_to_block"] == 101
        assert row["event_name"] == "AuctionBid"
        assert row["event_tx_hash"] == "0xbid"
        assert row["pending_refresh"] is False
        assert row["duration_seconds"] >= 0


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"watcher_tests=pass count={len(tests)}")
