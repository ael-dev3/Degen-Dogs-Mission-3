#!/usr/bin/env python3
"""Apply historical USD estimates to unified Dog auction records."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def decimal_or_none(value: Any) -> Decimal | None:
    text = "" if value is None else str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


LIVE_USD_SOURCES = {
    "generated_auction_feed",
    "generated_current_auction",
    "generated_current_latest_bid",
    "generated_recent_bids",
    "current_eth_usd_price",
    "token_stats.eth_usd_price",
}


def text_value(value: Any) -> str:
    return "" if value is None else str(value).strip()


def is_explicit_integer_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return (isinstance(value, int) and value == 0) or (
        isinstance(value, str) and value.strip() == "0"
    )


def is_canonical_live_zero_bid(
    record: dict[str, Any],
    amount: dict[str, Any],
    bid_stats: dict[str, Any],
    raw_bid_hashes: Any,
) -> bool:
    if text_value(record.get("status")).lower() not in {"ongoing", "live"}:
        return False
    if (
        decimal_or_none(amount.get("native")) != Decimal(0)
        or not is_explicit_integer_zero(amount.get("raw"))
    ):
        return False
    if (
        not is_explicit_integer_zero(bid_stats.get("bid_count"))
        or not is_explicit_integer_zero(bid_stats.get("unique_bidder_count"))
        or not isinstance(raw_bid_hashes, list)
        or raw_bid_hashes
    ):
        return False
    raw_who = record.get("winner_or_high_bidder")
    who: dict[str, Any] = raw_who if isinstance(raw_who, dict) else {}
    wallet = text_value(who.get("wallet")).lower()
    return wallet in {"", ZERO_ADDRESS}


def source_tokens(record: dict[str, Any], amount: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ["usd_estimate_source", "usd_estimate_confidence"]:
        token = text_value(amount.get(key))
        if token:
            tokens.add(token.lower())
    raw_source = record.get("source")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    for key in ["raw_confidence", "confidence"]:
        token = text_value(source.get(key))
        if token:
            tokens.add(token.lower())
    raw_sources = source.get("sources")
    if isinstance(raw_sources, list):
        tokens.update(text_value(item).lower() for item in raw_sources if text_value(item))
    elif isinstance(raw_sources, str):
        tokens.update(part.strip().lower() for part in raw_sources.split(",") if part.strip())
    return tokens


def should_preserve_source_usd(record: dict[str, Any], amount: dict[str, Any]) -> bool:
    raw_settlement = record.get("settlement")
    settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
    status = text_value(record.get("status")).lower()
    has_live_source = bool(source_tokens(record, amount) & LIVE_USD_SOURCES)
    if settlement.get("settled") or status == "settled":
        return False
    if status in {"ended pending settlement", "ended_unsettled"}:
        return has_live_source and not has_event_usd_provenance(amount)
    if status and status not in {"ongoing", "live"}:
        return False
    return has_live_source


def has_event_usd_provenance(amount: dict[str, Any]) -> bool:
    return all(text_value(amount.get(field)) for field in ["amount_usd_at_event", "eth_usd_price_at_event", "eth_usd_price_date_utc"])


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


def find_price(price_map: dict[tuple[str, str], dict[str, Any]], asset: str, event_time: Any) -> tuple[dict[str, Any] | None, str]:
    parsed_event = parse_day(event_time)
    if not parsed_event:
        return None, "missing_event_time"
    if parsed_event.tzinfo is None:
        parsed_event = parsed_event.replace(tzinfo=timezone.utc)
    event_day = parsed_event.date().isoformat()
    if (asset, event_day) in price_map:
        return price_map[(asset, event_day)], "same_day"
    candidates: list[tuple[float, dict[str, Any]]] = []
    for (row_asset, row_day), row in price_map.items():
        if row_asset != asset:
            continue
        row_time = parse_day(row.get("timestamp_utc")) or parse_day(row_day)
        if not row_time:
            continue
        if row_time.tzinfo is None:
            row_time = row_time.replace(tzinfo=timezone.utc)
        distance = abs((row_time - parsed_event).total_seconds())
        if distance <= 3 * 86400:
            candidates.append((distance, row))
    if candidates:
        distance, base_row = min(candidates, key=lambda item: item[0])
        row = dict(base_row)
        days = distance / 86400
        row["confidence"] = row.get("confidence") or "medium"
        row["notes"] = (
            (row.get("notes") or "").rstrip()
            + f" Used nearest available daily price ({row.get('date_utc')}) {days:.2f} day(s) from event."
        ).strip()
        return row, "nearest_daily"
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
    price_row, status = find_price(price_map, asset, record.get("activity_time_utc"))
    estimate: Decimal | None = None
    price_usd: Decimal | None = None
    price_source: Any = None
    price_source_detail: Any = None
    price_date_utc: Any = None
    price_confidence: Any = None
    notes: Any = ""

    source_estimate = decimal_or_none(amount.get("usd_estimate"))
    raw_settlement = record.get("settlement")
    settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
    status_text = text_value(record.get("status")).lower()
    is_settled = bool(settlement.get("settled")) or status_text == "settled"
    source_has_generated_feed = bool(source_tokens(record, amount) & LIVE_USD_SOURCES)
    amount_source = text_value(amount.get("usd_estimate_source")).lower()
    has_historical_event_source = bool(amount_source and amount_source not in LIVE_USD_SOURCES and has_event_usd_provenance(amount))
    preserve_source = should_preserve_source_usd(record, amount)
    if (
        not preserve_source
        and price_row is None
        and source_estimate is not None
        and source_estimate >= 0
        and ((not is_settled and source_has_generated_feed) or (is_settled and has_historical_event_source))
    ):
        preserve_source = True

    existing_event_price = decimal_or_none(amount.get("eth_usd_price_at_event"))
    existing_quoted_price = decimal_or_none(amount.get("usd_estimate_price_usd"))
    explicit_source_price = existing_event_price or existing_quoted_price
    # A display USD value is currency-rounded and cannot be inverted into the
    # exact source ETH/USD quote. Without an explicit quote, use a dated price
    # row (if available) or mark the estimate missing instead of inventing one.
    if preserve_source and explicit_source_price is None:
        preserve_source = False

    if preserve_source and explicit_source_price is not None:
        price_usd = explicit_source_price
        # Current quotes carry a precise unit price, so compute their USD value
        # from native amount. Settled historical rows retain their canonical
        # recorded event amount, which can intentionally be currency-rounded.
        estimate = source_estimate if is_settled and source_estimate is not None else native * price_usd
        price_source = text_value(amount.get("usd_estimate_source")) or "generated_auction_feed"
        price_source_detail = text_value(amount.get("usd_estimate_source_detail")) or "precomputed generated auction-feed USD estimate"
        price_date_utc = text_value(amount.get("eth_usd_price_date_utc")) or text_value(amount.get("usd_estimate_price_date_utc")) or event_day
        price_confidence = text_value(amount.get("usd_estimate_confidence")) or "medium"
        notes = text_value(amount.get("usd_estimate_notes")) or "preserved_source_usd_estimate"
        event_amount = text_value(amount.get("amount_usd_at_event"))
        event_price = text_value(amount.get("eth_usd_price_at_event"))
        event_date = text_value(amount.get("eth_usd_price_date_utc"))
        amount["usd_estimate"] = str(estimate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
        amount["usd_estimate_display"] = money_display(estimate)
        amount["usd_estimate_source"] = price_source
        amount["usd_estimate_source_detail"] = price_source_detail
        amount["usd_estimate_confidence"] = price_confidence
        amount["usd_estimate_price_date_utc"] = price_date_utc
        amount["usd_estimate_price_usd"] = str(price_usd) if price_usd is not None else None
        amount["usd_estimate_notes"] = notes
        amount["amount_usd_at_event"] = event_amount or None
        amount["eth_usd_price_at_event"] = event_price or None
        amount["eth_usd_price_date_utc"] = event_date or None
    elif price_row:
        price_usd = decimal_or_none(price_row.get("price_usd"))
        if price_usd is not None:
            estimate = native * price_usd
            amount["usd_estimate"] = str(estimate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
            amount["usd_estimate_display"] = money_display(estimate)
            amount["usd_estimate_source"] = price_row.get("source")
            amount["usd_estimate_source_detail"] = price_row.get("source_detail")
            amount["usd_estimate_confidence"] = price_row.get("confidence") or "high"
            amount["usd_estimate_price_date_utc"] = price_row.get("date_utc")
            amount["usd_estimate_price_usd"] = str(price_usd)
            amount["usd_estimate_notes"] = price_row.get("notes") or ""
            amount["amount_usd_at_event"] = str(estimate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
            amount["eth_usd_price_at_event"] = str(price_usd)
            amount["eth_usd_price_date_utc"] = price_row.get("date_utc")
            price_source = price_row.get("source")
            price_source_detail = price_row.get("source_detail")
            price_date_utc = price_row.get("date_utc")
            price_confidence = amount.get("usd_estimate_confidence")
            notes = amount.get("usd_estimate_notes") or ""
    if estimate is None:
        amount["usd_estimate"] = None
        amount["usd_estimate_display"] = None
        amount["usd_estimate_source"] = None
        amount["usd_estimate_confidence"] = "missing"
        amount["usd_estimate_price_date_utc"] = None
        amount["usd_estimate_price_usd"] = None
        amount["usd_estimate_notes"] = status
        amount["amount_usd_at_event"] = None
        amount["eth_usd_price_at_event"] = None
        amount["eth_usd_price_date_utc"] = None
        price_confidence = "missing"
        notes = status
    record["amount"] = amount
    raw_settlement = record.get("settlement")
    settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
    raw_created = record.get("auction_created")
    auction_created: dict[str, Any] = raw_created if isinstance(raw_created, dict) else {}
    raw_bid_hashes = record.get("bid_tx_hashes")
    bid_hashes = [text_value(value) for value in raw_bid_hashes if text_value(value)] if isinstance(raw_bid_hashes, list) else []
    raw_bid_stats = record.get("bid_stats")
    bid_stats: dict[str, Any] = raw_bid_stats if isinstance(raw_bid_stats, dict) else {}
    status_is_live = text_value(record.get("status")).lower() in {"ongoing", "live"}
    live_zero_bid = is_canonical_live_zero_bid(record, amount, bid_stats, raw_bid_hashes)
    event_type = (
        "settlement"
        if settlement.get("settled")
        else ("current_bid" if status_is_live and not live_zero_bid else "auction_record")
    )
    if event_type == "settlement":
        event_tx_hash = settlement.get("tx_hash")
        event_time_utc = settlement.get("block_time_utc") or record.get("activity_time_utc")
    elif event_type == "current_bid":
        event_tx_hash = bid_hashes[-1] if bid_hashes else None
        event_time_utc = bid_stats.get("last_bid_time_utc") or record.get("activity_time_utc")
    else:
        event_tx_hash = auction_created.get("tx_hash")
        event_time_utc = (
            auction_created.get("block_time_utc")
            if live_zero_bid
            else record.get("activity_time_utc")
        )
    return {
        "mission": record.get("mission"),
        "dog_id": record.get("dog_id"),
        "chain": record.get("chain"),
        "chain_id": record.get("chain_id"),
        "event_type": event_type,
        "event_time_utc": event_time_utc,
        "event_tx_hash": event_tx_hash,
        "native_amount_raw": amount.get("raw"),
        "native_amount": amount.get("native"),
        "native_symbol": amount.get("native_symbol"),
        "price_asset_key": asset,
        "price_usd": str(price_usd) if price_usd is not None else None,
        "estimated_usd_value": str(estimate.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)) if estimate is not None else None,
        "estimated_usd_display": money_display(estimate) if estimate is not None else None,
        "amount_usd_at_event": amount.get("amount_usd_at_event"),
        "eth_usd_price_at_event": amount.get("eth_usd_price_at_event"),
        "eth_usd_price_date_utc": amount.get("eth_usd_price_date_utc"),
        "price_date_utc": price_date_utc,
        "price_source": price_source,
        "price_source_detail": price_source_detail,
        "price_confidence": price_confidence or amount.get("usd_estimate_confidence"),
        "price_status": "priced" if estimate is not None else "missing",
        "notes": notes or amount.get("usd_estimate_notes") or "",
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = [
        "mission", "dog_id", "chain", "chain_id", "event_type", "event_time_utc", "event_tx_hash",
        "native_amount_raw", "native_amount", "native_symbol", "price_asset_key", "price_usd",
        "estimated_usd_value", "estimated_usd_display", "amount_usd_at_event", "eth_usd_price_at_event",
        "eth_usd_price_date_utc", "price_date_utc", "price_source", "price_source_detail",
        "price_confidence", "price_status", "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


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
