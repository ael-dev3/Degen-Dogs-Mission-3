#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_unified_dog_index.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_unified_dog_index", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def assert_runtime_error_contains(callback: Any, expected: str) -> None:
    try:
        callback()
    except RuntimeError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected RuntimeError containing {expected!r}")


def test_ended_pending_settlement_feed_row_stays_in_unified_archive() -> None:
    unified = load_module()
    feed = {
        "status": "ended pending settlement",
        "dog": "Dog #731",
        "dog_image_url": "https://api.degendogs.club/images/731.png",
        "dog_external_url": "https://degendogs.club/#dog731",
        "dog_opensea_url": "https://opensea.io/item/base/0x09154248ffdbaf8aa877ae8a4bf8ce1503596428/731",
        "bidder_winner": "@shalexvivek",
        "bidder_winner_url": "https://farcaster.xyz/shalexvivek",
        "bidder_winner_wallet": "0x412b7153504217b405af821bdcdc5f21c71e3cbc",
        "amount_eth": "0.04311",
        "amount_usd": "81.5",
        "auction_time_utc": "2026-06-02 19:29:19",
        "last_bid_utc": "2026-06-02 19:29:19",
        "auction_end_utc": "2026-06-02 19:34:19",
        "settled_time_utc": "",
        "rarity": "#74/732",
        "traits": "Background: Halo; Body: White",
        "trait_rarity": "Background: Halo (9.8%); Body: White (11.3%)",
    }
    current = {
        "token_id": "731",
        "bidder": "@shalexvivek",
        "bidder_url": "https://farcaster.xyz/shalexvivek",
        "bidder_wallet": "0x412b7153504217b405af821bdcdc5f21c71e3cbc",
        "current_bid_eth": "0.04311",
        "current_bid_usd": "81.5",
        "start_time_utc": "2026-06-01 19:24:33",
        "latest_block_time_utc": "2026-06-02 19:35:57",
    }

    record = unified.generated_feed_record(feed, current, {})
    assert unified.archive_status_from_feed(feed["status"]) == "ended pending settlement"
    assert record["status"] == "ended pending settlement"
    assert record["settlement"]["settled"] is False
    assert record["activity_time_basis"] == "last_bid_block_time"
    assert record["winner_or_high_bidder"]["wallet"] == "0x412b7153504217b405af821bdcdc5f21c71e3cbc"
    assert record["amount"]["native"] == "0.04311"
    assert record["rarity"]["scope"] == "base_existing"
    assert unified.record_sort_key(record)[0] == 0
    assert "ended pending settlement" in record["search_text"]


def test_stale_mission3_unsettled_archive_row_is_not_marked_ongoing() -> None:
    unified = load_module()
    row = {
        "_mission": 3,
        "token_id": 727,
        "settled": False,
        "auction_created_time_utc": "2026-05-28T18:36:55Z",
        "bid_count": 2,
        "unique_bidder_count": 2,
        "sources": ["base_logs", "archive_indexer"],
        "confidence": "verified",
    }

    record = unified.normalize_record(row, {}, {}, {})

    assert record is not None
    assert record["status"] == "ended pending settlement"
    assert record["settlement"]["settled"] is False
    assert unified.archive_status_from_feed("ended_unsettled") == "ended pending settlement"
    assert unified.archive_status_from_feed("live") == "archive unresolved"
    assert unified.archive_status_from_feed("live", is_current_auction=True) == "ongoing"
    assert unified.record_sort_key(record)[0] == 0
    assert "ongoing" not in record["search_text"]


def test_non_mission3_archive_status_labels_are_preserved() -> None:
    unified = load_module()
    row = {
        "_mission": 1,
        "token_id": 0,
        "status": "no auction dogmaster reward",
        "mint_time_utc": "2022-03-14T14:01:34Z",
        "rarity": "#10/669",
        "sources": ["polygon_receipts"],
        "confidence": "verified",
    }

    record = unified.normalize_record(row, {}, {}, {})

    assert record is not None
    assert record["status"] == "no auction dogmaster reward"
    assert record["rarity"]["scope"] == "base_existing"
    assert "no auction dogmaster reward" in record["search_text"]


