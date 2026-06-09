#!/usr/bin/env python3
"""Validate historical USD estimate artifacts and unified index enrichment."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNIFIED = ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json"
PUBLIC_UNIFIED = ROOT / "public" / "generated" / "unified_dog_search_index.json"
PRICES = ROOT / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
ESTIMATES = ROOT / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates.json"
MANIFEST = ROOT / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates_manifest.json"
LIVE_USD_SOURCES = {"generated_auction_feed", "current_eth_usd_price", "token_stats.eth_usd_price"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def decimal_or_none(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def fail(message: str) -> None:
    raise SystemExit(f"historical USD validation failed: {message}")


def text_value(value: Any) -> str:
    return str(value or "").strip()


def is_live_or_current(record: dict[str, Any]) -> bool:
    status = text_value(record.get("status")).lower()
    raw_settlement = record.get("settlement")
    settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
    if settlement.get("settled"):
        return False
    return status in {"ongoing", "live", "ended pending settlement", "ended_unsettled"}


def validate_historical_event_provenance(
    *,
    mission: Any,
    dog_id: Any,
    record: dict[str, Any],
    amount: dict[str, Any],
    row: dict[str, Any],
) -> None:
    if is_live_or_current(record):
        return
    source = text_value(amount.get("usd_estimate_source") or row.get("price_source"))
    if source in LIVE_USD_SOURCES:
        fail(f"historical event USD provenance uses live/current source for mission {mission} dog {dog_id}")
    required_amount_fields = ["amount_usd_at_event", "eth_usd_price_at_event", "eth_usd_price_date_utc"]
    missing_amount_fields = [field for field in required_amount_fields if text_value(amount.get(field)) == ""]
    required_row_fields = ["amount_usd_at_event", "eth_usd_price_at_event", "eth_usd_price_date_utc"]
    missing_row_fields = [field for field in required_row_fields if text_value(row.get(field)) == ""]
    if missing_amount_fields or missing_row_fields:
        fail(
            "historical event USD provenance missing "
            f"for mission {mission} dog {dog_id}: amount={missing_amount_fields} estimate={missing_row_fields}"
        )
    amount_usd = decimal_or_none(amount.get("amount_usd_at_event"))
    row_usd = decimal_or_none(row.get("amount_usd_at_event"))
    if amount_usd is None or row_usd is None or amount_usd != row_usd:
        fail(f"historical event USD provenance amount mismatch for mission {mission} dog {dog_id}")
    amount_price = decimal_or_none(amount.get("eth_usd_price_at_event"))
    row_price = decimal_or_none(row.get("eth_usd_price_at_event"))
    if amount_price is None or row_price is None or amount_price != row_price:
        fail(f"historical event USD provenance ETH price mismatch for mission {mission} dog {dog_id}")
    if text_value(amount.get("eth_usd_price_date_utc")) != text_value(row.get("eth_usd_price_date_utc")):
        fail(f"historical event USD provenance date mismatch for mission {mission} dog {dog_id}")


def main() -> None:
    for path in [UNIFIED, PUBLIC_UNIFIED, PRICES, ESTIMATES, MANIFEST]:
        if not path.exists():
            fail(f"missing {path.relative_to(ROOT)}")
    unified = load_json(UNIFIED)
    public = load_json(PUBLIC_UNIFIED)
    prices = load_json(PRICES)
    estimates = load_json(ESTIMATES)
    manifest = load_json(MANIFEST)
    if unified != public:
        fail("archive and public unified search indexes differ")
    if not isinstance(unified, list) or len(unified) < 700:
        fail("unified index is missing expected cross-mission records")
    if not isinstance(prices, list) or not prices:
        fail("historical price table is empty")
    if not isinstance(estimates, list) or not estimates:
        fail("auction USD estimates table is empty")

    required_assets = {"ETH", "DEGEN"}
    priced_assets = {row.get("asset_key") for row in prices if isinstance(row, dict)}
    missing_assets = required_assets - priced_assets
    if missing_assets:
        fail(f"price rows missing assets: {sorted(missing_assets)}")

    estimate_by_key = {(row.get("mission"), row.get("dog_id")): row for row in estimates if isinstance(row, dict)}
    priced = 0
    missing = 0
    for record in unified:
        if not isinstance(record, dict):
            fail("unified index contains non-object row")
        mission = record.get("mission")
        dog_id = record.get("dog_id")
        raw_amount = record.get("amount")
        amount: dict[str, Any] = raw_amount if isinstance(raw_amount, dict) else {}
        native = decimal_or_none(amount.get("native"))
        if native is None:
            # Non-auction rows can legitimately lack amounts.
            continue
        row = estimate_by_key.get((mission, dog_id))
        if not isinstance(row, dict):
            fail(f"missing estimate row for mission {mission} dog {dog_id}")
        status = row.get("price_status")
        if status == "priced":
            priced += 1
            price = decimal_or_none(row.get("price_usd"))
            usd = decimal_or_none(row.get("estimated_usd_value"))
            if price is None or usd is None or price <= 0 or usd < 0:
                fail(f"invalid priced estimate for mission {mission} dog {dog_id}")
            if native > 0 and usd <= 0:
                fail(f"positive native amount priced to non-positive USD for mission {mission} dog {dog_id}")
            if not row.get("price_source") or not row.get("price_date_utc"):
                fail(f"priced estimate lacks provenance for mission {mission} dog {dog_id}")
            validate_historical_event_provenance(
                mission=mission,
                dog_id=dog_id,
                record=record,
                amount=amount,
                row=row,
            )
        elif status == "missing":
            missing += 1
            if row.get("estimated_usd_value") not in (None, ""):
                fail(f"missing estimate has fabricated USD value for mission {mission} dog {dog_id}")
        else:
            fail(f"invalid price_status {status!r} for mission {mission} dog {dog_id}")

    if priced == 0:
        fail("no records priced")
    if int(manifest.get("estimate_rows", -1)) != len(estimates):
        fail("estimate manifest row count mismatch")
    print(json.dumps({"status": "ok", "priced_rows": priced, "missing_rows": missing, "estimate_rows": len(estimates)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
