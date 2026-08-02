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
    exact_quote = Decimal("31.84") / Decimal("0.0169")
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
            "usd_estimate_price_usd": str(exact_quote),
            "usd_estimate_price_date_utc": "2026-06-03",
            "usd_estimate_time_basis": "last_bid_block_time",
        },
        "auction_created": {"tx_hash": "0x" + "c" * 64},
        "bid_stats": {"last_bid_time_utc": "2026-06-03T02:05:09Z"},
        "bid_tx_hashes": ["0x" + "a" * 64, "0x" + "b" * 64],
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
    assert estimate["event_tx_hash"] == "0x" + "b" * 64
    assert estimate["event_time_utc"] == "2026-06-03T02:05:09Z"


def test_live_usd_without_exact_quote_is_not_reverse_engineered_from_rounded_display() -> None:
    archive_usd = load_module()
    record = live_feed_record()
    record["amount"].pop("usd_estimate_price_usd")

    estimate = archive_usd.update_record(record, {})

    assert record["amount"]["usd_estimate"] is None
    assert record["amount"]["usd_estimate_price_usd"] is None
    assert estimate is not None
    assert estimate["price_usd"] is None
    assert estimate["estimated_usd_value"] is None
    assert estimate["price_status"] == "missing"


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


def test_ended_pending_generated_feed_record_preserves_current_surface_usd() -> None:
    archive_usd = load_module()
    record = live_feed_record()
    record["status"] = "ended pending settlement"
    record["activity_time_utc"] = "2026-06-03T19:41:49Z"
    record["amount"]["native"] = "0.033"
    record["amount"]["usd_estimate"] = "54.74"
    record["amount"]["usd_estimate_display"] = "$54.74"
    record["amount"]["usd_estimate_price_usd"] = str(Decimal("54.74") / Decimal("0.033"))
    record["settlement"] = {"settled": False}
    price_map = {
        ("ETH", "2026-06-03"): {
            "asset_key": "ETH",
            "date_utc": "2026-06-03",
            "price_usd": "1687.0760837926352",
            "source": "unit_test_event_day_price",
            "source_detail": "unit-test historical endpoint",
            "confidence": "high",
        }
    }

    estimate = archive_usd.update_record(record, price_map)

    amount = record["amount"]
    assert amount["usd_estimate"] == "54.74000000"
    assert amount["usd_estimate_display"] == "$54.74"
    assert amount["usd_estimate_source"] == "generated_auction_feed"
    assert amount["amount_usd_at_event"] is None
    assert amount["eth_usd_price_at_event"] is None
    assert estimate is not None
    assert estimate["event_type"] == "auction_record"
    assert estimate["estimated_usd_display"] == "$54.74"
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
            "source_detail": "unit-test historical endpoint",
            "confidence": "high",
        }
    }

    estimate = archive_usd.update_record(record, price_map)

    amount = record["amount"]
    assert amount["usd_estimate"] == "50.00000000"
    assert amount["usd_estimate_display"] == "$50.00"
    assert amount["usd_estimate_source"] == "unit_test_event_day_price"
    assert amount["usd_estimate_source_detail"] == "unit-test historical endpoint"
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
    assert estimate["price_source_detail"] == "unit-test historical endpoint"