def test_sort_tie_uses_dog_id_not_status_rank() -> None:
    unified = load_module()
    settled = {"dog_id": 199, "mission": 1, "status": "settled", "activity_time_utc": "2023-11-11T14:01:42Z"}
    pending = {"dog_id": 200, "mission": 1, "status": "ended pending settlement", "activity_time_utc": "2023-11-11T14:01:42Z"}

    ordered = sorted([settled, pending], key=unified.record_sort_key, reverse=True)

    assert [row["dog_id"] for row in ordered] == [200, 199]


def test_settled_feed_amount_usd_is_reused_as_event_usd_for_archive_display() -> None:
    unified = load_module()
    feed = {
        "status": "settled",
        "dog": "Dog #736",
        "bidder_winner": "0xd29c…1cde",
        "bidder_winner_wallet": "0xd29c790466675153a50df7860b9efdb689a21cde",
        "amount_eth": "0.02662",
        "amount_usd": "48.22",
        "auction_time_utc": "2026-06-07 20:10:25",
        "settled_time_utc": "2026-06-07 20:10:25",
        "eth_usd_price_at_event": "1811.346676900944",
        "eth_usd_price_date_utc": "2026-06-04",
    }

    record = unified.generated_feed_record(feed, {}, {})
    amount = record["amount"]

    assert amount["usd_estimate"] == "48.22"
    assert amount["usd_estimate_display"] == "$48.22"
    assert amount["amount_usd_at_event"] == "48.22"
    assert "48.22" in record["search_text"]


def test_newest_sort_prioritizes_only_actual_current_before_recent_settled_rows() -> None:
    unified = load_module()
    live = {"dog_id": 739, "mission": 3, "status": "ongoing", "activity_time_utc": "2026-06-01T00:00:00Z"}
    old_pending = {"dog_id": 727, "mission": 3, "status": "ended pending settlement", "activity_time_utc": "2026-05-28T22:09:17Z"}
    recent_settled = {"dog_id": 738, "mission": 3, "status": "settled", "activity_time_utc": "2026-06-09T20:37:53Z"}

    ordered = sorted([old_pending, recent_settled, live], key=unified.record_sort_key, reverse=True)

    assert [row["dog_id"] for row in ordered] == [739, 738, 727]
    assert unified.record_sort_key(live)[0] == 1
    assert unified.record_sort_key(old_pending)[0] == 0
    assert unified.record_sort_key(recent_settled)[0] == 0


