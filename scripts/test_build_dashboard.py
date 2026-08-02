#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_dashboard.py"


def load_module() -> Any:
    os.environ["MISSION3_LOG_CACHE"] = "1"
    os.environ["MISSION3_BALANCE_CACHE"] = "1"
    spec = importlib.util.spec_from_file_location("build_dashboard", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def log(block: int, tx: str, index: int) -> dict[str, Any]:
    return {"blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(index), "data": "0x", "topics": []}


def test_quicknode_hostname_variants_share_one_quorum_vote() -> None:
    dashboard = load_module()
    assert dashboard._rpc_provider_key("https://alpha.quiknode.pro/key-a") == "quicknode"
    assert dashboard._rpc_provider_key("https://beta.quiknode.pro/key-b") == "quicknode"
    assert dashboard._rpc_provider_key("https://legacy.quicknode.pro/key") == "quicknode"
    assert dashboard._rpc_provider_key("https://base-mainnet.g.alchemy.com/public") == "alchemy"
    assert dashboard._rpc_provider_key("https://base-mainnet.public.blastapi.io") == "alchemy"
    base_failovers = dashboard._same_operator_rpc_urls("https://mainnet.base.org")
    assert "https://mainnet.base.org" in base_failovers
    assert "https://developer-access-mainnet.base.org" in base_failovers


def test_fetch_logs_extends_cached_ranges_with_overlap_and_dedupes() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        setattr(dashboard, "LOG_CACHE_OVERLAP_BLOCKS", 5)
        calls: list[tuple[int, int]] = []

        def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
            calls.append((start, end))
            if len(calls) == 1:
                return [log(100, "0xaaa", 0), log(150, "0xbbb", 2)]
            return [log(150, "0xbbb", 2), log(160, "0xccc", 1)]

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        first = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 150)
        assert [item["transactionHash"] for item in first] == ["0xaaa", "0xbbb"]
        assert calls == [(100, 150)]

        second = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 175)
        assert calls == [(100, 150), (146, 175)]
        assert [item["transactionHash"] for item in second] == ["0xaaa", "0xbbb", "0xccc"]

        third = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 175)
        assert calls == [(100, 150), (146, 175), (171, 175)]
        assert [item["transactionHash"] for item in third] == ["0xaaa", "0xbbb", "0xccc"]


def test_fetch_logs_replaces_reorged_overlap_instead_of_retaining_orphans() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        setattr(dashboard, "LOG_CACHE_OVERLAP_BLOCKS", 10)
        calls = 0

        def fake_fetch(_address: str, _topics: str | list[str], _start: int, _end: int) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                orphan = log(150, "0xorphan", 0)
                orphan["blockHash"] = "0xold"
                return [log(100, "0xstable", 0), orphan]
            replacement = log(150, "0xcanonical", 0)
            replacement["blockHash"] = "0xnew"
            return [replacement]

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        first = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 150)
        assert [item["transactionHash"] for item in first] == ["0xstable", "0xorphan"]

        second = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 155)
        assert [item["transactionHash"] for item in second] == ["0xstable", "0xcanonical"]
        assert all(item["transactionHash"] != "0xorphan" for item in second)


def test_fetch_logs_caches_empty_ranges() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        calls: list[tuple[int, int]] = []

        def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
            calls.append((start, end))
            return []

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        assert dashboard.fetch_logs("0xabc", [dashboard.TOPIC_AUCTION_CREATED], 200, 250) == []
        assert dashboard.fetch_logs("0xabc", [dashboard.TOPIC_AUCTION_CREATED], 200, 250) == []
        assert calls == [(200, 250), (200, 250)]


def test_fetch_logs_checkpoints_completed_batches_before_transient_failure() -> None:
    dashboard = load_module()
    calls: list[tuple[int, int]] = []
    old_cache_dir = dashboard.LOG_CACHE_DIR
    old_chunk = dashboard.LOG_CHUNK
    old_workers = dashboard.LOG_WORKERS
    old_overlap = dashboard.LOG_CACHE_OVERLAP_BLOCKS

    def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
        calls.append((start, end))
        if len(calls) == 2:
            raise RuntimeError("transient provider failure")
        return [{
            "address": "0xabc",
            "blockHash": f"0x{end:064x}",
            "blockNumber": hex(end),
            "data": "0x",
            "logIndex": "0x0",
            "removed": False,
            "topics": [dashboard.TOPIC_AUCTION_CREATED],
            "transactionHash": f"0x{end + 1:064x}",
        }]

    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.LOG_CACHE_DIR = Path(tmp)
            dashboard.LOG_CHUNK = 10
            dashboard.LOG_WORKERS = 1
            dashboard.LOG_CACHE_OVERLAP_BLOCKS = 0
            dashboard._fetch_logs_uncached = fake_fetch
            try:
                dashboard.fetch_logs("0xabc", dashboard.TOPIC_AUCTION_CREATED, 100, 200)
            except RuntimeError as exc:
                assert "transient provider failure" in str(exc)
            else:
                raise AssertionError("expected transient provider failure")
            cache_path = dashboard._log_cache_path("0xabc", dashboard.TOPIC_AUCTION_CREATED, 100)
            cached_to, cached_logs = dashboard._load_log_cache(
                cache_path,
                "0xabc",
                dashboard.TOPIC_AUCTION_CREATED,
                100,
            )
            assert cached_to == 109
            assert [int(row["blockNumber"], 16) for row in cached_logs] == [109]
        finally:
            dashboard.LOG_CACHE_DIR = old_cache_dir
            dashboard.LOG_CHUNK = old_chunk
            dashboard.LOG_WORKERS = old_workers
            dashboard.LOG_CACHE_OVERLAP_BLOCKS = old_overlap


