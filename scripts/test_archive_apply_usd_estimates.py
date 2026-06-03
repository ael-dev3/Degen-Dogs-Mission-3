#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "archive_apply_usd_estimates.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("archive_apply_usd_estimates", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def live_feed_record() -> dict[str, Any]:
    return {
        "mission": 3,
        "dog_id": 732,
        "chain": "Base",
        "chain_id": 8453,
        "status": "ongoing",
        "activity_time_utc": "2026-06-03T02:05:09Z",
        "amount": {
            "native": "0.0169",
            "native_symbol": "ETH",
            "price_asset_key": "ETH",
            "raw": "16900000000000000",
            "usd_estimate": "31.84",
            "usd_estimate_display": "$31.84",
            "usd_estimate_source": "generated_auction_feed",
            "usd_estimate_confidence": "medium",
            "usd_estimate_time_basis": "last_bid_block_time",
        },
        "source": {"sources": ["generated_auction_feed"]},
    }


def test_preserves_generated_feed_usd_when_historical_price_missing() -> None:
    archive_usd = load_module()
    record = live_feed_record()

    estimate = archive_usd.update_record(record, {})

    amount = record["amount"]
    assert amount["usd_estimate"] == "31.84000000"
    assert amount["usd_estimate_display"] == "$31.84"
    assert amount["usd_estimate_source"] == "generated_auction_feed"
    assert amount["usd_estimate_confidence"] == "medium"
    assert estimate is not None
    assert estimate["price_status"] == "priced"
    assert estimate["estimated_usd_display"] == "$31.84"
    assert Decimal(estimate["price_usd"]) == Decimal("31.84") / Decimal("0.0169")


def test_generated_feed_usd_beats_stale_or_mismatched_historical_price() -> None:
    archive_usd = load_module()
    record = live_feed_record()
    price_map = {
        ("ETH", "2026-06-03"): {
            "asset_key": "ETH",
            "date_utc": "2026-06-03",
            "price_usd": "2000",
            "source": "unit_test_stale_daily_price",
            "confidence": "high",
            "notes": "Would make the latest bid display $33.80, not the generated auction-feed value.",
        }
    }

    estimate = archive_usd.update_record(record, price_map)

    amount = record["amount"]
    assert amount["usd_estimate"] == "31.84000000"
    assert amount["usd_estimate_display"] == "$31.84"
    assert amount["usd_estimate_source"] == "generated_auction_feed"
    assert estimate is not None
    assert estimate["estimated_usd_display"] == "$31.84"
    assert estimate["price_source"] == "generated_auction_feed"


def test_regular_archive_record_still_uses_historical_price() -> None:
    archive_usd = load_module()
    record = {
        "mission": 1,
        "dog_id": 42,
        "chain": "Polygon",
        "chain_id": 137,
        "status": "settled",
        "activity_time_utc": "2023-01-02T00:00:00Z",
        "amount": {"native": "0.5", "native_symbol": "WETH", "price_asset_key": "ETH", "raw": "500000000000000000"},
    }
    price_map = {
        ("ETH", "2023-01-02"): {
            "asset_key": "ETH",
            "date_utc": "2023-01-02",
            "price_usd": "1200",
            "source": "unit_test_daily_price",
            "confidence": "high",
        }
    }

    estimate = archive_usd.update_record(record, price_map)

    amount = record["amount"]
    assert amount["usd_estimate"] == "600.00000000"
    assert amount["usd_estimate_display"] == "$600.00"
    assert amount["usd_estimate_source"] == "unit_test_daily_price"
    assert estimate is not None
    assert estimate["price_status"] == "priced"
    assert estimate["price_usd"] == "1200"


def test() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for item in tests:
        item()
    print(f"archive_apply_usd_estimates_tests=pass count={len(tests)}")


if __name__ == "__main__":
    test()
