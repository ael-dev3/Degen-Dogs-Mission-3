#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
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
    assert unified.record_sort_key(record)[0] == 1
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
    assert unified.record_sort_key(record)[0] == 1
    assert "ongoing" not in record["search_text"]


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


def test() -> None:
    tests = [
        test_ended_pending_settlement_feed_row_stays_in_unified_archive,
        test_stale_mission3_unsettled_archive_row_is_not_marked_ongoing,
        test_settled_feed_amount_usd_is_reused_as_event_usd_for_archive_display,
    ]
    for item in tests:
        item()
    print(f"build_unified_dog_index_tests=pass count={len(tests)}")


if __name__ == "__main__":
    test()