def test_settled_generated_feed_record_preserves_event_usd_when_historical_price_missing() -> None:
    archive_usd = load_module()
    record = live_feed_record()
    record["dog_id"] = 736
    record["status"] = "settled"
    record["activity_time_utc"] = "2026-06-07T20:10:25Z"
    record["settlement"] = {"settled": True, "block_time_utc": "2026-06-07T20:10:25Z"}
    record["amount"].update({
        "native": "0.02662",
        "raw": "26620000000000000",
        "usd_estimate": "48.22",
        "usd_estimate_display": "$48.22",
        "usd_estimate_source": "defillama_coin_prices",
        "usd_estimate_source_detail": "coins.llama.fi/chart/coingecko:ethereum",
        "usd_estimate_confidence": "medium",
        "usd_estimate_price_usd": "1811.346676900944",
        "usd_estimate_price_date_utc": "2026-06-04",
        "amount_usd_at_event": "48.22",
        "eth_usd_price_at_event": "1811.346676900944",
        "eth_usd_price_date_utc": "2026-06-04",
        "usd_estimate_time_basis": "settlement_block_time",
    })

    estimate = archive_usd.update_record(record, {})

    amount = record["amount"]
    assert amount["usd_estimate"] == "48.22000000"
    assert amount["usd_estimate_display"] == "$48.22"
    assert amount["usd_estimate_source"] == "defillama_coin_prices"
    assert amount["usd_estimate_confidence"] == "medium"
    assert amount["amount_usd_at_event"] == "48.22"
    assert amount["eth_usd_price_at_event"] == "1811.346676900944"
    assert amount["eth_usd_price_date_utc"] == "2026-06-04"
    assert estimate is not None
    assert estimate["event_type"] == "settlement"
    assert estimate["price_status"] == "priced"
    assert estimate["estimated_usd_display"] == "$48.22"
    assert estimate["price_source"] == "defillama_coin_prices"
    assert estimate["price_source_detail"] == "coins.llama.fi/chart/coingecko:ethereum"
    assert estimate["amount_usd_at_event"] == "48.22"
    assert estimate["eth_usd_price_at_event"] == "1811.346676900944"
    assert estimate["eth_usd_price_date_utc"] == "2026-06-04"


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