def test_historical_mission3_backfill_keeps_settled_row_after_recent_feed_rolloff() -> None:
    unified = load_module()
    row = {
        "mission": 3,
        "chain": "Base",
        "chain_id": 8453,
        "token_id": 728,
        "dog": "Dog #728",
        "dog_image_url": "https://api.degendogs.club/images/728.png",
        "dog_external_url": "https://degendogs.club/#dog728",
        "dog_opensea_url": "https://opensea.io/item/base/0x09154248ffdbaf8aa877ae8a4bf8ce1503596428/728",
        "status": "settled",
        "winner": "@unitwinner",
        "winner_url": "https://farcaster.xyz/unitwinner",
        "winner_wallet": "0xffe16898fc0af80ee9bcf29d2b54a0f20f9498ad",
        "amount": "0.01210 ETH ($24.44)",
        "amount_raw": "12100000000000000",
        "bid_count": 3,
        "unique_bidder_count": 2,
        "auction_created_time_utc": "2026-05-29 18:38:33",
        "settled_time_utc": "2026-05-30 18:40:09",
        "rarity": "#110/740",
        "traits": "Background: Green",
        "trait_rarity": "Background: Green (10%)",
        "confidence": "verified_live_base_logs",
        "sources": "base_logs,dashboard_builder",
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old_path = unified.HISTORICAL_SEARCH
        old_archive = unified.DOG_ARCHIVE
        try:
            unified.HISTORICAL_SEARCH = tmp_path / "historical_dog_search.json"
            unified.DOG_ARCHIVE = tmp_path / "archive" / "dogs"
            unified.HISTORICAL_SEARCH.write_text(json.dumps([row]), encoding="utf-8")
            records: list[dict[str, Any]] = []
            added = unified.apply_historical_mission3_backfill(records, {}, {})
        finally:
            unified.HISTORICAL_SEARCH = old_path
            unified.DOG_ARCHIVE = old_archive

    assert added == 1
    assert len(records) == 1
    record = records[0]
    amount = record["amount"]
    assert record["dog_id"] == 728
    assert record["status"] == "settled"
    assert record["settlement"]["settled"] is True
    assert amount["native"] == "0.01210"
    assert amount["native_symbol"] == "ETH"
    assert amount["usd_estimate"] == "24.44"
    assert amount["usd_estimate_display"] == "$24.44"
    assert amount["amount_usd_at_event"] == "24.44"
    assert record["activity_time_utc"] == "2026-05-30T18:40:09Z"
    assert record["winner_or_high_bidder"]["wallet"] == "0xffe16898fc0af80ee9bcf29d2b54a0f20f9498ad"
    assert "dog #728" in record["search_text"]


def test_historical_mission3_backfill_preserves_existing_bid_context() -> None:
    unified = load_module()
    row = {
        "mission": 3,
        "token_id": 728,
        "status": "settled",
        "winner_wallet": "0xffe16898fc0af80ee9bcf29d2b54a0f20f9498ad",
        "amount": "0.01210 ETH ($24.44)",
        "bid_count": 3,
        "unique_bidder_count": 3,
        "settled_time_utc": "2026-05-30 18:40:09",
        "sources": "base_logs,dashboard_builder",
        "confidence": "verified_live_base_logs",
    }
    existing = {
        "mission": 3,
        "dog_id": 728,
        "status": "settled",
        "amount": {
            "usd_estimate": "24.43202508222839",
            "usd_estimate_display": "$24.43",
            "usd_estimate_source": "defillama_coin_prices",
            "usd_estimate_source_detail": "coins.llama.fi/chart/coingecko:ethereum",
            "amount_usd_at_event": "24.43202508222839",
        },
        "bid_stats": {
            "bid_count": 3,
            "unique_bidder_count": 3,
            "last_bid_time_utc": "2026-05-30T18:33:17Z",
        },
        "bid_tx_hashes": ["0x" + "a" * 64],
        "source": {"sources": ["generated_auction_feed"], "confidence": "verified"},
    }

    record = unified.historical_mission3_record(row, {}, {}, existing)

    assert record is not None
    assert record["bid_stats"]["last_bid_time_utc"] == "2026-05-30T18:33:17Z"
    assert record["bid_tx_hashes"] == ["0x" + "a" * 64]
    assert record["amount"]["usd_estimate"] == "24.43202508222839"
    assert record["amount"]["usd_estimate_display"] == "$24.43"
    assert record["amount"]["usd_estimate_source"] == "defillama_coin_prices"
    assert record["amount"]["amount_usd_at_event"] == "24.43202508222839"
    assert record["amount"]["usd_estimate_source_detail"] == "coins.llama.fi/chart/coingecko:ethereum"
    assert "generated_auction_feed" in record["source"]["sources"]
    assert "historical_dog_search_backfill" in record["source"]["sources"]
    assert "0x" + "a" * 64 in record["search_text"]


def test_current_feed_override_drops_historical_backfill_provenance() -> None:
    unified = load_module()
    record = {
        "mission": 3,
        "dog_id": 739,
        "status": "settled",
        "source": {
            "sources": ["base_logs", "historical_dog_search_backfill"],
            "notes": "Mission 3 live/current source of truth is Base auction logs and current auction contract state. Backfilled from generated historical_dog_search so settled Mission 3 rows do not disappear.",
        },
        "amount": {
            "amount_usd_at_event": "43",
            "eth_usd_price_at_event": None,
            "usd_estimate": "43",
            "usd_estimate_display": "$43",
        },
        "winner_or_high_bidder": {},
        "bid_stats": {},
        "bid_tx_hashes": [],
        "links": {},
        "rarity": {},
        "traits": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old_feed, old_current, old_recent, old_historical = unified.AUCTION_FEED, unified.CURRENT_AUCTION, unified.RECENT_BIDS, unified.HISTORICAL_SEARCH
        try:
            unified.AUCTION_FEED = tmp_path / "auction_feed.json"
            unified.CURRENT_AUCTION = tmp_path / "current_auction.json"
            unified.RECENT_BIDS = tmp_path / "recent_bids.json"
            unified.HISTORICAL_SEARCH = tmp_path / "historical_dog_search.json"
            unified.AUCTION_FEED.write_text(json.dumps([{
                "status": "ongoing",
                "dog": "Dog #739",
                "bidder_winner": "@current",
                "bidder_winner_wallet": "0x46e9beef5dc68dff095eca56dadf90247f1af7ef",
                "amount_eth": "0.0265",
                "amount_usd": "43.70",
                "last_bid_utc": "2026-06-09 20:38:17",
                "rarity": "#100/669",
            }]), encoding="utf-8")
            unified.CURRENT_AUCTION.write_text(json.dumps([{"token_id": 739, "auction_state": "live"}]), encoding="utf-8")
            unified.RECENT_BIDS.write_text("[]", encoding="utf-8")
            unified.HISTORICAL_SEARCH.write_text("[]", encoding="utf-8")
            updated = unified.apply_current_auction_overrides([record], {})
        finally:
            unified.AUCTION_FEED, unified.CURRENT_AUCTION, unified.RECENT_BIDS, unified.HISTORICAL_SEARCH = old_feed, old_current, old_recent, old_historical

    assert updated == 1
    assert record["status"] == "ongoing"
    assert record["amount"]["amount_usd_at_event"] is None
    assert record["amount"]["eth_usd_price_at_event"] is None
    assert record["amount"]["usd_estimate"] == "43.70"
    assert record["rarity"]["scope"] == "base_existing"
    assert "historical_dog_search_backfill" not in record["source"]["sources"]
    assert "historical_dog_search" not in record["source"]["notes"]
    assert "generated_auction_feed" in record["source"]["sources"]


def test_all_mission3_onchain_tables_override_stale_archive_and_preserve_transactions() -> None:
    unified = load_module()
    stale_wallet = "0x76d0e7a13248945ee9f808b4a472262b28778942"
    winner_wallet = "0xdbb811ec62338db94858ec21ef1d56b658111922"
    created_tx = "0x" + "c" * 64
    settled_tx = "0x" + "d" * 64
    record = {
        "mission": 3,
        "dog_id": 727,
        "status": "ended pending settlement",
        "winner_or_high_bidder": {"wallet": stale_wallet, "display": "@stale"},
        "amount": {"native": "0.01", "raw": "10000000000000000"},
        "bid_stats": {"bid_count": 2, "unique_bidder_count": 2},
        "auction_created": {"block_number": 46602034, "block_time_utc": "2026-05-28T18:36:55Z"},
        "settlement": {"settled": False, "block_number": None, "tx_hash": None},
        "links": {},
        "source": {"sources": ["base_logs"], "confidence": "verified"},
        "rarity": {"display": "#332/791", "rank": 332, "scope": "base_existing", "total": 791},
        "traits": [],
    }
    historical = {
        "mission": 3,
        "token_id": 727,
        "status": "settled",
        "winner": "@floam",
        "winner_url": "https://farcaster.xyz/floam",
        "winner_wallet": winner_wallet,
        "amount": "0.01100 ETH ($22)",
        "bid_count": 3,
        "unique_bidder_count": 3,
        "auction_created_time_utc": "2026-05-28 18:36:55",
        "settled_time_utc": "2026-05-29 18:38:33",
    }
    timeline = {
        "token_id": 727,
        "auction_state": "settled",
        "bids": 3,
        "unique_bidders": 3,
        "latest_bid_eth": "0.011",
        "latest_bid_utc": "2026-05-29 13:51:53",
        "settled_eth": "0.011",
        "settled_time_utc": "2026-05-29 18:38:33",
        "start_time_utc": "2026-05-28 18:36:55",
        "created_tx_hash": created_tx,
        "settled_tx_hash": settled_tx,
    }
    winner = {
        "token_id": 727,
        "winner_wallet": winner_wallet,
        "winner": "@floam",
        "winner_url": "https://farcaster.xyz/floam",
        "winning_bid_eth": "0.011",
        "winning_bid_usd_at_settlement": "22.08",
        "eth_usd_price_at_event": "2007.7116820979008",
        "eth_usd_price_date_utc": "2026-05-29",
        "usd_estimate_source": "defillama_coin_prices",
        "usd_estimate_source_detail": "coins.llama.fi/chart/coingecko:ethereum",
        "usd_estimate_confidence": "medium",
        "bid_count": 3,
        "unique_bidders": 3,
        "last_bid_utc": "2026-05-29 13:51:53",
        "settled_time_utc": "2026-05-29 18:38:33",
        "block_number": 46645283,
        "tx_hash": settled_tx,
    }

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old_paths = (
            unified.AUCTION_TIMELINE,
            unified.AUCTION_WINNERS,
            unified.HISTORICAL_SEARCH,
            unified.CURRENT_AUCTION,
        )
        old_mission3_source = unified.MISSION_INDEXES[3]
        try:
            unified.AUCTION_TIMELINE = tmp_path / "auction_timeline.json"
            unified.AUCTION_WINNERS = tmp_path / "auction_winners.json"
            unified.HISTORICAL_SEARCH = tmp_path / "historical_dog_search.json"
            unified.CURRENT_AUCTION = tmp_path / "current_auction.json"
            unified.MISSION_INDEXES[3] = tmp_path / "mission3_dog_search_index.json"
            unified.AUCTION_TIMELINE.write_text(json.dumps([timeline]), encoding="utf-8")
            unified.AUCTION_WINNERS.write_text(json.dumps([winner]), encoding="utf-8")
            unified.HISTORICAL_SEARCH.write_text(json.dumps([historical]), encoding="utf-8")
            unified.CURRENT_AUCTION.write_text("[]", encoding="utf-8")
            unified.MISSION_INDEXES[3].write_text(json.dumps([{
                "token_id": 727,
                "auction_created_tx": created_tx,
                "auction_created_block": 46602034,
            }]), encoding="utf-8")
            updated = unified.apply_mission3_onchain_overrides([record], {}, {})
        finally:
            (
                unified.AUCTION_TIMELINE,
                unified.AUCTION_WINNERS,
                unified.HISTORICAL_SEARCH,
                unified.CURRENT_AUCTION,
            ) = old_paths
            unified.MISSION_INDEXES[3] = old_mission3_source

    assert updated == 1
    assert record["status"] == "settled"
    assert record["winner_or_high_bidder"]["wallet"] == winner_wallet
    assert record["winner_or_high_bidder"]["display"] == "@floam"
    assert record["amount"]["native"] == "0.011"
    assert record["amount"]["usd_estimate_source"] == "defillama_coin_prices"
    assert record["bid_stats"]["bid_count"] == 3
    assert record["bid_stats"]["unique_bidder_count"] == 3
    assert record["auction_created"]["block_number"] == 46602034
    assert record["auction_created"]["tx_hash"] == created_tx
    assert record["settlement"]["block_number"] == 46645283
    assert record["settlement"]["tx_hash"] == settled_tx
    assert record["links"]["settlement_tx"].endswith(settled_tx)
    assert "generated_auction_timeline" in record["source"]["sources"]
    assert "generated_auction_winners" in record["source"]["sources"]
    assert settled_tx in record["search_text"]


def test_created_block_is_derived_from_matching_predecessor_settlement_transaction() -> None:
    unified = load_module()
    genesis_tx = "0x" + "1" * 64
    transition_tx = "0x" + "2" * 64
    winner_wallet = "0x" + "a" * 40
    records = [
        {
            "mission": 3,
            "dog_id": 590,
            "status": "settled",
            "winner_or_high_bidder": {},
            "amount": {},
            "bid_stats": {},
            "auction_created": {"block_number": None},
            "settlement": {},
            "links": {},
            "source": {"sources": []},
            "rarity": {},
            "traits": [],
        },
        {
            "mission": 3,
            "dog_id": 591,
            "status": "archive unresolved",
            "winner_or_high_bidder": {"wallet": "0x" + "b" * 40, "display": "@stale"},
            "amount": {"native": "9", "raw": "9000000000000000000"},
            "bid_stats": {},
            "auction_created": {"block_number": None},
            "settlement": {},
            "links": {},
            "source": {"sources": []},
            "rarity": {},
            "traits": [],
        },
    ]
    timeline = [
        {
            "token_id": 590,
            "auction_state": "settled",
            "bids": 1,
            "unique_bidders": 1,
            "latest_bid_eth": "1",
            "latest_bid_utc": "2026-01-01 00:30:00",
            "settled_eth": "1",
            "settled_time_utc": "2026-01-02 00:00:00",
            "start_time_utc": "2026-01-01 00:00:00",
            "created_tx_hash": genesis_tx,
            "settled_tx_hash": transition_tx,
        },
        {
            "token_id": 591,
            "auction_state": "live",
            "bids": 0,
            "unique_bidders": 0,
            "latest_bid_eth": None,
            "latest_bid_utc": None,
            "settled_eth": 0,
            "settled_time_utc": None,
            "start_time_utc": "2026-01-02 00:00:00",
            "created_tx_hash": transition_tx,
            "settled_tx_hash": None,
        },
    ]
    historical = [
        {
            "mission": 3,
            "token_id": 590,
            "status": "settled",
            "winner": "@winner",
            "winner_wallet": winner_wallet,
            "amount": "1 ETH ($1)",
            "bid_count": 1,
            "unique_bidder_count": 1,
            "auction_created_time_utc": "2026-01-01 00:00:00",
            "settled_time_utc": "2026-01-02 00:00:00",
        },
        {
            "mission": 3,
            "token_id": 591,
            "status": "live",
            "winner": "",
            "winner_wallet": "",
            "amount": "",
            "bid_count": 0,
            "unique_bidder_count": 0,
            "auction_created_time_utc": "2026-01-02 00:00:00",
            "settled_time_utc": "",
        },
    ]
    winners = [{
        "token_id": 590,
        "winner": "@winner",
        "winner_wallet": winner_wallet,
        "winning_bid_eth": "1",
        "bid_count": 1,
        "unique_bidders": 1,
        "last_bid_utc": "2026-01-01 00:30:00",
        "settled_time_utc": "2026-01-02 00:00:00",
        "block_number": 200,
        "tx_hash": transition_tx,
    }]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        old_paths = (
            unified.AUCTION_TIMELINE,
            unified.AUCTION_WINNERS,
            unified.HISTORICAL_SEARCH,
            unified.CURRENT_AUCTION,
        )
        old_mission3_source = unified.MISSION_INDEXES[3]
        try:
            unified.AUCTION_TIMELINE = tmp_path / "auction_timeline.json"
            unified.AUCTION_WINNERS = tmp_path / "auction_winners.json"
            unified.HISTORICAL_SEARCH = tmp_path / "historical_dog_search.json"
            unified.CURRENT_AUCTION = tmp_path / "current_auction.json"
            unified.MISSION_INDEXES[3] = tmp_path / "mission3_dog_search_index.json"
            unified.AUCTION_TIMELINE.write_text(json.dumps(timeline), encoding="utf-8")
            unified.AUCTION_WINNERS.write_text(json.dumps(winners), encoding="utf-8")
            unified.HISTORICAL_SEARCH.write_text(json.dumps(historical), encoding="utf-8")
            unified.CURRENT_AUCTION.write_text(json.dumps([{"token_id": 591, "auction_state": "live"}]), encoding="utf-8")
            unified.MISSION_INDEXES[3].write_text(json.dumps([
                {"token_id": 590, "auction_created_tx": genesis_tx, "auction_created_block": 100},
                {"token_id": 591, "auction_created_tx": transition_tx, "auction_created_block": None},
            ]), encoding="utf-8")
            updated = unified.apply_mission3_onchain_overrides(records, {}, {})
        finally:
            (
                unified.AUCTION_TIMELINE,
                unified.AUCTION_WINNERS,
                unified.HISTORICAL_SEARCH,
                unified.CURRENT_AUCTION,
            ) = old_paths
            unified.MISSION_INDEXES[3] = old_mission3_source

    assert updated == 2
    created = records[1]["auction_created"]
    assert created["tx_hash"] == transition_tx
    assert created["block_number"] == 200
    assert records[1]["activity_time_utc"] == "2026-01-02T00:00:00Z"
    assert records[1]["activity_time_basis"] == "auction_created_block_time"
    assert records[1]["winner_or_high_bidder"]["wallet"] is None
    assert records[1]["amount"]["native"] is None
    assert records[1]["amount"]["raw"] is None


def test_required_canonical_loader_rejects_empty_and_duplicate_token_tables() -> None:
    unified = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "canonical.json"
        path.write_text("[]", encoding="utf-8")
        assert_runtime_error_contains(lambda: unified.rows_by_dog(path, required=True), "is empty")
        path.write_text(json.dumps([{"token_id": 590}, {"token_id": 590}]), encoding="utf-8")
        assert_runtime_error_contains(lambda: unified.rows_by_dog(path, required=True), "duplicate Dog #590")


def test_farcaster_identity_url_requires_exact_https_host() -> None:
    unified = load_module()
    canonical = "https://farcaster.xyz/unitwinner"
    assert unified.canonical_farcaster_profile_url(canonical, "unitwinner") == canonical
    for unsafe in (
        "javascript:alert(1)",
        "http://farcaster.xyz/unitwinner",
        "https://evil.example/?next=farcaster.xyz/unitwinner",
        "https://farcaster.xyz.evil.example/unitwinner",
        "https://farcaster.xyz@evil.example/unitwinner",
    ):
        assert unified.canonical_farcaster_profile_url(unsafe, "unitwinner") == canonical


def test() -> None:
    tests = [
        test_ended_pending_settlement_feed_row_stays_in_unified_archive,
        test_stale_mission3_unsettled_archive_row_is_not_marked_ongoing,
        test_non_mission3_archive_status_labels_are_preserved,
        test_sort_tie_uses_dog_id_not_status_rank,
        test_settled_feed_amount_usd_is_reused_as_event_usd_for_archive_display,
        test_newest_sort_prioritizes_only_actual_current_before_recent_settled_rows,
        test_historical_mission3_backfill_keeps_settled_row_after_recent_feed_rolloff,
        test_historical_mission3_backfill_preserves_existing_bid_context,
        test_current_feed_override_drops_historical_backfill_provenance,
        test_all_mission3_onchain_tables_override_stale_archive_and_preserve_transactions,
        test_created_block_is_derived_from_matching_predecessor_settlement_transaction,
        test_required_canonical_loader_rejects_empty_and_duplicate_token_tables,
        test_farcaster_identity_url_requires_exact_https_host,
    ]
    for item in tests:
        item()
    print(f"build_unified_dog_index_tests=pass count={len(tests)}")


if __name__ == "__main__":
    test()