def address_topic(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def transfer_log(dashboard: Any, block: int, from_address: str, to_address: str) -> dict[str, Any]:
    return {
        "blockNumber": hex(block),
        "transactionHash": f"0x{block:064x}",
        "logIndex": "0x0",
        "topics": [dashboard.TOPIC_TRANSFER, address_topic(from_address), address_topic(to_address)],
        "data": "0x",
    }


def test_fetch_woof_holders_reuses_cached_balances_until_address_is_touched() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    carol = "0x00000000000000000000000000000000000000c3"
    balances = {alice: 100, bob: 200, carol: 300}

    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "WOOF_BALANCE_CACHE", Path(tmp) / "woof_balances.json")
        calls: list[list[str]] = []

        def fake_fetch(addresses: list[str], _block_tag: str) -> dict[str, int]:
            calls.append(addresses)
            return {address: balances[address] for address in addresses}

        setattr(dashboard, "fetch_balances", fake_fetch)
        first_logs = [transfer_log(dashboard, 100, alice, bob)]
        first = dashboard.fetch_woof_holders(first_logs, 0, "0x64")
        assert calls == [[alice, bob]]
        assert [(row["address"], row["balance_raw"]) for row in first] == [(bob, "200"), (alice, "100")]

        second = dashboard.fetch_woof_holders(first_logs, 0, "0x65")
        assert calls == [[alice, bob], []]
        assert [(row["address"], row["balance_raw"]) for row in second] == [(bob, "200"), (alice, "100")]

        balances[bob] = 250
        third_logs = [*first_logs, transfer_log(dashboard, 102, bob, carol)]
        third = dashboard.fetch_woof_holders(third_logs, 0, "0x66")
        assert calls == [[alice, bob], [], [bob, carol]]
        assert [(row["address"], row["balance_raw"]) for row in third] == [(carol, "300"), (bob, "250"), (alice, "100")]


def test_fetch_farcaster_profiles_stops_after_neynar_auth_failure() -> None:
    dashboard = load_module()
    original_key_loader = dashboard.load_neynar_api_key
    original_urlopen = dashboard.urllib.request.urlopen
    original_sleep = dashboard.time.sleep
    try:
        for code in (401, 403):
            calls: list[str] = []
            sleeps: list[float] = []

            def fake_urlopen(req: Any, timeout: int = 0, *, status_code: int = code) -> Any:
                calls.append(req.full_url)
                raise dashboard.urllib.error.HTTPError(req.full_url, status_code, "Auth failed", {}, None)

            dashboard.load_neynar_api_key = lambda: "bad-key"
            dashboard.urllib.request.urlopen = fake_urlopen
            dashboard.time.sleep = lambda seconds: sleeps.append(seconds)
            addresses = [f"0x{i:040x}" for i in range(205)]
            assert dashboard.fetch_farcaster_profiles(addresses) == []
            assert len(calls) == 1
            assert sleeps == []
    finally:
        dashboard.load_neynar_api_key = original_key_loader
        dashboard.urllib.request.urlopen = original_urlopen
        dashboard.time.sleep = original_sleep


def test_degendogs_auction_profiles_include_all_current_bid_history_bidders() -> None:
    dashboard = load_module()
    original_urlopen = dashboard.urllib.request.urlopen
    current_bidder = "0x00000000000000000000000000000000000000b2"
    early_bidder = "0x00000000000000000000000000000000000000c3"

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({
                "nounId": 11,
                "bidder": current_bidder,
                "amount": 1.0,
                "bids": [
                    {"nounId": 11, "bidder": current_bidder, "username": "unitcurrent", "pfp_url": ""},
                    {"nounId": 11, "bidder": early_bidder, "username": "unitearly", "pfp_url": ""},
                ],
            }).encode("utf-8")

    try:
        dashboard.urllib.request.urlopen = lambda _req, timeout=0: FakeResponse()
        rows = dashboard.fetch_degendogs_auction_profiles({"token_id": 11, "bidder": current_bidder, "amount_eth": 1.0})
    finally:
        dashboard.urllib.request.urlopen = original_urlopen

    by_address = {row["address"]: row for row in rows}
    assert by_address[current_bidder]["username"] == "unitcurrent"
    assert by_address[early_bidder]["username"] == "unitearly"


def test_cached_wallet_identity_profiles_can_backfill_dashboard_labels() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000d4"
    with tempfile.TemporaryDirectory() as tmp:
        identity_path = Path(tmp) / "wallet_profiles.json"
        identity_path.write_text(json.dumps({
            wallet: {
                "wallet": wallet,
                "display": "@cachedbidder",
                "farcaster_handle": "cachedbidder",
                "farcaster_fid": 104,
                "profile_url": "https://farcaster.xyz/cachedbidder",
            }
        }), encoding="utf-8")
        rows = dashboard.load_cached_farcaster_profiles(identity_path)

    assert rows == [{
        "address": wallet,
        "fid": 104,
        "username": "cachedbidder",
        "display_name": "@cachedbidder",
        "pfp_url": "",
    }]
    assert dashboard.merge_farcaster_profiles([], rows)[0]["username"] == "cachedbidder"


