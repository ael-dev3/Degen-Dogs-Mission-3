#!/usr/bin/env python3
"""Apply historical USD estimates to unified Dog auction records."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 80

ROOT = Path(__file__).resolve().parents[1]
PRICES = ROOT / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
OUT_DIR = ROOT / "archive" / "prices" / "data" / "generated"
ARCHIVE_UNIFIED = ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json"
PUBLIC_UNIFIED = ROOT / "public" / "generated" / "unified_dog_search_index.json"
DOG_ARCHIVE = ROOT / "archive" / "dogs" / "by-id"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def decimal_or_none(value: Any) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def parse_day(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def day_key(value: Any) -> str | None:
    parsed = parse_day(value)
    return parsed.date().isoformat() if parsed else None


def ensure_unified_index() -> None:
    if ARCHIVE_UNIFIED.exists():
        return
    subprocess.run([sys.executable, "scripts/build_unified_dog_index.py"], cwd=ROOT, check=True)


def load_price_map() -> dict[tuple[str, str], dict[str, Any]]:
    rows = load_json(PRICES, [])
    price_map: dict[tuple[str, str], dict[str, Any]] = {}
    if not isinstance(rows, list):
        return price_map
    for row in rows:
        if not isinstance(row, dict):
            continue
        asset = str(row.get("asset_key") or "").strip()
        date_utc = str(row.get("date_utc") or "").strip()
        price = decimal_or_none(row.get("price_usd"))
        if asset and date_utc and price is not None:
            price_map.setdefault((asset, date_utc), row)
    return price_map


def find_price(price_map: dict[tuple[str, str], dict[str, Any]], asset: str, event_day: str | None) -> tuple[dict[str, Any] | None, str]:
    if not event_day:
        return None, "missing_event_time"
    if (asset, event_day) in price_map:
        return price_map[(asset, event_day)], "same_day"
    try:
        parsed = datetime.strptime(event_day, "%Y-%m-%d").date()
    except ValueError:
        return None, "invalid_event_time"
    for days_back in range(1, 4):
        candidate = (parsed - timedelta(days=days_back)).isoformat()
        if (asset, candidate) in price_map:
            row = dict(price_map[(asset, candidate)])
            row["confidence"] = "medium"
            row["notes"] = (row.get("notes") or "") + f" Used nearest prior price {days_back} day(s) before event."
            return row, "nearest_prior"
    return None, "missing_price"


def money_display(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${rounded:,.2f}"


def update_record(record: dict[str, Any], price_map: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any] | None:
    raw_amount = record.get("amount")
    amount: dict[str, Any] = raw_amount if isinstance(raw_amount, dict) else {}
    native = decimal_or_none(amount.get("native"))
    if native is None:
        return None
    asset = str(amount.get("price_asset_key") or "").strip()
    event_day = day_key(record.get("activity_time_utc"))
    price_row, status = find_price(price_map, asset, event_day)
    estimate: Decimal | None = None
    price_usd: Decimal | None = None
    if price_row:
        price_usd = decimal_or_none(price_row.get("price_usd"))
        if price_usd is not None:
            estimate = native * price_usd
            amount["usd_estimate"] = str(estimate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
            amount["usd_estimate_display"] = money_display(estimate)
            amount["usd_estimate_source"] = price_row.get("source")
            amount["usd_estimate_confidence"] = price_row.get("confidence") or "high"
            amount["usd_estimate_price_date_utc"] = price_row.get("date_utc")
            amount["usd_estimate_price_usd"] = str(price_usd)
            amount["usd_estimate_notes"] = price_row.get("notes") or ""
    if estimate is None:
        amount["usd_estimate"] = None
        amount["usd_estimate_display"] = None
        amount["usd_estimate_source"] = None
        amount["usd_estimate_confidence"] = "missing"
        amount["usd_estimate_price_date_utc"] = None
        amount["usd_estimate_price_usd"] = None
        amount["usd_estimate_notes"] = status
    record["amount"] = amount
    raw_settlement = record.get("settlement")
    settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
    raw_created = record.get("auction_created")
    auction_created: dict[str, Any] = raw_created if isinstance(raw_created, dict) else {}
    event_type = "settlement" if settlement.get("settled") else ("current_bid" if str(record.get("status", "")).lower() in {"ongoing", "live"} else "auction_record")
    return {
        "mission": record.get("mission"),
        "dog_id": record.get("dog_id"),
        "chain": record.get("chain"),
        "chain_id": record.get("chain_id"),
        "event_type": event_type,
        "event_time_utc": record.get("activity_time_utc"),
        "event_tx_hash": (settlement.get("tx_hash") or auction_created.get("tx_hash")),
        "native_amount_raw": amount.get("raw"),
        "native_amount": amount.get("native"),
        "native_symbol": amount.get("native_symbol"),
        "price_asset_key": asset,
        "price_usd": str(price_usd) if price_usd is not None else None,
        "estimated_usd_value": str(estimate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)) if estimate is not None else None,
        "estimated_usd_display": money_display(estimate) if estimate is not None else None,
        "price_date_utc": price_row.get("date_utc") if price_row else None,
        "price_source": price_row.get("source") if price_row else None,
        "price_source_detail": price_row.get("source_detail") if price_row else None,
        "price_confidence": amount.get("usd_estimate_confidence"),
        "price_status": "priced" if estimate is not None else "missing",
        "notes": amount.get("usd_estimate_notes") or "",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = [
        "mission", "dog_id", "chain", "chain_id", "event_type", "event_time_utc", "event_tx_hash",
        "native_amount_raw", "native_amount", "native_symbol", "price_asset_key", "price_usd",
        "estimated_usd_value", "estimated_usd_display", "price_date_utc", "price_source", "price_source_detail",
        "price_confidence", "price_status", "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_per_dog(records: list[dict[str, Any]]) -> None:
    DOG_ARCHIVE.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    for record in records:
        dog_id = record.get("dog_id")
        if dog_id is None:
            continue
        path = DOG_ARCHIVE / f"{int(dog_id):03d}.json"
        existing = load_json(path, {})
        generated_at = now
        if isinstance(existing, dict):
            generated_at = str(existing.get("generated_at_utc") or now)
            if existing.get("record") == record:
                continue
        write_json(path, {"schema_version": 1, "generated_at_utc": generated_at, "record": record})


def main() -> None:
    ensure_unified_index()
    records = load_json(ARCHIVE_UNIFIED, [])
    if not isinstance(records, list):
        raise SystemExit("unified index is not a JSON array")
    price_map = load_price_map()
    estimates: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        estimate = update_record(record, price_map)
        if estimate:
            estimates.append(estimate)

    write_json(OUT_DIR / "auction_usd_estimates.json", estimates)
    write_csv(OUT_DIR / "auction_usd_estimates.csv", estimates)
    write_json(ARCHIVE_UNIFIED, records)
    write_json(PUBLIC_UNIFIED, records)
    write_per_dog(records)
    missing = sum(1 for row in estimates if row.get("price_status") == "missing")
    summary = {"updated_at_utc": utc_now(), "estimate_rows": len(estimates), "priced_rows": len(estimates) - missing, "missing_rows": missing}
    write_json(OUT_DIR / "auction_usd_estimates_manifest.json", summary)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
