#!/usr/bin/env python3
"""Validate historical USD estimate artifacts and unified index enrichment."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
UNIFIED = ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json"
PUBLIC_UNIFIED = ROOT / "public" / "generated" / "unified_dog_search_index.json"
PRICES = ROOT / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
ESTIMATES = ROOT / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates.json"
MANIFEST = ROOT / "archive" / "prices" / "data" / "generated" / "auction_usd_estimates_manifest.json"
LIVE_USD_SOURCES = {"generated_auction_feed", "current_eth_usd_price", "token_stats.eth_usd_price"}
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def decimal_or_none(value: Any) -> Decimal | None:
    text = "" if value is None else str(value).replace(",", "").strip()
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


def is_explicit_integer_zero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return (isinstance(value, int) and value == 0) or (
        isinstance(value, str) and value.strip() == "0"
    )


def is_canonical_live_zero_bid(record: dict[str, Any], amount: dict[str, Any]) -> bool:
    if text_value(record.get("status")).lower() not in {"ongoing", "live"}:
        return False
    if (
        decimal_or_none(amount.get("native")) != Decimal(0)
        or not is_explicit_integer_zero(amount.get("raw"))
    ):
        return False
    raw_bid_stats = record.get("bid_stats")
    bid_stats: dict[str, Any] = raw_bid_stats if isinstance(raw_bid_stats, dict) else {}
    raw_bid_hashes = record.get("bid_tx_hashes")
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
    for amount_field, estimate_field in [
        ("usd_estimate_source", "price_source"),
        ("usd_estimate_source_detail", "price_source_detail"),
        ("usd_estimate_confidence", "price_confidence"),
    ]:
        amount_value = text_value(amount.get(amount_field))
        estimate_value = text_value(row.get(estimate_field))
        if amount_value != estimate_value:
            fail(
                f"historical event USD provenance {amount_field} mismatch for mission {mission} dog {dog_id}: "
                f"unified={amount_value!r} estimate={estimate_value!r}"
            )


def validate_live_event_provenance(
    *,
    mission: Any,
    dog_id: Any,
    record: dict[str, Any],
    amount: dict[str, Any],
    row: dict[str, Any],
    current_by_dog: dict[int, dict[str, Any]],
) -> None:
    event_type = text_value(row.get("event_type"))
    status = text_value(record.get("status")).lower()
    is_active = status in {"ongoing", "live"}
    if not is_active:
        return
    if is_active and is_canonical_live_zero_bid(record, amount):
        raw_created = record.get("auction_created")
        created: dict[str, Any] = raw_created if isinstance(raw_created, dict) else {}
        created_hash = text_value(created.get("tx_hash"))
        created_time = text_value(created.get("block_time_utc"))
        if (
            event_type != "auction_record"
            or not created_hash
            or not created_time
            or text_value(row.get("event_tx_hash")) != created_hash
            or text_value(row.get("event_time_utc")) != created_time
        ):
            fail(f"zero-bid auction creation provenance mismatch for mission {mission} dog {dog_id}")
        current = current_by_dog.get(int(dog_id))
        if current is not None:
            current_native = decimal_or_none(current.get("current_bid_eth"))
            current_wallet = text_value(current.get("bidder_wallet")).lower()
            if current_native != Decimal(0) or current_wallet not in {"", ZERO_ADDRESS}:
                fail(f"zero-bid current auction surface mismatch for Dog #{dog_id}")
        return

    if event_type != "current_bid":
        fail(f"active bid event classification mismatch for mission {mission} dog {dog_id}")
    bid_hashes = [text_value(value) for value in record.get("bid_tx_hashes", []) if text_value(value)]
    if not bid_hashes or text_value(row.get("event_tx_hash")) != bid_hashes[-1]:
        fail(f"current bid transaction provenance mismatch for mission {mission} dog {dog_id}")
    bid_stats = record.get("bid_stats") if isinstance(record.get("bid_stats"), dict) else {}
    last_bid_time = text_value(bid_stats.get("last_bid_time_utc"))
    if not last_bid_time or text_value(row.get("event_time_utc")) != last_bid_time:
        fail(f"current bid time provenance mismatch for mission {mission} dog {dog_id}")
    if text_value(record.get("activity_time_utc")) != last_bid_time:
        fail(f"current bid activity time mismatch for mission {mission} dog {dog_id}")


def validate_current_bid_provenance(
    *,
    mission: Any,
    dog_id: Any,
    record: dict[str, Any],
    amount: dict[str, Any],
    row: dict[str, Any],
    current_by_dog: dict[int, dict[str, Any]],
) -> None:
    validate_live_event_provenance(
        mission=mission,
        dog_id=dog_id,
        record=record,
        amount=amount,
        row=row,
        current_by_dog=current_by_dog,
    )
    event_type = text_value(row.get("event_type"))
    status = text_value(record.get("status")).lower()
    is_active = status in {"ongoing", "live"}
    if is_active and is_canonical_live_zero_bid(record, amount):
        explicit_price = decimal_or_none(amount.get("usd_estimate_price_usd"))
        row_price = decimal_or_none(row.get("price_usd"))
        row_value = decimal_or_none(row.get("estimated_usd_value"))
        amount_value = decimal_or_none(amount.get("usd_estimate"))
        if (
            explicit_price is None
            or row_price is None
            or row_value is None
            or amount_value is None
            or row_price != explicit_price
            or row_value != Decimal(0)
            or amount_value != Decimal(0)
        ):
            fail(f"zero-bid exact live-price provenance mismatch for mission {mission} dog {dog_id}")

        current = current_by_dog.get(int(dog_id))
        if current is not None:
            current_price = decimal_or_none(current.get("eth_usd_price_live"))
            if current_price is None or current_price != explicit_price:
                fail(f"current auction ETH/USD quote differs from archive for Dog #{dog_id}")
            if text_value(current.get("eth_usd_price_date_utc")) != text_value(amount.get("usd_estimate_price_date_utc")):
                fail(f"current auction ETH/USD quote date differs from archive for Dog #{dog_id}")
        return

    if event_type != "current_bid":
        return
    bid_hashes = [text_value(value) for value in record.get("bid_tx_hashes", []) if text_value(value)]
    if not bid_hashes or text_value(row.get("event_tx_hash")) != bid_hashes[-1]:
        fail(f"current bid transaction provenance mismatch for mission {mission} dog {dog_id}")
    bid_stats = record.get("bid_stats") if isinstance(record.get("bid_stats"), dict) else {}
    last_bid_time = text_value(bid_stats.get("last_bid_time_utc"))
    if not last_bid_time or text_value(row.get("event_time_utc")) != last_bid_time:
        fail(f"current bid time provenance mismatch for mission {mission} dog {dog_id}")
    if text_value(record.get("activity_time_utc")) != last_bid_time:
        fail(f"current bid activity time mismatch for mission {mission} dog {dog_id}")

    native = decimal_or_none(amount.get("native"))
    explicit_price = decimal_or_none(amount.get("usd_estimate_price_usd"))
    row_price = decimal_or_none(row.get("price_usd"))
    row_value = decimal_or_none(row.get("estimated_usd_value"))
    amount_value = decimal_or_none(amount.get("usd_estimate"))
    if native is None or explicit_price is None or row_price is None or row_value is None or amount_value is None:
        fail(f"current bid exact live-price provenance missing for mission {mission} dog {dog_id}")
    if row_price != explicit_price:
        fail(f"current bid ETH/USD quote mismatch for mission {mission} dog {dog_id}")
    expected_value = (native * explicit_price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    if row_value != expected_value or amount_value != expected_value:
        fail(f"current bid USD value is not derived from its exact quote for mission {mission} dog {dog_id}")

    current = current_by_dog.get(int(dog_id))
    if current is not None:
        surface_price = decimal_or_none(current.get("eth_usd_price_live"))
        if surface_price is None or surface_price != explicit_price:
            fail(f"current auction ETH/USD quote differs from archive for Dog #{dog_id}")
        if text_value(current.get("eth_usd_price_date_utc")) != text_value(amount.get("usd_estimate_price_date_utc")):
            fail(f"current auction ETH/USD quote date differs from archive for Dog #{dog_id}")


def surface_dog_id(row: dict[str, Any]) -> int:
    raw = row.get("token_id")
    if raw in (None, ""):
        raw = row.get("dog_id")
    if raw not in (None, ""):
        return int(raw)
    label = text_value(row.get("dog") or row.get("dog_name"))
    digits = "".join(char if char.isdigit() else " " for char in label).split()
    return int(digits[-1]) if digits else -1


def validate_surface_provenance(
    *,
    dog_id: int,
    canonical: dict[str, Any],
    surface: dict[str, Any],
    label: str,
    event_amount_field: str,
) -> None:
    event_amount = decimal_or_none(surface.get(event_amount_field))
    canonical_amount = decimal_or_none(canonical.get("amount_usd_at_event"))
    if event_amount is None or canonical_amount is None:
        fail(f"{label} historical event USD amount missing for Dog #{dog_id}")
    if event_amount.quantize(Decimal("0.01")) != canonical_amount.quantize(Decimal("0.01")):
        fail(f"{label} historical event USD amount differs from archive for Dog #{dog_id}")
    surface_price = decimal_or_none(surface.get("eth_usd_price_at_event"))
    canonical_price = decimal_or_none(canonical.get("eth_usd_price_at_event"))
    if surface_price is None or canonical_price is None or surface_price != canonical_price:
        fail(f"{label} historical ETH/USD price differs from archive for Dog #{dog_id}")
    for field in [
        "eth_usd_price_date_utc",
        "usd_estimate_source",
        "usd_estimate_source_detail",
        "usd_estimate_confidence",
    ]:
        actual = text_value(surface.get(field))
        expected = text_value(canonical.get(field))
        if actual != expected:
            fail(f"{label} {field} differs from archive for Dog #{dog_id}: {actual!r} != {expected!r}")
    if text_value(surface.get("usd_estimate_source")).lower() in LIVE_USD_SOURCES:
        fail(f"{label} uses live/current historical provenance for Dog #{dog_id}")


def validate_archive_surface_parity(
    *,
    record: dict[str, Any],
    amount: dict[str, Any],
    feed_by_dog: dict[int, dict[str, Any]],
    winner_by_dog: dict[int, dict[str, Any]],
) -> None:
    if record.get("mission") != 3 or is_live_or_current(record):
        return
    dog_id = int(record.get("dog_id"))
    dog_path = ROOT / "archive" / "dogs" / "by-id" / f"{dog_id:03d}.json"
    if dog_path.exists():
        dog_payload = load_json(dog_path)
        dog_record = dog_payload.get("record") if isinstance(dog_payload, dict) else None
        dog_amount = dog_record.get("amount") if isinstance(dog_record, dict) and isinstance(dog_record.get("amount"), dict) else None
        if not isinstance(dog_amount, dict):
            fail(f"archive/dogs/by-id/{dog_id:03d}.json lacks amount provenance")
        for field in [
            "amount_usd_at_event",
            "eth_usd_price_at_event",
            "eth_usd_price_date_utc",
            "usd_estimate_source",
            "usd_estimate_source_detail",
            "usd_estimate_confidence",
        ]:
            if text_value(dog_amount.get(field)) != text_value(amount.get(field)):
                fail(f"by-id {field} differs from unified archive for Dog #{dog_id}")

    feed = feed_by_dog.get(dog_id)
    if feed is not None and text_value(feed.get("status")).lower() == "settled":
        validate_surface_provenance(
            dog_id=dog_id,
            canonical=amount,
            surface=feed,
            label="auction_feed",
            event_amount_field="amount_usd_at_event",
        )

    winner = winner_by_dog.get(dog_id)
    if winner is not None:
        validate_surface_provenance(
            dog_id=dog_id,
            canonical=amount,
            surface=winner,
            label="auction_winners",
            event_amount_field="winning_bid_usd_at_settlement",
        )
        winner_tx = text_value(winner.get("tx_hash"))
        winner_block = text_value(winner.get("block_number"))
        if winner_tx and not winner_block:
            fail(f"auction_winners settlement block missing for Dog #{dog_id}")
        raw_settlement = record.get("settlement")
        settlement = raw_settlement if isinstance(raw_settlement, dict) else {}
        archive_block = text_value(settlement.get("block_number"))
        if archive_block and winner_block and archive_block != winner_block:
            fail(f"auction_winners settlement block differs from unified archive for Dog #{dog_id}")


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
    feed_rows = load_json(ROOT / "generated" / "auction_feed.json") if (ROOT / "generated" / "auction_feed.json").exists() else []
    winner_rows = load_json(ROOT / "generated" / "auction_winners.json") if (ROOT / "generated" / "auction_winners.json").exists() else []
    current_rows = load_json(ROOT / "generated" / "current_auction.json") if (ROOT / "generated" / "current_auction.json").exists() else []
    feed_by_dog = {surface_dog_id(row): row for row in feed_rows if isinstance(row, dict)} if isinstance(feed_rows, list) else {}
    winner_by_dog = {surface_dog_id(row): row for row in winner_rows if isinstance(row, dict)} if isinstance(winner_rows, list) else {}
    current_by_dog = {surface_dog_id(row): row for row in current_rows if isinstance(row, dict)} if isinstance(current_rows, list) else {}
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
        validate_live_event_provenance(
            mission=mission,
            dog_id=dog_id,
            record=record,
            amount=amount,
            row=row,
            current_by_dog=current_by_dog,
        )
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
            validate_current_bid_provenance(
                mission=mission,
                dog_id=dog_id,
                record=record,
                amount=amount,
                row=row,
                current_by_dog=current_by_dog,
            )
            validate_archive_surface_parity(
                record=record,
                amount=amount,
                feed_by_dog=feed_by_dog,
                winner_by_dog=winner_by_dog,
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
