#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "archive_apply_usd_estimates.py"
VALIDATOR_MODULE_PATH = ROOT / "scripts" / "archive_validate_usd_estimates.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("archive_apply_usd_estimates", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_validator_module() -> Any:
    spec = importlib.util.spec_from_file_location("archive_validate_usd_estimates", VALIDATOR_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    assert amount["amount_usd_at_event"] is None
    assert amount["eth_usd_price_at_event"] is None
    assert estimate is not None
    assert estimate["price_status"] == "priced"
    assert estimate["estimated_usd_display"] == "$31.84"
    assert Decimal(estimate["price_usd"]) == Decimal("31.84") / Decimal("0.0169")


def test_live_generated_feed_usd_beats_stale_or_mismatched_historical_price() -> None:
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


def test_settled_generated_feed_record_uses_historical_event_price_not_current_feed_usd() -> None:
    archive_usd = load_module()
    record = live_feed_record()
    record["status"] = "settled"
    record["activity_time_utc"] = "2026-06-03T19:41:49Z"
    record["amount"]["native"] = "0.05"
    record["amount"]["usd_estimate"] = "500.00"
    record["amount"]["usd_estimate_display"] = "$500.00"
    record["settlement"] = {"settled": True, "block_time_utc": "2026-06-03T19:41:49Z"}
    price_map = {
        ("ETH", "2026-06-03"): {
            "asset_key": "ETH",
            "date_utc": "2026-06-03",
            "price_usd": "1000",
            "source": "unit_test_event_day_price",
            "confidence": "high",
        }
    }

    estimate = archive_usd.update_record(record, price_map)

    amount = record["amount"]
    assert amount["usd_estimate"] == "50.00000000"
    assert amount["usd_estimate_display"] == "$50.00"
    assert amount["usd_estimate_source"] == "unit_test_event_day_price"
    assert amount["usd_estimate_confidence"] == "high"
    assert amount["amount_usd_at_event"] == "50.00000000"
    assert amount["eth_usd_price_at_event"] == "1000"
    assert amount["eth_usd_price_date_utc"] == "2026-06-03"
    assert estimate is not None
    assert estimate["event_type"] == "settlement"
    assert estimate["estimated_usd_display"] == "$50.00"
    assert estimate["amount_usd_at_event"] == "50.00000000"
    assert estimate["eth_usd_price_at_event"] == "1000"
    assert estimate["eth_usd_price_date_utc"] == "2026-06-03"
    assert estimate["price_source"] == "unit_test_event_day_price"


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
    assert amount["amount_usd_at_event"] == "600.00000000"
    assert amount["eth_usd_price_at_event"] == "1200"
    assert amount["eth_usd_price_date_utc"] == "2023-01-02"
    assert estimate is not None
    assert estimate["price_status"] == "priced"
    assert estimate["price_usd"] == "1200"


def test_regular_archive_record_uses_nearest_daily_price_when_exact_day_is_absent() -> None:
    archive_usd = load_module()
    record = {
        "mission": 3,
        "dog_id": 729,
        "chain": "Base",
        "chain_id": 8453,
        "status": "settled",
        "activity_time_utc": "2026-05-31T19:12:29Z",
        "settlement": {"settled": True, "block_time_utc": "2026-05-31T19:12:29Z"},
        "amount": {"native": "0.01", "native_symbol": "ETH", "price_asset_key": "ETH", "raw": "10000000000000000"},
    }
    price_map = {
        ("ETH", "2026-06-01"): {
            "asset_key": "ETH",
            "date_utc": "2026-06-01",
            "price_usd": "2000",
            "source": "unit_test_nearest_daily_price",
            "confidence": "medium",
            "timestamp_utc": "2026-06-01T00:00:02Z",
        }
    }

    estimate = archive_usd.update_record(record, price_map)

    amount = record["amount"]
    assert amount["usd_estimate"] == "20.00000000"
    assert amount["amount_usd_at_event"] == "20.00000000"
    assert amount["eth_usd_price_at_event"] == "2000"
    assert amount["eth_usd_price_date_utc"] == "2026-06-01"
    assert amount["usd_estimate_source"] == "unit_test_nearest_daily_price"
    assert estimate is not None
    assert estimate["price_status"] == "priced"
    assert "nearest available daily price" in estimate["notes"]


def test_archive_validator_rejects_settled_current_price_without_event_provenance() -> None:
    validator = load_validator_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        unified = [
            {
                "mission": 3,
                "dog_id": 1,
                "status": "settled",
                "amount": {
                    "native": "0.05",
                    "native_symbol": "ETH",
                    "price_asset_key": "ETH",
                    "usd_estimate": "500.00000000",
                    "usd_estimate_display": "$500.00",
                    "usd_estimate_source": "generated_auction_feed",
                    "usd_estimate_confidence": "medium",
                },
            }
        ] + [{"mission": 3, "dog_id": dog_id, "status": "created"} for dog_id in range(2, 702)]
        estimates = [
            {
                "mission": 3,
                "dog_id": 1,
                "event_type": "settlement",
                "native_amount": "0.05",
                "price_asset_key": "ETH",
                "price_usd": "10000",
                "estimated_usd_value": "500.00000000",
                "estimated_usd_display": "$500.00",
                "price_date_utc": "2026-06-03",
                "price_source": "generated_auction_feed",
                "price_confidence": "medium",
                "price_status": "priced",
            }
        ]
        write_json(root / "archive" / "data" / "generated" / "unified_dog_search_index.json", unified)
        write_json(root / "public" / "generated" / "unified_dog_search_index.json", unified)
        write_json(root / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json", [
            {"asset_key": "ETH", "date_utc": "2026-06-03", "price_usd": "1000"},
            {"asset_key": "DEGEN", "date_utc": "2026-06-03", "price_usd": "0.01"},
        ])
        write_json(root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates.json", estimates)
        write_json(root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates_manifest.json", {"estimate_rows": 1})
        validator.ROOT = root
        validator.UNIFIED = root / "archive" / "data" / "generated" / "unified_dog_search_index.json"
        validator.PUBLIC_UNIFIED = root / "public" / "generated" / "unified_dog_search_index.json"
        validator.PRICES = root / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
        validator.ESTIMATES = root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates.json"
        validator.MANIFEST = root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates_manifest.json"
        try:
            validator.main()
        except SystemExit as exc:
            assert "historical event USD provenance" in str(exc)
        else:
            raise AssertionError("validator accepted current-price USD for a settled historical row")


def test() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for item in tests:
        item()
    print(f"archive_apply_usd_estimates_tests=pass count={len(tests)}")


if __name__ == "__main__":
    test()
