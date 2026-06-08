#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "watch_mission3_auction.py"
TELEMETRY_MODULE_PATH = ROOT / "scripts" / "refresh_telemetry.py"


def load_module():
    spec = importlib.util.spec_from_file_location("watch_mission3_auction", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_telemetry_module():
    spec = importlib.util.spec_from_file_location("refresh_telemetry", TELEMETRY_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def word(value: int) -> str:
    return f"{value:064x}"


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
            "MISSION3_REFRESH_COMMAND": "true",
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
            "MISSION3_REFRESH_COMMAND": "false",
            "MISSION3_WATCHER_COOLDOWN_SECONDS": "300",
            "MISSION3_WATCHER_BID_COOLDOWN_SECONDS": "60",
        })
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        assert watcher.run_once(config) == 2
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
            "MISSION3_REFRESH_COMMAND": "true",
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
            "MISSION3_REFRESH_COMMAND": "true",
            "MISSION3_WATCHER_COOLDOWN_SECONDS": "300",
            "MISSION3_WATCHER_BID_COOLDOWN_SECONDS": "60",
        })
        setattr(watcher, "fetch_snapshot", lambda _config, _state: snapshot)
        assert watcher.run_once(config) == 0
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
    assert redacted == "https://***.alchemy.com/<redacted-path>?redacted=1"

    infura = watcher.redact_url("https://mainnet.infura.io/v3/infura-secret")
    assert "infura-secret" not in infura
    assert infura == "https://***.infura.io/<redacted-path>"

    public = watcher.redact_url("https://mainnet.base.org")
    assert public == "https://mainnet.base.org"


def test_config_uses_shared_log_dir_for_watcher_log():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        config = watcher.config_from_env({"DEGEN_DOGS_LOG_DIR": tmp})
        assert config.log_path == Path(tmp) / "watch-onchain.log"


def test_refresh_command_default_safe_and_auto_push_guard(monkeypatch=None):
    watcher = load_module()
    env = {}
    config = watcher.config_from_env(env)
    assert config.auto_push is False
    assert config.refresh_command == "npm run data && npm run build"
    assert config.state_path.name == "mission3_onchain_tracker_state.json"
    assert config.interval_seconds == 60
    assert config.cooldown_seconds == 180
    assert config.bid_cooldown_seconds == 60
    assert config.refresh_lock_path and config.refresh_lock_path.name == "refresh.lock"

    env = {"MISSION3_WATCHER_AUTO_PUSH": "1"}
    config = watcher.config_from_env(env)
    assert config.auto_push is True
    assert config.refresh_command == "npm run refresh:publish"

    unsafe = watcher.config_from_env({"MISSION3_REFRESH_COMMAND": "git push origin main"})
    try:
        watcher.validate_refresh_command(unsafe)
    except SystemExit as exc:
        assert "auto-push" in str(exc).lower()
    else:
        raise AssertionError("unsafe publish command should require MISSION3_WATCHER_AUTO_PUSH=1")

    for command in ("npm run refresh:publish", "npm run refresh:archive", "bash scripts/refresh_archive_and_publish.sh"):
        unsafe = watcher.config_from_env({"MISSION3_REFRESH_COMMAND": command})
        try:
            watcher.validate_refresh_command(unsafe)
        except SystemExit as exc:
            assert "auto-push" in str(exc).lower()
        else:
            raise AssertionError(f"unsafe publish command should require MISSION3_WATCHER_AUTO_PUSH=1: {command}")


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


def test_refresh_lock_defers_overlapping_refresh_commands():
    watcher = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        refresh_lock_path = Path(tmp) / "refresh.lock"
        config = watcher.config_from_env({
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_REFRESH_LOCK_PATH": str(refresh_lock_path),
            "MISSION3_REFRESH_COMMAND": "true",
        })
        held = watcher.acquire_refresh_lock(config)
        assert held is not None
        try:
            try:
                watcher.run_refresh(config, ["auction_bid"], dry_run=False)
            except watcher.RefreshAlreadyRunning as exc:
                assert "another refresh" in str(exc)
            else:
                raise AssertionError("overlapping refresh should be deferred")
        finally:
            watcher.release_run_lock(held)


def test_log_scan_start_uses_last_checked_block_safety_overlap_or_recent_lookback():
    watcher = load_module()
    assert watcher.choose_log_from_block({}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_501
    assert watcher.choose_log_from_block({"last_checked_block": 9_900}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_851
    assert watcher.choose_log_from_block({"last_checked_block": 1}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 4_000
    assert watcher.choose_log_from_block({"last_seen_block": 9_900}, latest_block=10_000, default_from_block=4_000, lookback_blocks=500, safety_overlap_blocks=50) == 9_851


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
        config = watcher.config_from_env({
            "MISSION3_WATCHER_STATE_PATH": str(state_path),
            "MISSION3_WATCHER_LOG_PATH": "-",
            "MISSION3_REFRESH_COMMAND": "false",
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
        capture_script = Path(tmp) / "capture_env.py"
        capture_script.write_text(
            "import json, os\n"
            "from pathlib import Path\n"
            "keys = ['DEGEN_DOGS_REFRESH_TRIGGER', 'DEGEN_DOGS_REFRESH_REASONS', 'DEGEN_DOGS_DETECTED_AT_UTC', 'DEGEN_DOGS_EVENT_NAME', 'DEGEN_DOGS_EVENT_BLOCK_NUMBER', 'DEGEN_DOGS_EVENT_TX_HASH', 'DEGEN_DOGS_EVENT_LOG_INDEX']\n"
            "Path(os.environ['WATCH_TEST_OUT']).write_text(json.dumps({key: os.environ.get(key) for key in keys}, sort_keys=True), encoding='utf-8')\n",
            encoding="utf-8",
        )
        old_out = os.environ.get("WATCH_TEST_OUT")
        os.environ["WATCH_TEST_OUT"] = str(out_path)
        try:
            config = watcher.config_from_env({
                "MISSION3_WATCHER_LOG_PATH": "-",
                "MISSION3_REFRESH_LOCK_PATH": str(Path(tmp) / "refresh.lock"),
                "MISSION3_REFRESH_COMMAND": f"python3 {capture_script}",
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
        assert (status, exit_code) == ("success", 0)
        captured = json.loads(out_path.read_text(encoding="utf-8"))
        assert captured["DEGEN_DOGS_REFRESH_TRIGGER"] == "watcher"
        assert json.loads(captured["DEGEN_DOGS_REFRESH_REASONS"]) == ["auction_bid", "highest_bid_amount_changed"]
        assert captured["DEGEN_DOGS_DETECTED_AT_UTC"]
        assert captured["DEGEN_DOGS_EVENT_NAME"] == "AuctionBid"
        assert captured["DEGEN_DOGS_EVENT_BLOCK_NUMBER"] == "123"
        assert captured["DEGEN_DOGS_EVENT_TX_HASH"] == "0xabc"
        assert captured["DEGEN_DOGS_EVENT_LOG_INDEX"] == "7"


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
        old_path = os.environ.get("MISSION3_WATCHER_TELEMETRY_PATH")
        os.environ["MISSION3_WATCHER_TELEMETRY_PATH"] = str(telemetry_path)
        try:
            config = watcher.config_from_env({
                "MISSION3_WATCHER_STATE_PATH": str(state_path),
                "MISSION3_WATCHER_LOG_PATH": "-",
                "MISSION3_WATCHER_LOCK_PATH": str(tmp_path / "watcher.lock"),
                "MISSION3_REFRESH_COMMAND": "true",
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