def test_current_bid_reward_stats_calculates_payback_daily_roi_and_simple_apr() -> None:
    dashboard = load_module()
    stats = dashboard.current_bid_reward_stats(
        {"amount_wei": "10000000000000000"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0.113508"},
    )
    assert stats["reward_current_bid_payback_days"] == "176.02"
    assert stats["reward_current_bid_daily_roi_pct"] == "0.5681"
    assert stats["reward_current_bid_apr_pct"] == "207.36"
    assert stats["reward_current_bid_apr_display"] == "≈207% APR"


def test_current_bid_reward_stats_unavailable_when_bid_or_daily_flow_missing() -> None:
    dashboard = load_module()
    zero_bid = dashboard.current_bid_reward_stats(
        {"amount_wei": "0"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0.113508"},
    )
    assert zero_bid["reward_current_bid_payback_days"] == "N/A"
    assert zero_bid["reward_current_bid_apr_pct"] == "N/A"
    assert zero_bid["reward_current_bid_apr_display"] == "N/A"

    zero_flow = dashboard.current_bid_reward_stats(
        {"amount_wei": "10000000000000000"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0"},
    )
    assert zero_flow["reward_current_bid_payback_days"] == "N/A"
    assert zero_flow["reward_current_bid_apr_pct"] == "N/A"
    assert zero_flow["reward_current_bid_apr_display"] == "N/A"


def test_timer_urgency_stays_calm_until_less_than_one_hour_remains() -> None:
    dashboard = load_module()
    assert dashboard.timer_urgency_state(3601, "live") == "calm"
    assert dashboard.timer_urgency_state(3600, "live") == "calm"
    assert dashboard.timer_urgency_state(3599, "live") == "urgent"
    assert dashboard.timer_urgency_state(600, "live") == "critical"
    assert dashboard.timer_urgency_state(0, "live") == "ended"


def run_pricing_sql_fixture(dashboard: Any, current_eth_usd: str) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(":memory:")
    dashboard.insert_rows(conn, "auction_created", [
        {"token_id": 8, "start_time_utc": "2026-06-07 00:00:00", "end_time_utc": "2026-06-08 12:00:00", "block_number": 80, "tx_hash": "0xcreated8"},
        {"token_id": 9, "start_time_utc": "2026-05-31 00:00:00", "end_time_utc": "2026-05-31 12:00:00", "block_number": 90, "tx_hash": "0xcreated9"},
        {"token_id": 10, "start_time_utc": "2026-06-01 00:00:00", "end_time_utc": "2026-06-01 12:00:00", "block_number": 100, "tx_hash": "0xcreated10"},
        {"token_id": 11, "start_time_utc": "2026-06-02 00:00:00", "end_time_utc": "2026-06-03 00:00:00", "block_number": 200, "tx_hash": "0xcreated11"},
    ], [("token_id", "INTEGER"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT")])
    dashboard.insert_rows(conn, "auction_bids", [
        {"token_id": 9, "bidder": "0x0000000000000000000000000000000000000099", "bid_eth": 0.25, "bid_wei": "250000000000000000", "extended": 0, "block_number": 95, "tx_hash": "0xbid9", "log_index": 0, "block_time_utc": "2026-05-31 19:00:00"},
        {"token_id": 10, "bidder": "0x00000000000000000000000000000000000000a1", "bid_eth": 0.5, "bid_wei": "500000000000000000", "extended": 0, "block_number": 110, "tx_hash": "0xbid10", "log_index": 0, "block_time_utc": "2026-06-01 19:00:00"},
        {"token_id": 11, "bidder": "0x00000000000000000000000000000000000000c3", "bid_eth": 0.75, "bid_wei": "750000000000000000", "extended": 0, "block_number": 205, "tx_hash": "0xbid11early", "log_index": 0, "block_time_utc": "2026-06-02 00:30:00"},
        {"token_id": 11, "bidder": "0x00000000000000000000000000000000000000b2", "bid_eth": 1.0, "bid_wei": "1000000000000000000", "extended": 0, "block_number": 210, "tx_hash": "0xbid11", "log_index": 0, "block_time_utc": "2026-06-02 01:00:00"},
    ], [("token_id", "INTEGER"), ("bidder", "TEXT"), ("bid_eth", "REAL"), ("bid_wei", "TEXT"), ("extended", "INTEGER"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    dashboard.insert_rows(conn, "auction_settled", [
        {"token_id": 9, "winner": "0x0000000000000000000000000000000000000099", "amount_eth": 0.25, "amount_wei": "250000000000000000", "block_number": 98, "tx_hash": "0xsettled9", "log_index": 0, "block_time_utc": "2026-05-31 19:12:29"},
        {"token_id": 10, "winner": "0x00000000000000000000000000000000000000a1", "amount_eth": 0.5, "amount_wei": "500000000000000000", "block_number": 120, "tx_hash": "0xsettled10", "log_index": 0, "block_time_utc": "2026-06-01 20:00:00"},
    ], [("token_id", "INTEGER"), ("winner", "TEXT"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    dashboard.insert_rows(conn, "woof_holders", [], [("address", "TEXT"), ("balance_woof", "REAL"), ("balance_raw", "TEXT")])
    dashboard.insert_rows(conn, "farcaster_profiles", [
        {"address": "0x00000000000000000000000000000000000000b2", "fid": 102, "username": "unitcurrent", "display_name": "Unit Current", "pfp_url": ""},
        {"address": "0x00000000000000000000000000000000000000c3", "fid": 103, "username": "unitearly", "display_name": "Unit Early", "pfp_url": ""},
    ], [("address", "TEXT"), ("fid", "INTEGER"), ("username", "TEXT"), ("display_name", "TEXT"), ("pfp_url", "TEXT")])
    dashboard.insert_rows(conn, "dog_metadata", [
        {"token_id": 9, "dog_name": "Degen Dog #9", "dog_image_url": "", "dog_external_url": "", "dog_opensea_url": "", "traits": "", "trait_rarity": "", "rarity": "", "rarity_score": 0},
        {"token_id": 10, "dog_name": "Degen Dog #10", "dog_image_url": "", "dog_external_url": "", "dog_opensea_url": "", "traits": "", "trait_rarity": "", "rarity": "", "rarity_score": 0},
        {"token_id": 11, "dog_name": "Degen Dog #11", "dog_image_url": "", "dog_external_url": "", "dog_opensea_url": "", "traits": "", "trait_rarity": "", "rarity": "", "rarity_score": 0},
    ], [("token_id", "INTEGER"), ("dog_name", "TEXT"), ("dog_image_url", "TEXT"), ("dog_external_url", "TEXT"), ("dog_opensea_url", "TEXT"), ("traits", "TEXT"), ("trait_rarity", "TEXT"), ("rarity", "TEXT"), ("rarity_score", "REAL")])
    dashboard.insert_rows(conn, "token_stats", [
        {"metric": "eth_usd_price", "value": current_eth_usd},
        {"metric": "eth_usd_source", "value": "unit_current_price"},
        {"metric": "woof_total_supply", "value": "1"},
    ], [("metric", "TEXT"), ("value", "TEXT")])
    dashboard.insert_rows(conn, "current_auction_source", [{
        "token_id": 11,
        "amount_eth": 1.0,
        "amount_wei": "1000000000000000000",
        "start_time_utc": "2026-06-02 00:00:00",
        "end_time_utc": "2026-06-03 00:00:00",
        "bidder": "0x00000000000000000000000000000000000000b2",
        "settled": 0,
        "latest_block": 220,
        "latest_block_time_utc": "2026-06-02 02:00:00",
    }], [("token_id", "INTEGER"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("bidder", "TEXT"), ("settled", "INTEGER"), ("latest_block", "INTEGER"), ("latest_block_time_utc", "TEXT")])
    dashboard.insert_rows(conn, "historical_prices_daily", [{
        "asset_key": "ETH",
        "date_utc": "2026-06-01",
        "price_usd": "1000",
        "source": "unit_event_price",
        "source_detail": "unit fixture",
        "confidence": "high",
        "timestamp_utc": "2026-06-01T00:00:00Z",
        "notes": "fixture",
    }, {
        "asset_key": "ETH",
        "date_utc": "2026-06-02",
        "price_usd": "9000",
        "source": "unit_next_day_price",
        "source_detail": "unit fixture",
        "confidence": "medium",
        "timestamp_utc": "2026-06-02T00:00:00Z",
        "notes": "fixture that is closer by timestamp but not the event date",
    }], [("asset_key", "TEXT"), ("date_utc", "TEXT"), ("price_usd", "TEXT"), ("source", "TEXT"), ("source_detail", "TEXT"), ("confidence", "TEXT"), ("timestamp_utc", "TEXT"), ("notes", "TEXT")])
    conn.executescript(dashboard.SQL_PATH.read_text(encoding="utf-8"))
    return {
        name: dashboard.table_dicts(*dashboard.fetch_table(conn, name))
        for name in ["recent_bids", "auction_winners", "auction_feed", "current_auction", "current_auction_bid_history"]
    }


def test_historical_auction_usd_uses_event_day_price_while_live_bid_uses_current_price() -> None:
    dashboard = load_module()
    low_current = run_pricing_sql_fixture(dashboard, "2000")
    high_current = run_pricing_sql_fixture(dashboard, "9000")

    low_winner = low_current["auction_winners"][0]
    high_winner = high_current["auction_winners"][0]
    assert low_winner["winning_bid_usd"] == high_winner["winning_bid_usd"] == 500.0
    assert low_winner["winning_bid_usd_at_settlement"] == 500.0
    assert low_winner["eth_usd_price_at_event"] == "1000"
    assert low_winner["eth_usd_price_date_utc"] == "2026-06-01"
    assert low_winner["usd_estimate_source"] == "unit_event_price"
    assert low_winner["usd_estimate_confidence"] == "high"

    historical_bid = next(row for row in low_current["recent_bids"] if row["token_id"] == 10)
    assert historical_bid["bid_usd"] == 500.0
    assert historical_bid["bid_usd_at_event"] == 500.0
    assert historical_bid["usd_estimate_source"] == "unit_event_price"

    nearest_bid = next(row for row in low_current["recent_bids"] if row["token_id"] == 9)
    assert nearest_bid["bid_usd"] == 250.0
    assert nearest_bid["bid_usd_at_event"] == 250.0
    assert nearest_bid["eth_usd_price_date_utc"] == "2026-06-01"
    assert nearest_bid["usd_estimate_basis"] == "nearest_bid_date_eth_usd"

    nearest_winner = next(row for row in low_current["auction_winners"] if row["token_id"] == 9)
    assert nearest_winner["winning_bid_usd"] == 250.0
    assert nearest_winner["winning_bid_usd_at_settlement"] == 250.0
    assert nearest_winner["eth_usd_price_date_utc"] == "2026-06-01"
    assert nearest_winner["usd_estimate_basis"] == "nearest_settlement_date_eth_usd"

    low_feed_settled = next(row for row in low_current["auction_feed"] if row["status"] == "settled" and row["dog"] == "Dog #10")
    high_feed_settled = next(row for row in high_current["auction_feed"] if row["status"] == "settled" and row["dog"] == "Dog #10")
    assert low_feed_settled["amount_usd"] == high_feed_settled["amount_usd"] == 500.0
    assert low_feed_settled["amount_usd_at_event"] == 500.0
    assert low_feed_settled["eth_usd_price_at_event"] == "1000"

    low_live = next(row for row in low_current["auction_feed"] if row["status"] == "ongoing")
    high_live = next(row for row in high_current["auction_feed"] if row["status"] == "ongoing")
    assert low_live["amount_usd"] == 2000.0
    assert high_live["amount_usd"] == 9000.0
    assert low_live["usd_estimate_source"] == "current_eth_usd_price"

    low_current_row = low_current["current_auction"][0]
    high_current_row = high_current["current_auction"][0]
    low_history_high_bid = low_current["current_auction_bid_history"][0]
    high_history_high_bid = high_current["current_auction_bid_history"][0]
    assert low_current_row["current_bid_usd"] == low_live["amount_usd"] == low_history_high_bid["bid_usd"] == 2000.0
    assert high_current_row["current_bid_usd"] == high_live["amount_usd"] == high_history_high_bid["bid_usd"] == 9000.0
    assert low_history_high_bid["eth_usd_price_live"] == "2000"
    assert high_history_high_bid["eth_usd_price_live"] == "9000"
    assert low_history_high_bid["usd_estimate_source"] == "current_eth_usd_price"
    assert low_history_high_bid["usd_estimate_confidence"] == "live_current"


def test_current_auction_bid_history_archives_all_current_bids_with_live_usd_and_profiles() -> None:
    dashboard = load_module()
    result = run_pricing_sql_fixture(dashboard, "2000")
    history = result["current_auction_bid_history"]

    assert [row["token_id"] for row in history] == [11, 11]
    assert [row["bidder"] for row in history] == ["@unitcurrent", "@unitearly"]
    assert [row["bidder_wallet"] for row in history] == [
        "0x00000000000000000000000000000000000000b2",
        "0x00000000000000000000000000000000000000c3",
    ]
    assert history[0]["bid_eth"] == 1.0
    assert history[0]["bid_usd"] == 2000.0
    assert history[0]["bid"] == "1.00000 ETH ($2000)"
    assert history[1]["bid_eth"] == 0.75
    assert history[1]["bid_usd"] == 1500.0
    assert history[0]["eth_usd_price_live"] == "2000"
    assert history[0]["usd_estimate_basis"] == "current_auction_bid_history_live_eth_usd"
    assert history[0]["tx_hash"] == "0xbid11"
    assert history[1]["tx_hash"] == "0xbid11early"


def test_current_bid_history_renders_top_dropdown_without_bottom_table() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000b2"
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [("site_url", "https://example.test"), ("current_auction_token_id", "11")],
        ),
        "auction_feed": (
            [
                "status",
                "dog",
                "dog_image_url",
                "dog_external_url",
                "dog_opensea_url",
                "bidder_winner",
                "bidder_winner_url",
                "bidder_winner_wallet",
                "bid",
                "amount_eth",
                "amount_usd",
                "time_remaining",
                "auction_end_utc",
                "rarity",
                "traits",
                "trait_rarity",
            ],
            [(
                "ongoing",
                "Dog #11",
                "",
                "",
                "",
                "@unitcurrent",
                "https://farcaster.xyz/unitcurrent",
                wallet,
                "1.00000 ETH ($2000)",
                1.0,
                2000.0,
                "02:00:00",
                "2026-06-02 04:00:00",
                "Rank 1",
                "",
                "",
            )],
        ),
        "current_auction_bid_history": (
            ["bid_time_utc", "token_id", "dog", "bidder", "bidder_url", "bidder_wallet", "bid", "bid_eth", "bid_usd", "block_number", "log_index", "tx_hash"],
            [
                ("2026-06-02 01:00:00", 11, "Dog #11", "@unitcurrent", "https://farcaster.xyz/unitcurrent", wallet, "1.00000 ETH ($2000)", 1.0, 2000.0, 210, 0, "0xbid11"),
                ("2026-06-02 00:30:00", 11, "Dog #11", "@unitearly", "https://farcaster.xyz/unitearly", "0x00000000000000000000000000000000000000c3", "0.75000 ETH ($1500)", 0.75, 1500.0, 205, 0, "0xbid11early"),
            ],
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    assert 'class="bid-history-menu"' in rendered
    assert "Bid history" in rendered
    assert "2 bids" in rendered
    assert "@unitcurrent" in rendered
    assert wallet in rendered
    assert "1.00000 ETH ($2000)" in rendered
    assert rendered.index("detail-bidder") < rendered.index("bid-history-menu")
    assert 'data-table="current_auction_bid_history"' not in rendered
    assert 'data-name="current_auction_bid_history"' not in rendered
    css_markers = [
        ".bid-history-menu{position:relative;align-self:stretch;flex:0 1 158px;min-width:150px;max-width:100%;margin-inline:0",
        ".bid-history-menu summary{list-style:none;cursor:pointer;position:relative;display:flex;min-height:48px;height:100%;flex-direction:column;align-items:center;justify-content:center;text-align:center",
        ".bid-history-list{position:absolute;left:50%;top:calc(100% + 3px);z-index:24;transform:translateX(-50%);width:min(340px,calc(100vw - 24px))",
        "@media (max-width:640px){.bid-history-menu{flex:0 1 150px;min-width:136px}",
        "@media (max-width:380px){.current-detail{display:grid;grid-template-columns:1fr}.current-detail > span,.bid-history-menu{width:100%;max-width:100%}",
    ]
    for marker in css_markers:
        assert marker in rendered


def test_log_chunk_is_capped_for_public_base_rpc() -> None:
    dashboard = load_module()
    assert dashboard.LOG_CHUNK <= 10000


def test_verified_snapshot_requires_hash_agreement_from_independent_providers() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example", "https://three.example"]
    old_quorum_urls = dashboard._quorum_rpc_urls
    old_rpc_once = dashboard._rpc_once
    old_quorum_size = dashboard.RPC_QUORUM_SIZE
    old_confirmations = dashboard.SNAPSHOT_CONFIRMATIONS
    old_log_urls = list(dashboard.LOG_RPC_URLS)

    def fake_rpc_once(url: str, method: str, params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_blockNumber":
            return {urls[0]: "0x64", urls[1]: "0x63", urls[2]: "0x64"}[url]
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "hash": "0xcanonical", "timestamp": "0x1"}
        if method == "eth_getCode":
            return "0x60016000"
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    try:
        dashboard._quorum_rpc_urls = lambda: urls
        dashboard._rpc_once = fake_rpc_once
        dashboard.RPC_QUORUM_SIZE = 2
        dashboard.SNAPSHOT_CONFIRMATIONS = 1
        dashboard.LOG_RPC_URLS = urls
        block, block_data, verification = dashboard.verified_snapshot()
    finally:
        dashboard._quorum_rpc_urls = old_quorum_urls
        dashboard._rpc_once = old_rpc_once
        dashboard.RPC_QUORUM_SIZE = old_quorum_size
        dashboard.SNAPSHOT_CONFIRMATIONS = old_confirmations
        dashboard.LOG_RPC_URLS = old_log_urls
        dashboard.VERIFIED_SNAPSHOT_URLS = []
        dashboard.VERIFIED_LOG_URLS = []

    assert block == 99
    assert block_data["hash"] == "0xcanonical"
    assert verification["onchain_verification_status"] == "current_snapshot_cross_provider_verified"
    assert "current_auction" in verification["onchain_verification_scope"]
    assert verification["onchain_chain_id"] == "8453"
    assert verification["rpc_quorum_size"] == "2"
    assert verification["snapshot_block_hash"] == "0xcanonical"
    assert len(verification["log_rpc_quorum_providers"].split(",")) >= 2


def test_rpc_quorum_rejects_disagreement() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        return {"https://one.example": "0x1", "https://two.example": "0x2"}[url]

    try:
        dashboard._rpc_once = fake_rpc_once
        try:
            dashboard.rpc_quorum(
                "eth_call",
                [],
                urls=["https://one.example", "https://two.example"],
                min_agreement=2,
            )
        except RuntimeError as exc:
            assert "quorum disagreement" in str(exc)
        else:
            raise AssertionError("RPC quorum accepted conflicting provider results")
    finally:
        dashboard._rpc_once = old_rpc_once


def test_rpc_quorum_rejects_two_by_two_tie() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    answers = {
        "https://one.example": "0x1",
        "https://two.example": "0x1",
        "https://three.example": "0x2",
        "https://four.example": "0x2",
    }

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        return answers[url]

    try:
        dashboard._rpc_once = fake_rpc_once
        try:
            dashboard.rpc_quorum("eth_call", [], urls=list(answers), min_agreement=2)
        except RuntimeError as exc:
            assert "votes=[2, 2]" in str(exc)
        else:
            raise AssertionError("RPC quorum accepted a 2-2 provider tie")
    finally:
        dashboard._rpc_once = old_rpc_once


def test_rpc_quorum_returns_without_waiting_for_decisive_straggler() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        if url == urls[-1]:
            time.sleep(0.6)
        return "0xcanonical"

    try:
        dashboard._rpc_once = fake_rpc_once
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 1.0
        started = time.monotonic()
        value, agreeing = dashboard.rpc_quorum("eth_call", [], urls=urls, min_agreement=2)
        elapsed = time.monotonic() - started
    finally:
        dashboard._rpc_once = old_rpc_once
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert value == "0xcanonical"
    assert len(agreeing) == 2
    assert elapsed < 0.25


def test_head_probe_returns_after_minimum_quorum_and_grace() -> None:
    dashboard = load_module()
    old_grace = dashboard.RPC_HEAD_PROBE_GRACE_SECONDS
    old_deadline = dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]

    def probe(url: str) -> tuple[str, int]:
        if url == urls[-1]:
            time.sleep(0.6)
        return url, 100

    try:
        dashboard.RPC_HEAD_PROBE_GRACE_SECONDS = 0.0
        dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS = 1.0
        started = time.monotonic()
        results, _errors = dashboard._collect_rpc_probes(urls, required=2, probe=probe, label="test-head")
        elapsed = time.monotonic() - started
    finally:
        dashboard.RPC_HEAD_PROBE_GRACE_SECONDS = old_grace
        dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert len(results) == 2
    assert elapsed < 0.25


def test_long_log_scan_always_quorum_checks_recent_tail() -> None:
    dashboard = load_module()
    old_verified = list(dashboard.VERIFIED_LOG_URLS)
    old_max = dashboard.LOG_QUORUM_MAX_BLOCKS
    old_window = dashboard.LOG_QUORUM_WINDOW_BLOCKS
    old_checkpointed = dashboard._fetch_logs_checkpointed
    old_quorum = dashboard.rpc_quorum
    prefix_calls: list[tuple[int, int]] = []
    tail_calls: list[tuple[int, int]] = []

    def fake_checkpointed(
        _address: str,
        _topics: str | list[str],
        start: int,
        end: int,
        _checkpoint: Any = None,
    ) -> list[dict[str, Any]]:
        prefix_calls.append((start, end))
        return []

    def fake_quorum(_method: str, params: list[Any], **_kwargs: Any) -> tuple[list[Any], list[str]]:
        filter_data = params[0]
        tail_calls.append((int(filter_data["fromBlock"], 16), int(filter_data["toBlock"], 16)))
        return [], ["https://one.example", "https://two.example"]

    try:
        dashboard.VERIFIED_LOG_URLS = ["https://one.example", "https://two.example"]
        dashboard.LOG_QUORUM_MAX_BLOCKS = 50
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = 500
        dashboard._fetch_logs_checkpointed = fake_checkpointed
        dashboard.rpc_quorum = fake_quorum
        assert dashboard._fetch_logs_verified_or_uncached(
            "0xabc",
            dashboard.TOPIC_AUCTION_CREATED,
            0,
            1000,
        ) == []
    finally:
        dashboard.VERIFIED_LOG_URLS = old_verified
        dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
        dashboard._fetch_logs_checkpointed = old_checkpointed
        dashboard.rpc_quorum = old_quorum

    assert prefix_calls == [(0, 500)]
    assert tail_calls == [(start, min(1000, start + 49)) for start in range(501, 1001, 50)]


def test_snapshot_recheck_rejects_mid_refresh_reorg() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)

    def fake_rpc_once(_url: str, method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        assert method == "eth_getBlockByNumber"
        return {"hash": "0xchanged"}

    try:
        dashboard._rpc_once = fake_rpc_once
        dashboard.VERIFIED_SNAPSHOT_URLS = ["https://one.example", "https://two.example"]
        try:
            dashboard.verify_snapshot_unchanged(100, "0xexpected")
        except RuntimeError as exc:
            assert "reorganized during refresh" in str(exc)
        else:
            raise AssertionError("expected a mid-refresh reorg failure")
    finally:
        dashboard._rpc_once = old_rpc_once
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls


def test_write_html_includes_browser_favicon_only() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [
                ("site_url", "https://example.test"),
                ("current_auction_token_id", "11"),
            ],
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    assert '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">' in rendered
    for marker in (
        '<link rel="icon" href="data:,">',
        "apple-touch-icon",
        "site-brand",
        "site-logo",
        "degen-dogs-logo.png",
    ):
        assert marker not in rendered


def test_unified_archive_bid_cell_formats_usd_from_shared_numeric_fallbacks() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (["metric", "value"], [("site_url", "https://example.test"), ("current_auction_token_id", "11")]),
        "auction_feed": ([
            "status", "dog", "dog_image_url", "dog_external_url", "dog_opensea_url", "bidder_winner",
            "bidder_winner_url", "bidder_winner_wallet", "bid", "amount_eth", "amount_usd", "time_remaining",
            "auction_end_utc", "rarity", "traits", "trait_rarity",
        ], [(
            "ongoing", "Dog #11", "", "", "", "@unitcurrent", "https://farcaster.xyz/unitcurrent",
            "0x00000000000000000000000000000000000000b2", "1.00000 ETH ($2000)", 1.0, 2000.0,
            "02:00:00", "2026-06-02 04:00:00", "Rank 1", "", "",
        )]),
        "current_auction_bid_history": (["bid_time_utc", "token_id", "dog", "bidder", "bidder_url", "bidder_wallet", "bid", "bid_eth", "bid_usd", "block_number", "log_index", "tx_hash"], []),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    required_markers = [
        "const usdCandidates=record=>",
        "amount.amount_usd_at_event",
        "const getUsdSortValue=record=>firstNumeric(usdCandidates(record))",
        "const usdDisplay=record=>",
        "const display=usdDisplay(record)",
        "const archiveCurrentRank=record=>",
        "status==='live'||status.includes('ongoing')?1:0",
    ]
    for marker in required_markers:
        assert marker in rendered


def test_write_html_hydrates_every_current_surface_without_overlapping_polls() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [("site_url", "https://example.test"), ("latest_block", "210"), ("current_auction_token_id", "11")],
        ),
        "auction_feed": (["status", "dog", "bid", "auction_end_utc"], [("ongoing", "Dog #11", "1 ETH", "2026-06-02 04:00:00")]),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    for marker in (
        "data-current-dog",
        "data-current-detail",
        "data-current-rewards",
        "data-current-traits",
        "data-current-dog-stage",
        "const LIVE_REFRESH_MS=10000",
        "const CURRENT_FETCH_TIMEOUT_MS=6000",
        "const ARCHIVE_FETCH_TIMEOUT_MS=45000",
        "const controller=new AbortController()",
        "const refreshLiveSurface=()=>liveRefreshPromise||",
        "if(liveSnapshotBlock&&Number(nextBlock)<Number(liveSnapshotBlock))return",
        "cache:'no-store'",
        "https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/main/public/generated/",
        "const rootLocal=new URL(`/generated/${suffix}`,location.origin)",
        "location.hostname.endsWith('github.io')?[raw.href,local.href]",
        "fetchGenerated('current_auction',nextBlock)",
        "fetchGenerated('auction_feed',nextBlock)",
        "fetchGenerated('current_auction_bid_history',nextBlock)",
        "fetchGenerated('mission3_metrics',nextBlock)",
        "const assertCurrentSnapshot=",
        "const assertArchiveSnapshot=",
        "const queueArchiveRefresh=context=>",
        "if(target.key!==liveSnapshotKey)continue",
        "generated snapshot is not atomic yet",
        "hydrateCurrentCard(feed,current,historyRows,metrics)",
        "if(context&&archiveSnapshotKey!==nextKey)queueArchiveRefresh(context)",
        "refreshLiveSurface().finally(scheduleLiveRefresh)",
        "window.addEventListener('online',refreshLiveSurface)",
    ):
        assert marker in rendered
    assert "setInterval(refreshLiveSurface" not in rendered
    assert "fetchGenerated('mission3_metrics',nextBlock),fetchGenerated('unified_dog_search_index'" not in rendered



def write_reward_snapshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "snapshot_utc": "2026-06-02T20:55:15Z",
        "reward_account_dogs_count": "133",
        "account_woof_flow_per_day": "20494201.30",
        "account_sup_flow_per_day": "199.58",
        "account_woof_received": "2856495886.75",
        "account_sup_received": "38733.66",
        "derived_woof_per_dog_per_day": "154091.739097744361",
        "derived_sup_per_dog_per_day": "1.5006015037593985",
        "basis_source": "observed_stream_snapshot_133_dogs",
        "note": "Observed reward account stream snapshot; update when the live stream changes.",
    }, indent=2) + "\n", encoding="utf-8")


def test_load_reward_stream_snapshot_derives_observed_per_dog_values() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "config" / "reward_stream_snapshot.json"
        write_reward_snapshot(snapshot_path)
        snapshot = dashboard.load_reward_stream_snapshot(snapshot_path)
        assert snapshot.dogs_count == Decimal("133")
        assert snapshot.woof_flow_per_day == Decimal("20494201.30")
        assert snapshot.sup_flow_per_day == Decimal("199.58")
        assert dashboard.decimal_value_str(snapshot.woof_per_dog_per_day, 12) == "154091.739097744361"
        assert dashboard.decimal_value_str(snapshot.sup_per_dog_per_day, 16) == "1.5006015037593985"


def test_reward_token_stats_uses_observed_snapshot_for_per_dog_flows_and_usd_totals() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "config" / "reward_stream_snapshot.json"
        write_reward_snapshot(snapshot_path)
        snapshot = dashboard.load_reward_stream_snapshot(snapshot_path)
    stats = dashboard.reward_token_stats(Decimal("0.0000005"), Decimal("0.02"), snapshot=snapshot)
    assert stats["reward_basis_dogs"] == "133"
    assert stats["reward_basis_source"] == "observed_stream_snapshot_133_dogs"
    assert stats["reward_observed_dogs_count"] == "133"
    assert stats["reward_observed_woof_flow_per_day"] == "20494201.3"
    assert stats["reward_observed_sup_flow_per_day"] == "199.58"
    assert stats["reward_observed_woof_received"] == "2856495886.75"
    assert stats["reward_observed_sup_received"] == "38733.66"
    assert stats["reward_observed_woof_per_dog_per_day"] == "154091.739097744361"
    assert stats["reward_observed_sup_per_dog_per_day"] == "1.5006015037593985"
    assert stats["reward_woof_per_dog_per_day"] == "154091.739097744361"
    assert stats["reward_sup_per_dog_per_day"] == "1.5006015037593985"
    assert stats["reward_total_per_dog_usd_per_day"] == "0.107058"


def test_reward_strip_renders_apr_inside_bid_payback_card_with_caveat_copy() -> None:
    dashboard = load_module()
    metrics = {
        "reward_basis_dogs": "133",
        "reward_basis_source": "observed_stream_snapshot_133_dogs",
        "reward_woof_per_dog_per_day": "154091.739097744361",
        "reward_woof_per_dog_usd_per_day": "0.077046",
        "reward_sup_per_dog_per_day": "1.5006015037593985",
        "reward_sup_per_dog_usd_per_day": "0.030012",
        "reward_total_per_dog_usd_per_day": "0.107058",
        "reward_current_bid_payback_days": "186.63",
        "reward_current_bid_apr_pct": "195.58",
        "reward_current_bid_apr_display": "≈196% APR",
    }
    rendered = dashboard.render_reward_strip(metrics)
    assert "<b>Bid payback</b>" in rendered
    assert "Observed 133-Dog stream" not in rendered
    assert "WOOF Vault Bonus excluded." not in rendered
    assert "≈187 days" in rendered
    assert "≈196% APR" in rendered
    assert "Current bid / observed per-Dog flow" in rendered
    assert "Simple APR estimate" in rendered
    assert "not guaranteed" in rendered.lower()
    assert "guaranteed return" not in rendered.lower()


def season6_test_config(
    dashboard: Any,
    *,
    total: str = "1000",
    cap: str = "600",
    campaign_seconds: int = 100,
    expected_future_settlement_interval_seconds: int = 0,
    projection_model: str = "time_weighted_xp_unit_test",
) -> Any:
    campaign_end = f"2026-06-02T00:{campaign_seconds // 60:02d}:{campaign_seconds % 60:02d}Z"
    return dashboard.Season6SupConfig(
        enabled=True,
        sup_token=dashboard.SUP.lower(),
        season_start_utc="2026-06-02T00:00:00Z",
        season_end_utc=campaign_end,
        total_sup=Decimal(total),
        cap_sup=Decimal(cap),
        xp_per_settled_win=Decimal("100"),
        reward_start_delay_days=0,
        cap_level="wallet_estimate",
        projection_model=projection_model,
        expected_future_settlement_interval_seconds=expected_future_settlement_interval_seconds,
        visible_dashboard_mode="compact_final_estimate_only",
        cap_percent_label="5% cap",
    )


def test_season6_time_sliced_rewards_split_after_later_xp_event() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:50Z"},
        ],
        {"token_id": 3, "bidder": bob, "amount_eth": 0.03, "end_time_utc": "2026-06-02T00:01:40Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard),
    )
    by_winner = {row["winner_wallet"]: row for row in outputs["season6_sup_by_winner"]}
    assert by_winner[alice]["season6_wins_confirmed"] == 1
    assert by_winner[alice]["season6_xp_confirmed"] == 100
    assert by_winner[alice]["season6_raw_sup_projected_full"] == "750"
    assert by_winner[bob]["season6_raw_sup_projected_full"] == "250"
    assert by_winner[alice]["season6_capped_sup_projected_full"] == "600"
    assert by_winner[alice]["season6_cap_limited"] == "true"
    assert outputs["season6_metrics"]["season6_sup_unallocated_due_to_zero_xp"] == "0"


def test_season6_cap_uses_explicit_12500_sup_not_percent_math() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": wallet, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {},
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard, total="251340", cap="12500"),
    )
    row = outputs["season6_sup_by_winner"][0]
    assert row["season6_raw_sup_projected_full"] == "251340"
    assert row["season6_cap_sup"] == "12500"
    assert row["season6_capped_sup_projected_full"] == "12500"
    assert row["season6_cap_limited"] == "true"


def test_season6_price_missing_keeps_raw_sup_and_na_usd() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": wallet, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {},
        {"sup_usd_price": "0", "sup_usd_source": "unavailable"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard),
    )
    row = outputs["season6_sup_by_winner"][0]
    assert row["season6_raw_sup_projected_full"] == "1000"
    assert row["season6_raw_usd_projected_full"] == "N/A"
    assert row["season6_capped_usd_projected_full"] == "N/A"


def test_season6_current_bidder_projection_adds_hypothetical_win_and_prior_status() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {"token_id": 2, "bidder": alice, "amount_eth": 0.02, "end_time_utc": "2026-06-02T00:00:50Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:00:50Z",
        config=season6_test_config(dashboard),
    )
    status = outputs["season6_sup_current_bidder_status"][0]
    metrics = outputs["season6_metrics"]
    assert status["current_bidder_wallet"] == alice
    assert status["prior_s6_wins_confirmed"] == 1
    assert status["prior_s6_xp_confirmed"] == 100
    assert status["projected_s6_wins_if_current_bid_wins"] == 2
    assert status["projected_s6_xp_if_current_bid_wins"] == 200
    assert status["projected_capped_sup_if_current_bid_wins"] == "600"
    assert status["current_bidder_cap_status"] == "wallet_near_cap"
    assert metrics["season6_sup_current_bidder_prior_s6_wins"] == "1"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_sup"] == "0"
    assert metrics["season6_sup_current_bid_estimate_status"] == "wallet_near_cap"


def test_season6_three_equal_winners_split_one_third_after_third_win() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    carol = "0x00000000000000000000000000000000000000c3"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:30Z"},
            {"token_id": 3, "winner": carol, "amount_eth": 0.03, "block_time_utc": "2026-06-02T00:01:00Z"},
        ],
        {},
        {"sup_usd_price": "1", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:30Z",
        config=season6_test_config(dashboard, total="90", cap="1000", campaign_seconds=90),
    )
    by_winner = {row["winner_wallet"]: row for row in outputs["season6_sup_by_winner"]}
    assert by_winner[alice]["season6_raw_sup_projected_full"] == "55"
    assert by_winner[bob]["season6_raw_sup_projected_full"] == "25"
    assert by_winner[carol]["season6_raw_sup_projected_full"] == "10"


def test_season6_current_bid_estimate_is_incremental_cap_aware_and_counts_prior_wins() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:50Z"},
        ],
        {"token_id": 3, "bidder": alice, "amount_eth": 0.03, "end_time_utc": "2026-06-02T00:01:15Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:15Z",
        config=season6_test_config(dashboard, total="1000", cap="760", campaign_seconds=100),
    )
    metrics = outputs["season6_metrics"]
    assert metrics["season6_sup_current_bidder_prior_s6_wins"] == "1"
    assert metrics["season6_sup_current_bidder_prior_s6_xp"] == "100"
    assert metrics["season6_sup_current_bid_projected_total_without_win_sup"] == "750"
    assert metrics["season6_sup_current_bid_projected_total_with_win_sup"] == "791.666667"
    assert metrics["season6_sup_current_bid_estimated_raw_incremental_sup"] == "41.666667"
    assert metrics["season6_sup_current_bid_cap_remaining_before_win_sup"] == "10"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_sup"] == "10"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_usd"] == "20"


def test_season6_future_daily_dilution_reduces_current_bid_estimate() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    current = {"token_id": 1, "bidder": alice, "amount_eth": 0.01, "end_time_utc": "2026-06-02T00:00:00Z"}
    no_future = dashboard.build_season6_sup_outputs(
        [],
        current,
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:00:00Z",
        config=season6_test_config(dashboard, total="1000", cap="2000", campaign_seconds=100, expected_future_settlement_interval_seconds=0),
    )["season6_metrics"]
    with_future = dashboard.build_season6_sup_outputs(
        [],
        current,
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:00:00Z",
        config=season6_test_config(dashboard, total="1000", cap="2000", campaign_seconds=100, expected_future_settlement_interval_seconds=50),
    )["season6_metrics"]
    assert no_future["season6_sup_current_bid_estimated_cap_aware_sup"] == "1000"
    assert with_future["season6_sup_future_dilution_enabled"] == "true"
    assert with_future["season6_sup_current_bid_estimated_cap_aware_sup"] == "750"
    assert Decimal(with_future["season6_sup_current_bid_estimated_cap_aware_sup"]) < Decimal(no_future["season6_sup_current_bid_estimated_cap_aware_sup"])


def test_season6_compact_card_uses_final_cap_aware_estimate_only() -> None:
    dashboard = load_module()
    rendered = dashboard.render_season6_strip({
        "season6_sup_enabled": "true",
        "season6_sup_estimate_status": "estimated",
        "season6_sup_current_bid_estimated_cap_aware_sup": "11240.25",
        "season6_sup_current_bid_estimated_cap_aware_usd": "118.02",
    })
    assert "Season 6 SUP estimate" in rendered
    assert "≈11,240 SUP" in rendered
    assert "≈$118 if current bid wins" in rendered
    assert "Adjusted for prior S6 wins; estimate only." in rendered
    forbidden = ["Pool:", "Cap:", "100 XP per settled Dog win", "Projected if current bid wins", "Cap-limited estimate"]
    assert not any(text in rendered for text in forbidden)


def test_season6_compact_card_neutral_without_current_high_bidder() -> None:
    dashboard = load_module()
    rendered = dashboard.render_season6_strip({
        "season6_sup_enabled": "true",
        "season6_sup_estimate_status": "no_current_bid",
    })
    assert "Bid to estimate S6 SUP" in rendered
    assert "Pool:" not in rendered
    assert "Cap:" not in rendered


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"build_dashboard_tests=pass count={len(tests)}")