def test_archive_validator_accepts_ended_pending_current_surface_usd() -> None:
    validator = load_validator_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        unified = [
            {
                "mission": 3,
                "dog_id": 1,
                "status": "ended pending settlement",
                "settlement": {"settled": False},
                "amount": {
                    "native": "0.033",
                    "native_symbol": "ETH",
                    "price_asset_key": "ETH",
                    "usd_estimate": "54.67000000",
                    "usd_estimate_display": "$54.67",
                    "usd_estimate_source": "current_eth_usd_price",
                    "usd_estimate_confidence": "live_current",
                },
            }
        ] + [{"mission": 3, "dog_id": dog_id, "status": "created"} for dog_id in range(2, 702)]
        estimates = [
            {
                "mission": 3,
                "dog_id": 1,
                "event_type": "auction_record",
                "native_amount": "0.033",
                "price_asset_key": "ETH",
                "price_usd": "1656.6666666666667",
                "estimated_usd_value": "54.67000000",
                "estimated_usd_display": "$54.67",
                "price_date_utc": "2026-06-09",
                "price_source": "current_eth_usd_price",
                "price_confidence": "live_current",
                "price_status": "priced",
            }
        ]
        write_json(root / "archive" / "data" / "generated" / "unified_dog_search_index.json", unified)
        write_json(root / "public" / "generated" / "unified_dog_search_index.json", unified)
        write_json(root / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json", [
            {"asset_key": "ETH", "date_utc": "2026-06-09", "price_usd": "1656.6666666666667"},
            {"asset_key": "DEGEN", "date_utc": "2026-06-09", "price_usd": "0.01"},
        ])
        write_json(root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates.json", estimates)
        write_json(root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates_manifest.json", {"estimate_rows": 1})
        validator.ROOT = root
        validator.UNIFIED = root / "archive" / "data" / "generated" / "unified_dog_search_index.json"
        validator.PUBLIC_UNIFIED = root / "public" / "generated" / "unified_dog_search_index.json"
        validator.PRICES = root / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
        validator.ESTIMATES = root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates.json"
        validator.MANIFEST = root / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates_manifest.json"
        validator.main()


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


def canonical_event_amount() -> dict:
    return {
        "amount_usd_at_event": "20.12345678",
        "eth_usd_price_at_event": "2012.345678",
        "eth_usd_price_date_utc": "2026-08-01",
        "usd_estimate_source": "defillama_coin_prices",
        "usd_estimate_source_detail": "coins.llama.fi/chart/coingecko:ethereum",
        "usd_estimate_confidence": "medium",
    }


def test_archive_validator_rejects_surface_provenance_drift() -> None:
    validator = load_validator_module()
    canonical = canonical_event_amount()
    surface = dict(canonical)
    surface["amount_usd_at_event"] = "20.12"
    surface["usd_estimate_source"] = "current_eth_usd_price"

    try:
        validator.validate_surface_provenance(
            dog_id=789,
            canonical=canonical,
            surface=surface,
            label="auction_feed",
            event_amount_field="amount_usd_at_event",
        )
    except SystemExit as exc:
        assert "usd_estimate_source differs from archive" in str(exc)
    else:
        raise AssertionError("validator accepted live provenance drift on settled auction_feed row")


def test_archive_validator_requires_settlement_block_on_winner_row() -> None:
    validator = load_validator_module()
    canonical = canonical_event_amount()
    record = {
        "mission": 3,
        "dog_id": 789,
        "status": "settled",
        "settlement": {"settled": True, "tx_hash": "0x" + "a" * 64},
        "amount": canonical,
    }
    winner = dict(canonical)
    winner.update(
        {
            "token_id": 789,
            "winning_bid_usd_at_settlement": "20.12",
            "tx_hash": "0x" + "a" * 64,
            "block_number": "",
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        validator.ROOT = Path(tmp)
        try:
            validator.validate_archive_surface_parity(
                record=record,
                amount=canonical,
                feed_by_dog={},
                winner_by_dog={789: winner},
            )
        except SystemExit as exc:
            assert "settlement block missing" in str(exc)
        else:
            raise AssertionError("validator accepted a settled winner transaction without its block")


def current_bid_provenance_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    bid_hash = "0x" + "b" * 64
    record = {
        "mission": 3,
        "dog_id": 791,
        "status": "ongoing",
        "activity_time_utc": "2026-08-02T13:17:09Z",
        "bid_stats": {"last_bid_time_utc": "2026-08-02T13:17:09Z"},
        "bid_tx_hashes": [bid_hash],
    }
    amount = {
        "native": "0.009",
        "usd_estimate": "16.67763000",
        "usd_estimate_price_usd": "1853.07",
        "usd_estimate_price_date_utc": "2026-08-02",
    }
    row = {
        "event_type": "current_bid",
        "event_time_utc": "2026-08-02T13:17:09Z",
        "event_tx_hash": bid_hash,
        "price_usd": "1853.07",
        "estimated_usd_value": "16.67763000",
    }
    return record, amount, row


def test_archive_validator_accepts_exact_current_bid_provenance() -> None:
    validator = load_validator_module()
    record, amount, row = current_bid_provenance_fixture()
    validator.validate_current_bid_provenance(
        mission=3,
        dog_id=791,
        record=record,
        amount=amount,
        row=row,
        current_by_dog={
            791: {
                "token_id": 791,
                "eth_usd_price_live": "1853.07",
                "eth_usd_price_date_utc": "2026-08-02",
            }
        },
    )


def test_archive_validator_rejects_current_bid_creation_tx_and_rounded_quote_inversion() -> None:
    validator = load_validator_module()
    record, amount, row = current_bid_provenance_fixture()
    row["event_tx_hash"] = "0x" + "c" * 64
    try:
        validator.validate_current_bid_provenance(
            mission=3,
            dog_id=791,
            record=record,
            amount=amount,
            row=row,
            current_by_dog={},
        )
    except SystemExit as exc:
        assert "transaction provenance mismatch" in str(exc)
    else:
        raise AssertionError("validator accepted auction creation tx as current bid provenance")

    _record, amount, row = current_bid_provenance_fixture()
    amount["usd_estimate_price_usd"] = str(Decimal("16.68") / Decimal("0.009"))
    row["price_usd"] = amount["usd_estimate_price_usd"]
    try:
        validator.validate_current_bid_provenance(
            mission=3,
            dog_id=791,
            record=_record,
            amount=amount,
            row=row,
            current_by_dog={791: {"eth_usd_price_live": "1853.07", "eth_usd_price_date_utc": "2026-08-02"}},
        )
    except SystemExit as exc:
        assert "exact quote" in str(exc) or "differs from archive" in str(exc)
    else:
        raise AssertionError("validator accepted a quote inverted from rounded display USD")


def test() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for item in tests:
        item()
    print(f"archive_apply_usd_estimates_tests=pass count={len(tests)}")


if __name__ == "__main__":
    test()
