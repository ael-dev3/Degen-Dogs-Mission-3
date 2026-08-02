#!/usr/bin/env python3
"""Refresh the dashboard's current auction surface without a full history scan.

The full builder is intentionally comprehensive, but a fresh checkout has no local
RPC log cache and can spend many minutes scanning historical Base blocks. This
fast path uses the committed dashboard tables as its baseline, re-fetches only a
small overlap before the last generated block, and updates the current auction,
feed, latest-bid, search, metrics, and rendered HTML files.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"
HISTORICAL_PRICES = ROOT / "archive" / "prices" / "data" / "generated" / "historical_prices_daily.json"
MISSION3_SOURCE_INDEX = ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_dog_search_index.json"
LIVE_USD_SOURCES = {
    "current_eth_usd_price",
    "generated_auction_feed",
    "generated_current_auction",
    "generated_current_latest_bid",
    "generated_recent_bids",
    "token_stats.eth_usd_price",
}


class FullRefreshRequired(RuntimeError):
    """Raised before writes when committed artifacts cannot support an exact delta."""


def bid_tx_hash(row: dict[str, Any]) -> str:
    return str(row.get("tx_hash") or row.get("transaction_hash") or "").strip().lower()


def bid_wallet(row: dict[str, Any]) -> str:
    return str(row.get("bidder_wallet") or row.get("bidder") or "").strip().lower()


def bid_amount(row: dict[str, Any]) -> Decimal:
    return Decimal(str(row.get("bid_eth") or row.get("amount_eth") or 0))


def bid_block(row: dict[str, Any]) -> int:
    return int_value(row.get("block_number"))


def bid_log_index(row: dict[str, Any]) -> int:
    return int_value(row.get("log_index"), -1)


def bid_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Stable event identity that still works for the legacy recent-bids schema."""
    tx_hash = bid_tx_hash(row)
    log_index = bid_log_index(row)
    if tx_hash and log_index >= 0:
        return ("tx_log", tx_hash, log_index)
    if tx_hash:
        # AuctionBid transactions contain one auction-house bid event in the
        # current contract. Legacy recent_bids rows omitted log_index.
        return ("tx", tx_hash)
    return (
        "fallback",
        bid_block(row),
        int_value(row.get("token_id"), -1),
        bid_wallet(row),
        format_eth_amount(bid_amount(row)),
        str(row.get("bid_time_utc") or row.get("block_time_utc") or ""),
    )


def bid_public_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identity shared by full history rows and legacy recent_bids rows."""
    tx_hash = bid_tx_hash(row)
    if tx_hash:
        return ("tx", tx_hash)
    return bid_identity(row)


def bid_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (bid_block(row), bid_log_index(row), bid_tx_hash(row))


def merge_overlap_bid_history(
    existing_rows: list[dict[str, Any]],
    fresh_rows: list[dict[str, Any]],
    *,
    from_block: int,
    token_ids: set[int],
) -> dict[int, list[dict[str, Any]]]:
    """Replace, rather than union, the canonical overlap for active auctions."""
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for source, is_fresh in ((existing_rows, False), (fresh_rows, True)):
        for original in source:
            row = dict(original)
            token_id = int_value(row.get("token_id"), -1)
            if token_id not in token_ids:
                continue
            if not is_fresh and bid_block(row) >= from_block:
                # Every cached row in the overlap is discarded first. This is
                # what removes orphaned logs after a short Base reorg.
                continue
            merged[bid_identity(row)] = row
    by_token: dict[int, list[dict[str, Any]]] = {token_id: [] for token_id in token_ids}
    for row in merged.values():
        by_token[int_value(row.get("token_id"), -1)].append(row)
    for rows in by_token.values():
        rows.sort(key=bid_sort_key)
    return by_token


def format_fresh_bid_rows(
    rows: list[dict[str, Any]],
    *,
    eth_usd: Decimal,
    eth_source: str,
    price_date_utc: str,
    profiles: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for bid in rows:
        wallet = bid_wallet(bid)
        name, url = display_for(wallet, profiles)
        amount = bid_amount(bid)
        amount_usd = (amount * eth_usd).quantize(Decimal("0.01"))
        token_id = int_value(bid.get("token_id"), -1)
        formatted.append(
            {
                "bid_time_utc": bid.get("block_time_utc") or bid.get("bid_time_utc") or "",
                "token_id": token_id,
                "dog": f"Dog #{token_id}",
                "bidder": name,
                "bidder_url": url,
                "bidder_wallet": wallet,
                "bid": f"{format_eth_amount(amount)} ETH (${amount_usd:,.0f})",
                "bid_eth": float(amount),
                "bid_usd": float(amount_usd),
                "bid_usd_at_event": float(amount_usd),
                "eth_usd_price_live": str(eth_usd),
                "eth_usd_price_at_event": str(eth_usd),
                "eth_usd_price_date_utc": price_date_utc,
                "usd_estimate_source": "current_eth_usd_price",
                "usd_estimate_source_detail": eth_source,
                "usd_estimate_confidence": "live_current",
                "usd_estimate_basis": "current_eth_usd_price",
                "extended": int_value(bid.get("extended")),
                "block_number": bid_block(bid),
                "log_index": bid_log_index(bid),
                "tx_hash": bid_tx_hash(bid),
            }
        )
    return formatted


def reconcile_recent_bid_rows(
    existing_rows: list[dict[str, Any]],
    fresh_rows: list[dict[str, Any]],
    complete_active_history: list[dict[str, Any]],
    *,
    from_block: int,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return recent rows plus exact event additions/removals for aggregate marts."""
    if existing_rows:
        oldest = min(bid_block(row) for row in existing_rows)
        if len(existing_rows) >= limit and oldest > from_block:
            raise FullRefreshRequired(
                f"recent_bids does not cover overlap start {from_block}; oldest retained block is {oldest}"
            )

    old_overlap = {
        bid_public_identity(row): dict(row)
        for row in existing_rows
        if bid_block(row) >= from_block
    }
    fresh_overlap = {
        bid_public_identity(row): dict(row)
        for row in fresh_rows
        if bid_block(row) >= from_block
    }
    removed = [row for key, row in old_overlap.items() if key not in fresh_overlap]
    added = [row for key, row in fresh_overlap.items() if key not in old_overlap]

    newest_existing = max((bid_block(row) for row in existing_rows), default=0)
    known = {
        bid_public_identity(row)
        for row in [*existing_rows, *fresh_rows]
    }
    for row in complete_active_history:
        key = bid_public_identity(row)
        # This catches data written by the old fast path: current bid history
        # advanced, but recent_bids and its aggregate marts did not.
        if key not in known and bid_block(row) > newest_existing:
            added.append(dict(row))
            known.add(key)

    candidates: dict[tuple[Any, ...], dict[str, Any]] = {
        bid_public_identity(row): dict(row)
        for row in existing_rows
        if bid_block(row) < from_block
    }
    for row in [*fresh_rows, *complete_active_history]:
        candidates[bid_public_identity(row)] = dict(row)
    all_rows = sorted(candidates.values(), key=bid_sort_key, reverse=True)
    return all_rows[:limit], added, removed, all_rows


def ensure_no_untracked_bid_gap(
    timeline_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
    recoverable_rows: list[dict[str, Any]],
) -> None:
    """Reject baselines where an earlier partial refresh skipped derived marts."""
    recent_times = [parse_utc(row.get("bid_time_utc")) for row in recent_rows]
    newest_recent = max((value for value in recent_times if value is not None), default=None)
    recoverable_times = {
        parse_utc(row.get("bid_time_utc") or row.get("block_time_utc"))
        for row in recoverable_rows
    }
    recoverable_times.discard(None)
    recoverable_counts = Counter(int_value(row.get("token_id"), -1) for row in recoverable_rows)
    if newest_recent is None:
        if any(int_value(row.get("bids")) > 0 for row in timeline_rows):
            raise FullRefreshRequired("recent_bids is empty while auction_timeline contains bids")
        return
    gaps: list[int] = []
    for row in timeline_rows:
        token_id = int_value(row.get("token_id"), -1)
        latest = parse_utc(row.get("latest_bid_utc"))
        bid_count = int_value(row.get("bids"))
        if bid_count > 0 and latest is None and recoverable_counts[token_id] < bid_count:
            gaps.append(token_id)
        elif bid_count > 0 and latest and latest > newest_recent and latest not in recoverable_times:
            gaps.append(token_id)
    if gaps:
        tokens = ",".join(str(token) for token in sorted(set(gaps)))
        raise FullRefreshRequired(
            f"derived bid ledger has an unrecoverable gap after {newest_recent.isoformat()}; tokens={tokens}"
        )


def apply_bidder_leaderboard_delta(
    rows: list[dict[str, Any]],
    added: list[dict[str, Any]],
    removed: list[dict[str, Any]],
    all_known_rows: list[dict[str, Any]],
    profiles: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Apply exact bid deltas to the all-time leaderboard or fail closed."""
    by_wallet = {bid_wallet(row): dict(row) for row in rows if bid_wallet(row)}
    affected = {bid_wallet(row) for row in [*added, *removed] if bid_wallet(row)}
    missing = sorted(wallet for wallet in affected if wallet not in by_wallet)
    if missing:
        raise FullRefreshRequired(
            "leaderboard lacks all-time baseline for affected bidder(s): " + ",".join(missing)
        )

    known_by_wallet: dict[str, list[dict[str, Any]]] = {}
    for row in all_known_rows:
        known_by_wallet.setdefault(bid_wallet(row), []).append(row)

    for wallet in affected:
        target = by_wallet[wallet]
        wallet_added = [row for row in added if bid_wallet(row) == wallet]
        wallet_removed = [row for row in removed if bid_wallet(row) == wallet]
        bid_delta = len(wallet_added) - len(wallet_removed)
        eth_delta = sum((bid_amount(row) for row in wallet_added), Decimal(0)) - sum(
            (bid_amount(row) for row in wallet_removed), Decimal(0)
        )
        if bid_delta < 0 or eth_delta < 0:
            # A decrease can move a top-100 bidder below an unpersisted rank-101
            # bidder, so the exact leaderboard boundary cannot be proven here.
            raise FullRefreshRequired(f"leaderboard rank may change after removed bid(s) for {wallet}")
        target["bids"] = int_value(target.get("bids")) + bid_delta
        target["bid_eth"] = format_eth_amount(Decimal(str(target.get("bid_eth") or 0)) + eth_delta)

        pair_delta = 0
        affected_tokens = {
            int_value(row.get("token_id"), -1)
            for row in [*wallet_added, *wallet_removed]
        }
        for affected_token in affected_tokens:
            final_count = sum(
                1
                for row in known_by_wallet.get(wallet, [])
                if int_value(row.get("token_id"), -1) == affected_token
            )
            added_count = sum(
                1 for row in wallet_added if int_value(row.get("token_id"), -1) == affected_token
            )
            removed_count = sum(
                1 for row in wallet_removed if int_value(row.get("token_id"), -1) == affected_token
            )
            prior_count = final_count - added_count + removed_count
            if prior_count == 0 and final_count > 0:
                pair_delta += 1
            elif prior_count > 0 and final_count == 0:
                raise FullRefreshRequired(f"cannot prove auctions_bid decrement for {wallet} token {affected_token}")
        target["auctions_bid"] = int_value(target.get("auctions_bid")) + pair_delta

        prior_high = Decimal(str(target.get("high_bid_eth") or 0))
        removed_high = max((bid_amount(row) for row in wallet_removed), default=Decimal(0))
        fresh_high = max((bid_amount(row) for row in wallet_added), default=Decimal(0))
        if removed_high >= prior_high and fresh_high < prior_high:
            raise FullRefreshRequired(f"cannot reconstruct historical high bid for {wallet}")
        target["high_bid_eth"] = format_eth_amount(max(prior_high, fresh_high))

        known = sorted(known_by_wallet.get(wallet, []), key=bid_sort_key, reverse=True)
        if known:
            latest = known[0]
            target["latest_bid_token_id"] = int_value(latest.get("token_id"), -1)
            target["latest_bid_utc"] = latest.get("bid_time_utc") or latest.get("block_time_utc") or ""
        label, url = display_for(wallet, profiles)
        target["bidder"] = label
        target["bidder_url"] = url

    output = list(by_wallet.values())
    output.sort(
        key=lambda row: (
            Decimal(str(row.get("bid_eth") or 0)),
            int_value(row.get("bids")),
            bid_wallet(row),
        ),
        reverse=True,
    )
    return output[:100]


def recompute_daily_activity(
    rows: list[dict[str, Any]],
    timeline_rows: list[dict[str, Any]],
    known_bid_rows: list[dict[str, Any]],
    affected_days: set[str],
) -> list[dict[str, Any]]:
    """Rebuild affected UTC days from a proven-complete recent event window."""
    known_times = [
        parse_utc(row.get("bid_time_utc") or row.get("block_time_utc"))
        for row in known_bid_rows
    ]
    known_times = [value for value in known_times if value is not None]
    if affected_days and not known_times:
        raise FullRefreshRequired("cannot rebuild daily bid activity without event timestamps")
    earliest_known_day = min(known_times).date().isoformat() if known_times else "9999-12-31"
    unsafe = sorted(day for day in affected_days if day <= earliest_known_day)
    if unsafe:
        raise FullRefreshRequired(
            "recent bid ledger does not cover complete affected UTC day(s): " + ",".join(unsafe)
        )

    by_day = {str(row.get("activity_day")): dict(row) for row in rows if row.get("activity_day")}
    for day in affected_days:
        day_bids = [
            row
            for row in known_bid_rows
            if (parse_utc(row.get("bid_time_utc") or row.get("block_time_utc")) or datetime.min.replace(tzinfo=timezone.utc)).date().isoformat() == day
        ]
        created_count = sum(
            1 for row in timeline_rows if (parse_utc(row.get("start_time_utc")) or datetime.min.replace(tzinfo=timezone.utc)).date().isoformat() == day
        )
        settled_on_day = [
            row for row in timeline_rows if (parse_utc(row.get("settled_time_utc")) or datetime.min.replace(tzinfo=timezone.utc)).date().isoformat() == day
        ]
        by_day[day] = {
            "activity_day": day,
            "created_auctions": created_count,
            "settled_auctions": len(settled_on_day),
            "bids": len(day_bids),
            "unique_bidders": len({bid_wallet(row) for row in day_bids if bid_wallet(row)}),
            "bid_eth": format_eth_amount(sum((bid_amount(row) for row in day_bids), Decimal(0))),
            "high_bid_eth": format_eth_amount(max((bid_amount(row) for row in day_bids), default=Decimal(0))),
            "settled_eth": format_eth_amount(
                sum((Decimal(str(row.get("settled_eth") or 0)) for row in settled_on_day), Decimal(0))
            ),
        }
    return sorted(by_day.values(), key=lambda row: str(row.get("activity_day") or ""), reverse=True)


def apply_winner_stats_to_leaderboard(
    leaderboard_rows: list[dict[str, Any]],
    winner_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stats: dict[str, tuple[int, Decimal]] = {}
    for row in winner_rows:
        wallet = str(row.get("winner_wallet") or "").lower()
        if not wallet:
            continue
        wins, amount = stats.get(wallet, (0, Decimal(0)))
        stats[wallet] = (wins + 1, amount + Decimal(str(row.get("winning_bid_eth") or 0)))
    output: list[dict[str, Any]] = []
    for original in leaderboard_rows:
        row = dict(original)
        wins, amount = stats.get(bid_wallet(row), (0, Decimal(0)))
        row["auction_wins"] = wins
        row["winning_eth"] = format_eth_amount(amount)
        output.append(row)
    return output


def read_json(name: str) -> Any:
    return json.loads((GENERATED / f"{name}.json").read_text(encoding="utf-8"))


def load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def ensure_owned_directory_tree(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while True:
        try:
            details = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            if cursor.parent == cursor:
                raise RuntimeError(f"unable to find a trusted ancestor for output directory: {directory}")
            cursor = cursor.parent
            continue
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise RuntimeError(f"refusing unsafe output directory ancestor: {cursor}")
        if details.st_uid != os.getuid() or cursor.parent == cursor:
            break
        cursor = cursor.parent
    for item in reversed(missing):
        try:
            item.mkdir(mode=0o700)
        except FileExistsError:
            pass
        details = item.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError(f"refusing unsafe output directory ancestor: {item}")


def atomic_write_text(path: Path, payload: str) -> None:
    ensure_owned_directory_tree(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_unified_by_id_records(records: list[dict[str, Any]], affected_token_ids: set[int]) -> None:
    """Atomically mirror changed unified records into the per-Dog archive."""
    by_id = ROOT / "archive" / "dogs" / "by-id"
    now = now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    by_token = {
        int_value(record.get("dog_id"), -1): record
        for record in records
        if isinstance(record, dict)
        and int_value(record.get("dog_id"), -1) >= 0
    }
    for token_id in sorted(affected_token_ids):
        record = by_token.get(token_id)
        if record is None:
            raise FullRefreshRequired(f"unified archive lost affected Dog #{token_id}")
        path = by_id / f"{token_id:03d}.json"
        existing = load_json_file(path, {})
        generated_at = now
        if isinstance(existing, dict):
            generated_at = str(existing.get("generated_at_utc") or now)
        payload = {
            "schema_version": 1,
            "generated_at_utc": generated_at,
            "record": record,
        }
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    manifest = {
        "schema_version": 1,
        "updated_at_utc": now,
        "record_count": len(records),
        "paths": [
            f"archive/dogs/by-id/{int_value(record.get('dog_id')):03d}.json"
            for record in records
            if isinstance(record, dict) and int_value(record.get("dog_id"), -1) >= 0
        ],
    }
    atomic_write_text(
        ROOT / "archive" / "dogs" / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def write_json(name: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    for directory in (GENERATED, PUBLIC_GENERATED):
        atomic_write_text(directory / f"{name}.json", payload)


def read_table(name: str) -> tuple[list[str], list[dict[str, Any]]]:
    path = GENERATED / f"{name}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    return list(reader.fieldnames or []), rows


def write_table(name: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    payload = buffer.getvalue()
    for directory in (GENERATED, PUBLIC_GENERATED):
        atomic_write_text(directory / f"{name}.csv", payload)
    write_json(name, rows)


def int_value(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def format_eth_amount(value: Any) -> str:
    amount = Decimal(str(value or 0))
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def historical_settlement_amount_display(
    token_id: int,
    amount_eth: Decimal,
    historical_usd: dict[str, Any] | None,
) -> str:
    if historical_usd is None:
        raise FullRefreshRequired(f"Dog #{token_id} settlement lost historical USD provenance")
    amount_usd = decimal_or_none(historical_usd.get("amount_usd_at_event"))
    if amount_usd is None or amount_usd < 0:
        raise FullRefreshRequired(f"Dog #{token_id} settlement has invalid historical USD provenance")
    return f"{format_eth_amount(amount_eth)} ETH (${amount_usd:,.0f})"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip().replace(" ", "T")
    if not raw:
        return None
    if not raw.endswith("Z"):
        raw += "Z"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_current_auction_creation(
    token_id: int,
    start_time_utc: Any,
    fresh_event: dict[str, Any],
    timeline_row: dict[str, Any],
    unified_row: dict[str, Any],
    mission3_source_row: dict[str, Any],
    winner_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a creation record corroborated by canonical onchain-derived tables.

    A same-auction fast refresh will normally start long after AuctionCreated, so
    its bounded log window cannot see that event. In that case the creation tx
    retained in the timeline is mapped back to the preceding AuctionSettled
    winner row. Fresh events and the historical Mission 3 source are equally
    authoritative. The unified baseline may corroborate them, but it is never
    accepted as the only block source.
    """
    existing_created = unified_row.get("auction_created")
    if not isinstance(existing_created, dict):
        existing_created = {}

    candidates = [
        ("fresh AuctionCreated", fresh_event.get("tx_hash")),
        ("auction timeline", timeline_row.get("created_tx_hash")),
        ("Mission 3 source", mission3_source_row.get("auction_created_tx")),
        ("unified baseline", existing_created.get("tx_hash")),
    ]
    tx_values: dict[str, str] = {}
    for label, value in candidates:
        tx_hash = str(value or "").strip().lower()
        if not tx_hash:
            continue
        if not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash):
            raise FullRefreshRequired(f"Dog #{token_id} has an invalid {label} transaction hash")
        tx_values[label] = tx_hash
    unique_txs = set(tx_values.values())
    if not unique_txs:
        raise FullRefreshRequired(f"Dog #{token_id} has no canonical AuctionCreated transaction")
    if len(unique_txs) != 1:
        detail = ", ".join(f"{label}={value}" for label, value in sorted(tx_values.items()))
        raise FullRefreshRequired(f"Dog #{token_id} AuctionCreated transaction sources disagree: {detail}")
    tx_hash = unique_txs.pop()

    authoritative_blocks: dict[str, int] = {}
    comparison_blocks: dict[str, int] = {}

    def add_block(target: dict[str, int], label: str, value: Any) -> None:
        block = int_value(value)
        if block > 0:
            target[label] = block

    if str(fresh_event.get("tx_hash") or "").strip().lower() == tx_hash:
        add_block(authoritative_blocks, "fresh AuctionCreated", fresh_event.get("block_number"))
    if str(mission3_source_row.get("auction_created_tx") or "").strip().lower() == tx_hash:
        add_block(authoritative_blocks, "Mission 3 source", mission3_source_row.get("auction_created_block"))
    for winner in winner_rows:
        if str(winner.get("tx_hash") or "").strip().lower() == tx_hash:
            add_block(authoritative_blocks, "preceding onchain settlement", winner.get("block_number"))
    if str(existing_created.get("tx_hash") or "").strip().lower() == tx_hash:
        add_block(comparison_blocks, "unified baseline", existing_created.get("block_number"))

    verified_blocks = set(authoritative_blocks.values())
    if not verified_blocks:
        raise FullRefreshRequired(
            f"Dog #{token_id} AuctionCreated transaction {tx_hash} has no verified onchain block mapping"
        )
    all_blocks = verified_blocks | set(comparison_blocks.values())
    if len(all_blocks) != 1:
        sources = {**authoritative_blocks, **comparison_blocks}
        detail = ", ".join(f"{label}={value}" for label, value in sorted(sources.items()))
        raise FullRefreshRequired(f"Dog #{token_id} AuctionCreated block sources disagree: {detail}")
    block_number = verified_blocks.pop()

    expected_time = parse_utc(start_time_utc)
    if expected_time is None:
        raise FullRefreshRequired(f"Dog #{token_id} has no valid pinned auction start time")
    time_candidates = {
        "fresh AuctionCreated": fresh_event.get("block_time_utc"),
        "auction timeline": timeline_row.get("start_time_utc"),
        "Mission 3 source": mission3_source_row.get("auction_created_time_utc"),
        "unified baseline": existing_created.get("block_time_utc"),
    }
    for label, value in time_candidates.items():
        if value in (None, ""):
            continue
        candidate_time = parse_utc(value)
        if candidate_time is None or candidate_time != expected_time:
            raise FullRefreshRequired(
                f"Dog #{token_id} AuctionCreated time disagrees with pinned contract state ({label})"
            )
    canonical_time = expected_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "block_number": block_number,
        "block_time_utc": canonical_time,
        "tx_hash": tx_hash,
        "tx_url": f"https://basescan.org/tx/{tx_hash}",
    }


def unified_trait_items(trait_rarity: str) -> list[dict[str, str]]:
    """Convert the generated trait-rarity string into unified-index objects."""
    items: list[dict[str, str]] = []
    for item in str(trait_rarity or "").split("; "):
        match = re.match(r"^([^:]+): (.+?) (\([^)]+%\))$", item)
        if match:
            items.append({"display": item, "trait_type": match.group(1), "value": match.group(2)})
    return items


def format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def short_wallet(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if wallet else ""


def profile_map() -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for path in GENERATED.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        values = data if isinstance(data, list) else [data]
        for row in values:
            if not isinstance(row, dict):
                continue
            for wallet_key, name_key, url_key in (
                ("bidder_wallet", "bidder", "bidder_url"),
                ("winner_wallet", "winner", "winner_url"),
                ("winner_wallet", "winner_display", "winner_url"),
            ):
                wallet = str(row.get(wallet_key) or "").lower()
                name = str(row.get(name_key) or "")
                if wallet and name and wallet not in found:
                    found[wallet] = (name, str(row.get(url_key) or ""))
    return found


def traits_from_text(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for item in str(text or "").split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key.strip() and value.strip():
            attrs[key.strip()] = value.strip()
    return attrs


def build_rarity(
    history: list[dict[str, Any]],
    token_id: int,
    new_attrs: dict[str, str],
    total_supply: int,
) -> tuple[str, float, str, str]:
    universe = build_rarity_universe(history, token_id, new_attrs, total_supply)
    current = universe[token_id]
    return (
        str(current["rarity"]),
        float(current["rarity_score"]),
        str(current["traits"]),
        str(current["trait_rarity"]),
    )


def build_rarity_universe(
    history: list[dict[str, Any]],
    token_id: int,
    new_attrs: dict[str, str],
    total_supply: int,
) -> dict[int, dict[str, Any]]:
    """Recompute the exact all-token rarity permutation after a mint."""
    all_attrs: dict[int, dict[str, str]] = {}
    for row in history:
        historical_token_id = int_value(row.get("token_id"), -1)
        if historical_token_id >= 0:
            all_attrs[historical_token_id] = traits_from_text(str(row.get("traits") or ""))
    # The current Dog may already be present after a full rebuild. Replacing its
    # attributes avoids scoring a phantom `max_id + 1` Dog and duplicating a rank.
    all_attrs[token_id] = new_attrs
    if len(all_attrs) != total_supply:
        raise FullRefreshRequired(
            f"rarity universe has {len(all_attrs)} Dogs at total supply {total_supply}; full refresh required"
        )
    counts: Counter[tuple[str, str]] = Counter()
    for attrs in all_attrs.values():
        counts.update(attrs.items())
    scores: dict[int, float] = {}
    for candidate_id, attrs in all_attrs.items():
        scores[candidate_id] = sum(total_supply / max(1, counts[(key, value)]) for key, value in attrs.items())
    ranks = {
        candidate_id: rank
        for rank, candidate_id in enumerate(
            sorted(scores, key=lambda candidate: (-scores[candidate], candidate)),
            start=1,
        )
    }
    universe: dict[int, dict[str, Any]] = {}
    for candidate_id, attrs in all_attrs.items():
        traits = "; ".join(f"{key}: {value}" for key, value in attrs.items())
        trait_rarity = "; ".join(
            f"{key}: {value} ({counts[(key, value)] * 100 / total_supply:.1f}%)"
            for key, value in attrs.items()
        )
        universe[candidate_id] = {
            "rarity": f"#{ranks[candidate_id]}/{total_supply}",
            "rarity_score": round(scores[candidate_id], 6),
            "trait_rarity": trait_rarity,
            "traits": traits,
        }
    return universe


def apply_rarity_fields(row: dict[str, Any], rarity_row: dict[str, Any]) -> None:
    row.update(
        {
            "rarity": rarity_row["rarity"],
            "rarity_score": rarity_row["rarity_score"],
            "trait_rarity": rarity_row["trait_rarity"],
            "traits": rarity_row["traits"],
        }
    )


def apply_unified_rarity_fields(row: dict[str, Any], rarity_row: dict[str, Any]) -> None:
    old_rarity = row.get("rarity") if isinstance(row.get("rarity"), dict) else {}
    old_traits = row.get("traits") if isinstance(row.get("traits"), list) else []
    search_text = str(row.get("search_text") or "")
    stale_terms = [str(old_rarity.get("display") or "")]
    stale_terms.extend(str(item.get("display") or "") for item in old_traits if isinstance(item, dict))
    for term in stale_terms:
        if term:
            search_text = re.sub(re.escape(term), " ", search_text, flags=re.IGNORECASE)
    structured_traits = unified_trait_items(str(rarity_row["trait_rarity"]))
    row["rarity"] = {
        "display": rarity_row["rarity"],
        "rank": int(str(rarity_row["rarity"]).split("/", 1)[0].lstrip("#")),
        "total": int(str(rarity_row["rarity"]).split("/", 1)[1]),
    }
    row["traits"] = structured_traits
    fresh_terms = [str(rarity_row["rarity"]), *(str(item.get("display") or "") for item in structured_traits)]
    row["search_text"] = " ".join([search_text, *fresh_terms]).lower().split()
    row["search_text"] = " ".join(row["search_text"])


def decimal_or_none(value: Any) -> Decimal | None:
    raw = str(value or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except Exception:  # noqa: BLE001
        return None
    return parsed if parsed.is_finite() else None


def historical_usd_candidate(row: Any) -> dict[str, Any] | None:
    """Normalize complete, non-live event-price provenance from an archive surface."""
    if not isinstance(row, dict):
        return None
    if isinstance(row.get("record"), dict):
        row = row["record"]
    amount = row.get("amount") if isinstance(row.get("amount"), dict) else row
    event_amount = decimal_or_none(
        amount.get("amount_usd_at_event")
        or amount.get("winning_bid_usd_at_settlement")
    )
    event_price = decimal_or_none(amount.get("eth_usd_price_at_event"))
    event_date = str(amount.get("eth_usd_price_date_utc") or "").strip()
    source = str(amount.get("usd_estimate_source") or "").strip()
    if event_amount is None or event_price is None or not event_date or not source:
        return None
    if source.lower() in LIVE_USD_SOURCES:
        return None
    return {
        "amount_usd_at_event": str(event_amount),
        "eth_usd_price_at_event": str(event_price),
        "eth_usd_price_date_utc": event_date,
        "usd_estimate": str(decimal_or_none(amount.get("usd_estimate")) or event_amount),
        "usd_estimate_source": source,
        "usd_estimate_source_detail": str(amount.get("usd_estimate_source_detail") or "").strip(),
        "usd_estimate_confidence": str(amount.get("usd_estimate_confidence") or "medium").strip(),
        "usd_estimate_basis": str(
            amount.get("usd_estimate_basis")
            or amount.get("usd_estimate_time_basis")
            or "settlement_block_time"
        ).strip(),
        "usd_estimate_notes": str(amount.get("usd_estimate_notes") or "").strip(),
    }


def archive_candidates(token_id: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    dog_path = ROOT / "archive" / "dogs" / "by-id" / f"{token_id:03d}.json"
    dog_payload = load_json_file(dog_path, {})
    if isinstance(dog_payload, dict):
        candidates.append(dog_payload)
    unified_path = ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json"
    unified_rows = load_json_file(unified_path, [])
    if isinstance(unified_rows, list):
        candidates.extend(
            row for row in unified_rows
            if isinstance(row, dict) and int_value(row.get("mission"), -1) == 3 and int_value(row.get("dog_id"), -1) == token_id
        )
    for table_name in ("auction_feed", "auction_winners"):
        rows = load_json_file(GENERATED / f"{table_name}.json", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_token_id = int_value(row.get("token_id"), -1)
            if row_token_id < 0:
                match = re.search(r"\d+", str(row.get("dog") or row.get("dog_name") or ""))
                row_token_id = int(match.group(0)) if match else -1
            if row_token_id == token_id:
                candidates.append(row)
    return candidates


def local_historical_usd(
    native_amount: Decimal,
    event_time: Any,
) -> dict[str, Any] | None:
    event_dt = parse_utc(event_time)
    rows = load_json_file(HISTORICAL_PRICES, [])
    if event_dt is None or not isinstance(rows, list):
        return None
    priced_rows: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("asset_key") or "") != "ETH":
            continue
        price = decimal_or_none(row.get("price_usd"))
        row_dt = parse_utc(row.get("timestamp_utc") or row.get("date_utc"))
        if price is None or row_dt is None:
            continue
        distance = abs((row_dt - event_dt).total_seconds())
        if distance <= 3 * 86400:
            priced_rows.append((distance, row))
    if not priced_rows:
        return None
    _distance, price_row = min(priced_rows, key=lambda item: item[0])
    price = decimal_or_none(price_row.get("price_usd"))
    source = str(price_row.get("source") or "").strip()
    if price is None or not source or source.lower() in LIVE_USD_SOURCES:
        return None
    estimate = native_amount * price
    return {
        "amount_usd_at_event": str(estimate.quantize(Decimal("0.00000001"))),
        "eth_usd_price_at_event": str(price),
        "eth_usd_price_date_utc": str(price_row.get("date_utc") or ""),
        "usd_estimate": str(estimate.quantize(Decimal("0.00000001"))),
        "usd_estimate_source": source,
        "usd_estimate_source_detail": str(price_row.get("source_detail") or ""),
        "usd_estimate_confidence": str(price_row.get("confidence") or "medium"),
        "usd_estimate_basis": "settlement_block_time",
        "usd_estimate_notes": str(price_row.get("notes") or ""),
    }


def canonical_historical_usd(token_id: int, native_amount: Decimal, event_time: Any) -> dict[str, Any]:
    event_dt = parse_utc(event_time)
    if not native_amount.is_finite() or native_amount < 0 or event_dt is None:
        raise FullRefreshRequired(f"Dog #{token_id} settlement has invalid onchain amount or timestamp")
    for candidate in archive_candidates(token_id):
        normalized = historical_usd_candidate(candidate)
        if normalized is None:
            continue
        event_amount = decimal_or_none(normalized.get("amount_usd_at_event"))
        event_price = decimal_or_none(normalized.get("eth_usd_price_at_event"))
        price_dt = parse_utc(normalized.get("eth_usd_price_date_utc"))
        if event_amount is None or event_amount < 0 or event_price is None or event_price <= 0 or price_dt is None:
            continue
        expected_amount = native_amount * event_price
        tolerance = max(Decimal("0.011"), abs(expected_amount) * Decimal("0.000001"))
        if abs(event_amount - expected_amount) > tolerance:
            continue
        if abs((price_dt - event_dt).total_seconds()) > 3 * 86400:
            continue
        return normalized
    local_quote = local_historical_usd(native_amount, event_time)
    if local_quote is not None:
        return local_quote
    raise FullRefreshRequired(
        f"Dog #{token_id} settlement has no canonical historical ETH/USD provenance"
    )


def settlement_candidate(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    if isinstance(row.get("record"), dict):
        row = row["record"]
    if isinstance(row.get("settlement"), dict):
        settlement = dict(row["settlement"])
        raw_amount = row.get("amount") if isinstance(row.get("amount"), dict) else {}
        raw_winner = row.get("winner_or_high_bidder") if isinstance(row.get("winner_or_high_bidder"), dict) else {}
        settlement.setdefault("amount_eth", raw_amount.get("native"))
        settlement.setdefault("amount_wei", raw_amount.get("raw"))
        settlement.setdefault("winner", raw_winner.get("wallet"))
        return settlement
    if row.get("tx_hash") or row.get("block_number"):
        return {
            "amount_eth": row.get("winning_bid_eth") or row.get("amount_eth"),
            "amount_wei": row.get("amount_raw"),
            "block_number": row.get("block_number"),
            "block_time_utc": row.get("settled_time_utc") or row.get("auction_time_utc"),
            "token_id": row.get("token_id"),
            "tx_hash": row.get("tx_hash") or row.get("settled_tx_hash"),
            "winner": row.get("winner_wallet") or row.get("bidder_winner_wallet"),
        }
    return None


def preserve_settlement_fields(token_id: int, settlement: dict[str, Any] | None) -> dict[str, Any] | None:
    if settlement is None:
        return None
    merged = dict(settlement)
    target_tx = str(merged.get("tx_hash") or "").lower()
    for raw_candidate in archive_candidates(token_id):
        candidate = settlement_candidate(raw_candidate)
        if candidate is None:
            continue
        candidate_tx = str(candidate.get("tx_hash") or "").lower()
        if target_tx and candidate_tx and target_tx != candidate_tx:
            continue
        for key in ("amount_eth", "amount_wei", "block_number", "block_time_utc", "log_index", "tx_hash", "winner"):
            if merged.get(key) in (None, "") and candidate.get(key) not in (None, ""):
                merged[key] = candidate[key]
        target_tx = str(merged.get("tx_hash") or "").lower()
    return merged


def merge_settled_winner_row(
    existing: dict[str, Any],
    settlement: dict[str, Any],
    historical_usd: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Apply settlement facts without erasing canonical price or block provenance."""
    row = dict(existing)
    row.update(updates)
    # The fast path may revisit an already-settled previous auction after its
    # complete bid ledger has rotated out of current_auction_bid_history. Empty
    # incremental values must not erase timestamps already established by the
    # full onchain build.
    for key in ("first_bid_utc", "last_bid_utc"):
        if row.get(key) in (None, "") and existing.get(key) not in (None, ""):
            row[key] = existing[key]
    event_amount = decimal_or_none(historical_usd.get("amount_usd_at_event"))
    if event_amount is None:
        raise FullRefreshRequired("settled winner is missing canonical event USD")
    row.update(
        {
            "winning_bid_usd": f"{event_amount.quantize(Decimal('0.01')):.2f}",
            "winning_bid_usd_at_settlement": f"{event_amount.quantize(Decimal('0.01')):.2f}",
            "eth_usd_price_at_event": historical_usd["eth_usd_price_at_event"],
            "eth_usd_price_date_utc": historical_usd["eth_usd_price_date_utc"],
            "usd_estimate_source": historical_usd["usd_estimate_source"],
            "usd_estimate_source_detail": historical_usd.get("usd_estimate_source_detail", ""),
            "usd_estimate_confidence": historical_usd.get("usd_estimate_confidence", "medium"),
            "usd_estimate_basis": historical_usd.get("usd_estimate_basis", "settlement_block_time"),
            "block_number": settlement.get("block_number") or existing.get("block_number", ""),
            "tx_hash": settlement.get("tx_hash") or existing.get("tx_hash", ""),
        }
    )
    return row


def display_for(wallet: str, profiles: dict[str, tuple[str, str]]) -> tuple[str, str]:
    return profiles.get(wallet.lower(), (f"{short_wallet(wallet)}", f"https://basescan.org/address/{wallet}"))


def update_readme_snapshot(current_row: dict[str, Any], metrics: dict[str, str], builder: Any) -> None:
    path = ROOT / "README.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    payback = metrics.get("reward_current_bid_payback_days", "N/A")
    payback_display = "N/A"
    try:
        payback_value = Decimal(payback)
        if payback_value > 0:
            places = 1 if payback_value < 10 else 0
            payback_display = f"≈{payback_value:,.{places}f} days"
    except (TypeError, ValueError):
        pass
    replacements = {
        "Snapshot block": str(current_row.get("latest_block", "")),
        "Snapshot time UTC": str(current_row.get("latest_block_time_utc", "")),
        "Current Dog": f"Dog #{current_row.get('token_id', '')}",
        "Current status": str(current_row.get("auction_state", "")),
        "Current bid": f"{current_row.get('current_bid_eth', '')} ETH (${Decimal(str(current_row.get('current_bid_usd', '0'))):,.2f})",
        "Current high bidder": str(current_row.get("bidder", "")),
        "Bid payback / APR": f"{payback_display} / {metrics.get('reward_current_bid_apr_display', 'N/A')}",
        "Season 6 SUP estimate if current bid wins": builder.season6_readme_estimate_summary(metrics),
        "Created / settled auctions": f"{metrics.get('created_auctions', '')} / {metrics.get('settled_auctions', '')}",
        "WOOF holders": builder.woof_holder_summary(metrics),
        "Onchain verification": str(metrics.get("onchain_verification_status", "N/A")),
        "Snapshot block hash": str(metrics.get("snapshot_block_hash", "N/A")),
    }
    for label, value in replacements.items():
        pattern = rf"^\| {re.escape(label)} \|.*$"
        text = re.sub(pattern, f"| {label} | {value} |", text, flags=re.MULTILINE)
    atomic_write_text(path, text)

def main() -> None:
    current_rows = read_json("current_auction")
    if not isinstance(current_rows, list) or not current_rows:
        raise RuntimeError("generated/current_auction.json has no baseline row")
    previous_block = int_value(current_rows[0].get("latest_block"))
    if previous_block <= 0:
        raise RuntimeError("current auction baseline has no latest_block")
    baseline_token_id = int_value(current_rows[0].get("token_id"), -1)
    if baseline_token_id < 0:
        raise RuntimeError("current auction baseline has no token_id")
    timeline_columns, timeline_rows = read_table("auction_timeline")
    history_columns, baseline_history_rows = read_table("current_auction_bid_history")
    recent_bid_columns, baseline_recent_bid_rows = read_table("recent_bids")
    leaderboard_columns, baseline_leaderboard_rows = read_table("auction_bidder_leaderboard")
    daily_columns, baseline_daily_rows = read_table("auction_daily_activity")
    # Detect known baseline corruption before making any network calls. The
    # full builder is the only exact repair when an older auction's bid details
    # were skipped by a previous incremental implementation.
    ensure_no_untracked_bid_gap(timeline_rows, baseline_recent_bid_rows, baseline_history_rows)
    overlap = max(50, int(os.environ.get("MISSION3_CURRENT_SURFACE_OVERLAP", "100")))
    from_block = max(0, previous_block - overlap)

    # Set conservative defaults before importing the full builder module. The
    # fast path needs only a few recent auction ranges and should not overload a
    # public RPC endpoint with the full builder's default worker count.
    os.environ.setdefault("BASE_FROM_BLOCK", str(from_block))
    os.environ.setdefault("BASE_LOG_CHUNK", "100")
    os.environ.setdefault("BASE_LOG_WORKERS", "1")
    os.environ.setdefault("BASE_RPC_ATTEMPTS", "2")
    os.environ.setdefault("BASE_LOG_RPC_TIMEOUT", "20")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_dashboard as builder

    latest_block, latest_block_data, verification = builder.verified_snapshot()
    latest_time = builder.utc_from_unix(int(latest_block_data["timestamp"], 16))
    current = builder.fetch_current_auction(latest_block, latest_time, hex(latest_block))
    token_id = int(current["token_id"])
    total_supply = builder.fetch_dog_total_supply(hex(latest_block))
    metadata = builder.fetch_one_dog_metadata(token_id, hex(latest_block))

    timeline_by_token = {int_value(row.get("token_id"), -1): dict(row) for row in timeline_rows}
    previous_token_id = token_id - 1
    if token_id not in {baseline_token_id, baseline_token_id + 1}:
        raise FullRefreshRequired(
            f"current token jumped from {baseline_token_id} to {token_id}; bounded refresh can reconcile at most one transition"
        )
    historical_snapshot = next((row for row in read_json("historical_dog_search") if int_value(row.get("token_id"), -1) == previous_token_id), {})
    previous_timeline = timeline_by_token.get(previous_token_id, {})
    if not previous_timeline and historical_snapshot:
        previous_timeline = {
            "token_id": str(previous_token_id),
            "dog_image_url": historical_snapshot.get("dog_image_url", ""),
            "start_time_utc": historical_snapshot.get("auction_created_time_utc", ""),
            "end_time_utc": "",
            "auction_state": "live",
            "bids": historical_snapshot.get("bid_count", ""),
            "unique_bidders": historical_snapshot.get("unique_bidder_count", ""),
            "high_bid_eth": "",
            "total_bid_eth": "",
            "latest_bidder": historical_snapshot.get("winner", ""),
            "latest_bidder_url": historical_snapshot.get("winner_url", ""),
            "latest_bid_eth": "",
            "latest_bid_utc": "",
            "winner": "",
            "winner_url": "",
            "settled_eth": "",
            "settled_time_utc": "",
            "rarity": historical_snapshot.get("rarity", ""),
            "created_tx_hash": "",
            "settled_tx_hash": "",
        }
    previous_status = str(previous_timeline.get("auction_state") or "").lower()
    reconcile_previous = bool(previous_timeline) and (
        previous_status in {"live", "ongoing"} or not previous_timeline.get("settled_time_utc") or not previous_timeline.get("settled_tx_hash")
    )

    logs = builder.fetch_logs(
        builder.AUCTION_HOUSE,
        [builder.TOPIC_AUCTION_CREATED, builder.TOPIC_AUCTION_BID, builder.TOPIC_AUCTION_SETTLED],
        from_block,
        latest_block,
    )
    if reconcile_previous:
        # A current-surface refresh may start after the previous auction's
        # settlement block. Reconcile the complete previous-auction window so
        # the old high-bid row is replaced by the actual AuctionSettled event.
        current_start_dt = parse_utc(current.get("start_time_utc"))
        if current_start_dt:
            latest_dt = parse_utc(latest_time)
            if not latest_dt:
                raise RuntimeError("latest block timestamp could not be parsed for settlement reconciliation")
            seconds_per_block = 2.0
            margin_blocks = max(250, int(os.environ.get("MISSION3_SETTLEMENT_RECON_MARGIN_BLOCKS", "500")))
            estimate = latest_block - int(max(0, (latest_dt - current_start_dt).total_seconds()) / seconds_per_block)
            reconcile_from = max(0, estimate - margin_blocks)
            reconcile_to = min(latest_block, estimate + margin_blocks)
            logs.extend(
                builder.fetch_logs(
                    builder.AUCTION_HOUSE,
                    [builder.TOPIC_AUCTION_CREATED, builder.TOPIC_AUCTION_SETTLED],
                    reconcile_from,
                    reconcile_to,
                )
            )
    deduped_logs = {}
    for row in logs:
        if bool(row.get("removed", False)):
            continue
        tx_hash = str(row.get("transactionHash") or "").lower()
        log_index = str(row.get("logIndex") or "").lower()
        deduped_logs[(tx_hash, log_index)] = row
    logs = list(deduped_logs.values())
    created_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_CREATED]
    bid_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_BID]
    settled_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_SETTLED]
    created, bids, settled = builder.decode_auction_logs(created_logs, bid_logs, settled_logs)
    fresh_bids = list(bids)
    previous_settlements = [row for row in settled if int(row["token_id"]) == previous_token_id]
    previous_settlement = max(previous_settlements, key=lambda row: (int(row.get("block_number") or 0), int(row.get("log_index") or 0)), default=None)
    current_settlements = [row for row in settled if int(row["token_id"]) == token_id]
    current_settlement = max(current_settlements, key=lambda row: (int(row.get("block_number") or 0), int(row.get("log_index") or 0)), default=None)
    if reconcile_previous and previous_timeline and previous_settlement is None:
        raise RuntimeError(f"could not reconcile AuctionSettled for previous Dog #{previous_token_id}; refusing partial refresh")
    if previous_settlement is None and previous_timeline.get("settled_time_utc") and previous_timeline.get("settled_tx_hash"):
        previous_feed_snapshot = next((row for row in read_json("auction_feed") if str(row.get("dog")) == f"Dog #{previous_token_id}"), {})
        previous_settlement = {
            "amount_eth": previous_timeline.get("settled_eth") or previous_feed_snapshot.get("amount_eth") or 0,
            "amount_wei": previous_feed_snapshot.get("amount_raw") or "",
            "block_number": "",
            "block_time_utc": previous_timeline.get("settled_time_utc"),
            "log_index": "",
            "token_id": previous_token_id,
            "tx_hash": previous_timeline.get("settled_tx_hash"),
            "winner": previous_feed_snapshot.get("bidder_winner_wallet") or "",
        }
    current_timeline_baseline = timeline_by_token.get(token_id, {})
    if current_settlement is None and current_timeline_baseline.get("settled_time_utc") and current_timeline_baseline.get("settled_tx_hash"):
        current_feed_snapshot = next((row for row in read_json("auction_feed") if str(row.get("dog")) == f"Dog #{token_id}"), {})
        current_settlement = {
            "amount_eth": current_timeline_baseline.get("settled_eth") or current_feed_snapshot.get("amount_eth") or 0,
            "amount_wei": current_feed_snapshot.get("amount_raw") or "",
            "block_number": "",
            "block_time_utc": current_timeline_baseline.get("settled_time_utc"),
            "log_index": "",
            "token_id": token_id,
            "tx_hash": current_timeline_baseline.get("settled_tx_hash"),
            "winner": current_feed_snapshot.get("bidder_winner_wallet") or "",
        }
    if int(current.get("settled") or 0) and current_settlement is None:
        raise FullRefreshRequired(f"contract reports Dog #{token_id} settled without a canonical AuctionSettled event")
    previous_settlement = preserve_settlement_fields(previous_token_id, previous_settlement)
    current_settlement = preserve_settlement_fields(token_id, current_settlement)

    unified_paths = [
        ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json",
        PUBLIC_GENERATED / "unified_dog_search_index.json",
    ]
    try:
        baseline_unified_rows = json.loads(unified_paths[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FullRefreshRequired(f"cannot load unified archive baseline: {exc}") from exc
    if not isinstance(baseline_unified_rows, list) or not baseline_unified_rows:
        raise FullRefreshRequired("unified archive baseline is missing or invalid")
    baseline_unified_current = next(
        (
            row
            for row in baseline_unified_rows
            if isinstance(row, dict)
            and int_value(row.get("mission"), -1) == 3
            and int_value(row.get("dog_id"), -1) == token_id
        ),
        {},
    )
    baseline_unified_previous = next(
        (
            row
            for row in baseline_unified_rows
            if isinstance(row, dict)
            and int_value(row.get("mission"), -1) == 3
            and int_value(row.get("dog_id"), -1) == previous_token_id
        ),
        {},
    )
    previous_unified_sources = {
        str(value)
        for value in (
            (baseline_unified_previous.get("source") or {}).get("sources", [])
            if isinstance(baseline_unified_previous.get("source"), dict)
            else []
        )
    }
    previous_unified_requires_reconcile = bool(previous_timeline) and (
        str(baseline_unified_previous.get("status") or "").lower() != "settled"
        or "generated_auction_winners" not in previous_unified_sources
    )
    try:
        mission3_source_rows = json.loads(MISSION3_SOURCE_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FullRefreshRequired(f"cannot load Mission 3 source index: {exc}") from exc
    if not isinstance(mission3_source_rows, list) or not mission3_source_rows:
        raise FullRefreshRequired("Mission 3 source index is missing or invalid")
    mission3_source_current = next(
        (
            row
            for row in mission3_source_rows
            if isinstance(row, dict) and int_value(row.get("token_id"), -1) == token_id
        ),
        {},
    )
    current_created = max(
        (row for row in created if int(row.get("token_id")) == token_id),
        key=lambda row: (int(row.get("block_number") or 0), str(row.get("tx_hash") or "")),
        default={},
    )
    current_auction_created = resolve_current_auction_creation(
        token_id,
        current.get("start_time_utc"),
        current_created,
        current_timeline_baseline,
        baseline_unified_current,
        mission3_source_current,
        read_json("auction_winners"),
    )

    eth_usd, eth_source = builder.fetch_eth_usd_price()
    amount_eth = Decimal(str(current["amount_eth"]))
    amount_usd = (amount_eth * eth_usd).quantize(Decimal("0.01"))
    wallet = str(current.get("bidder") or "").lower()
    if wallet == builder.ZERO:
        wallet = ""
    profiles = profile_map()
    try:
        for profile in builder.fetch_degendogs_auction_profiles(current):
            address = str(profile.get("address") or "").lower()
            handle = str(profile.get("username") or "").strip().lstrip("@")
            if address and handle:
                profiles[address] = (f"@{handle}", f"https://farcaster.xyz/{handle}")
    except Exception as exc:  # noqa: BLE001
        print(f"warning: current Farcaster identity lookup failed: {exc}", file=sys.stderr)
    bidder, bidder_url = display_for(wallet, profiles)
    formatted_fresh_bids = format_fresh_bid_rows(
        fresh_bids,
        eth_usd=eth_usd,
        eth_source=eth_source,
        price_date_utc=latest_time[:10],
        profiles=profiles,
    )
    active_token_ids = {token_id, baseline_token_id}
    history_by_token = merge_overlap_bid_history(
        baseline_history_rows,
        formatted_fresh_bids,
        from_block=from_block,
        token_ids=active_token_ids,
    )
    # Current-auction history is a live-value surface, so retained older bid
    # events must be repriced with the same ETH/USD snapshot as the headline.
    current_bids = format_fresh_bid_rows(
        history_by_token.get(token_id, []),
        eth_usd=eth_usd,
        eth_source=eth_source,
        price_date_utc=latest_time[:10],
        profiles=profiles,
    )
    previous_bids = format_fresh_bid_rows(
        history_by_token.get(previous_token_id, []),
        eth_usd=eth_usd,
        eth_source=eth_source,
        price_date_utc=latest_time[:10],
        profiles=profiles,
    )
    if token_id != baseline_token_id and not any(int_value(row.get("token_id"), -1) == token_id for row in created):
        raise FullRefreshRequired(f"AuctionCreated for new current Dog #{token_id} is outside the bounded window")

    if amount_eth > 0:
        if not current_bids:
            raise FullRefreshRequired(f"contract reports a bid for Dog #{token_id}, but the complete active bid ledger is empty")
        canonical_high = current_bids[-1]
        if bid_wallet(canonical_high) != wallet or bid_amount(canonical_high) != amount_eth:
            raise FullRefreshRequired(
                f"Dog #{token_id} latest bid does not match pinned contract state: "
                f"events={bid_wallet(canonical_high)} {bid_amount(canonical_high)} contract={wallet} {amount_eth}"
            )
    elif current_bids:
        raise FullRefreshRequired(f"contract reports no bid for Dog #{token_id}, but bid events are present")

    ensure_no_untracked_bid_gap(timeline_rows, baseline_recent_bid_rows, [*formatted_fresh_bids, *current_bids, *previous_bids])
    recent_bid_rows, added_bid_rows, removed_bid_rows, known_bid_rows = reconcile_recent_bid_rows(
        baseline_recent_bid_rows,
        formatted_fresh_bids,
        [*current_bids, *previous_bids],
        from_block=from_block,
    )
    leaderboard_rows = apply_bidder_leaderboard_delta(
        baseline_leaderboard_rows,
        added_bid_rows,
        removed_bid_rows,
        known_bid_rows,
        profiles,
    )

    affected_days = {
        value.date().isoformat()
        for value in (
            parse_utc(row.get("bid_time_utc") or row.get("block_time_utc"))
            for row in [*added_bid_rows, *removed_bid_rows]
        )
        if value is not None
    }
    for value in (
        parse_utc(current.get("start_time_utc")),
        parse_utc((previous_settlement or {}).get("block_time_utc")),
        parse_utc((current_settlement or {}).get("block_time_utc")),
    ):
        if value is not None:
            affected_days.add(value.date().isoformat())
    # Run the coverage check before the first file write. The returned values
    # are rebuilt again after the in-memory timeline has been reconciled.
    recompute_daily_activity(baseline_daily_rows, timeline_rows, known_bid_rows, affected_days)

    end_dt = parse_utc(current.get("end_time_utc"))
    snapshot_dt = parse_utc(latest_time)
    if snapshot_dt is None:
        raise RuntimeError("verified snapshot has no parseable timestamp")
    remaining = max(0, int((end_dt - snapshot_dt).total_seconds())) if end_dt else 0
    if int(current.get("settled") or 0):
        state = "settled"
    elif remaining > 0:
        state = "live"
    else:
        state = "ended_unsettled"
    surface_status = "ongoing" if state == "live" else state
    bid_text = f"{format_eth_amount(amount_eth)} ETH (${amount_usd:,.0f})"
    bid_time = current_bids[-1].get("bid_time_utc") if current_bids else current.get("start_time_utc") or latest_time
    dog_attrs = {
        str(item.get("trait_type")): str(item.get("value"))
        for item in metadata.get("attributes", [])
        if item.get("trait_type") and item.get("value")
    }
    history = read_json("historical_dog_search")
    rarity_universe = build_rarity_universe(history, token_id, dog_attrs, total_supply)
    current_rarity = rarity_universe[token_id]
    rarity = str(current_rarity["rarity"])
    rarity_score = float(current_rarity["rarity_score"])
    traits = str(current_rarity["traits"])
    trait_rarity = str(current_rarity["trait_rarity"])
    historical_by_token = {
        int_value(row.get("token_id"), -1): row
        for row in history
        if isinstance(row, dict) and int_value(row.get("token_id"), -1) >= 0
    }
    rarity_rebase_required = any(
        token not in historical_by_token
        or str(historical_by_token[token].get("rarity") or "") != str(values["rarity"])
        or str(historical_by_token[token].get("trait_rarity") or "") != str(values["trait_rarity"])
        for token, values in rarity_universe.items()
    )
    previous_historical_usd = (
        canonical_historical_usd(
            previous_token_id,
            Decimal(str(previous_settlement.get("amount_eth") or 0)),
            previous_settlement.get("block_time_utc"),
        )
        if previous_settlement
        else None
    )
    current_historical_usd = (
        canonical_historical_usd(
            token_id,
            Decimal(str(current_settlement.get("amount_eth") or 0)),
            current_settlement.get("block_time_utc"),
        )
        if current_settlement
        else None
    )

    current_row = dict(current_rows[0])
    current_row.update(
        {
            "token_id": token_id,
            "dog_name": metadata.get("name") or f"Degen Dog #{token_id}",
            "dog_image_url": metadata.get("image_url") or f"https://api.degendogs.club/images/{token_id}.png",
            "dog_external_url": metadata.get("external_url") or f"https://degendogs.club/#dog{token_id}",
            "dog_opensea_url": builder.dog_opensea_url(token_id),
            "traits": traits,
            "trait_rarity": trait_rarity,
            "rarity": rarity,
            "rarity_score": rarity_score,
            "current_bid": bid_text,
            "current_bid_eth": float(amount_eth),
            "current_bid_usd": float(amount_usd),
            "current_bid_usd_live": float(amount_usd),
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "bidder": bidder,
            "bidder_url": bidder_url,
            "bidder_wallet": wallet,
            "start_time_utc": current.get("start_time_utc", ""),
            "end_time_utc": current.get("end_time_utc", ""),
            "auction_state": state,
            "seconds_remaining": remaining,
            "time_remaining": (
                format_seconds(remaining)
                if state == "live"
                else ("settled" if state == "settled" else "ended; settlement pending")
            ),
            "settled": int(current.get("settled") or 0),
            "latest_block": latest_block,
            "latest_block_time_utc": latest_time,
        }
    )
    builder.verify_snapshot_unchanged(latest_block, verification["snapshot_block_hash"])
    current_columns, _ = read_table("current_auction")
    write_table("current_auction", current_columns, [current_row])

    latest_row = dict(read_json("current_latest_bid")[0])
    latest_row.update(
        {
            "dog": f"Dog #{token_id}",
            "dog_image_url": current_row["dog_image_url"],
            "dog_external_url": current_row["dog_external_url"],
            "dog_opensea_url": current_row["dog_opensea_url"],
            "latest_bid": bid_text,
            "latest_bid_eth": float(amount_eth),
            "latest_bid_usd": float(amount_usd),
            "latest_bid_usd_live": float(amount_usd),
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "bidder": bidder,
            "bidder_url": bidder_url,
            "bidder_wallet": wallet,
            "bid_time_utc": bid_time,
            "auction_state": state,
            "time_remaining": current_row["time_remaining"],
            "auction_end_utc": current.get("end_time_utc", ""),
            "traits": traits,
            "trait_rarity": trait_rarity,
            "rarity": rarity,
        }
    )
    latest_columns, _ = read_table("current_latest_bid")
    write_table("current_latest_bid", latest_columns, [latest_row])

    current_history_rows = [
        {column: row.get(column, "") for column in history_columns}
        for row in current_bids
    ]
    write_table("current_auction_bid_history", history_columns, current_history_rows)

    recent_bid_output = []
    for row in recent_bid_rows:
        normalized = dict(row)
        normalized.setdefault("bid_usd_at_event", normalized.get("bid_usd", ""))
        normalized.setdefault("eth_usd_price_at_event", normalized.get("eth_usd_price_live", ""))
        recent_bid_output.append({column: normalized.get(column, "") for column in recent_bid_columns})
    write_table("recent_bids", recent_bid_columns, recent_bid_output)

    feed_columns, feed_rows = read_table("auction_feed")
    if rarity_rebase_required:
        for row in feed_rows:
            match = re.search(r"(\d+)", str(row.get("dog") or ""))
            feed_token_id = int(match.group(1)) if match else -1
            if feed_token_id in rarity_universe:
                apply_rarity_fields(row, rarity_universe[feed_token_id])
    old_current = dict(feed_rows[0]) if feed_rows else {}
    previous_feed = next((dict(row) for row in feed_rows if int(re.sub(r"\D", "", str(row.get("dog") or "-1")) or -1) == previous_token_id), {})
    if not previous_feed and historical_snapshot:
        raw_previous_amount = Decimal(str(historical_snapshot.get("amount_raw") or 0))
        previous_feed = {
            "dog": f"Dog #{previous_token_id}",
            "dog_image_url": historical_snapshot.get("dog_image_url", ""),
            "dog_external_url": historical_snapshot.get("dog_external_url", ""),
            "dog_opensea_url": historical_snapshot.get("dog_opensea_url", ""),
            "bidder_winner": historical_snapshot.get("winner", ""),
            "bidder_winner_url": historical_snapshot.get("winner_url", ""),
            "bidder_winner_wallet": historical_snapshot.get("winner_wallet", ""),
            "bid": historical_snapshot.get("amount", ""),
            "amount_eth": float(raw_previous_amount / Decimal(10**18)) if raw_previous_amount else 0,
            "amount_raw": historical_snapshot.get("amount_raw", ""),
            "last_bid_utc": "",
            "rarity": historical_snapshot.get("rarity", ""),
            "traits": historical_snapshot.get("traits", ""),
            "trait_rarity": historical_snapshot.get("trait_rarity", ""),
        }
    settled_previous = None
    if previous_settlement:
        settled_previous = dict(previous_feed or {"dog": f"Dog #{previous_token_id}"})
        settlement_time = str(previous_settlement.get("block_time_utc") or current.get("start_time_utc", latest_time))
        settled_previous["status"] = "settled"
        settled_previous["settled_time_utc"] = settlement_time
        settled_previous["auction_time_utc"] = settlement_time
        settled_previous["time_remaining"] = "settled"
        if previous_settlement:
            previous_wallet = str(previous_settlement.get("winner") or "").lower()
            previous_bidder, previous_bidder_url = display_for(previous_wallet, profiles)
            previous_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
            if previous_historical_usd is None:
                raise FullRefreshRequired(f"Dog #{previous_token_id} settlement lost historical USD provenance")
            previous_amount_usd = Decimal(previous_historical_usd["amount_usd_at_event"])
            previous_last_bid = previous_bids[-1].get("bid_time_utc") if previous_bids else settled_previous.get("last_bid_utc", "")
            settled_previous.update(
                {
                    "bidder_winner": previous_bidder,
                    "bidder_winner_url": previous_bidder_url,
                    "bidder_winner_wallet": previous_wallet,
                    "bid": f"{format_eth_amount(previous_amount_eth)} ETH (${previous_amount_usd:,.0f})",
                    "amount_eth": float(previous_amount_eth),
                    "amount_usd": f"{previous_amount_usd.quantize(Decimal('0.01')):.2f}",
                    "amount_usd_at_event": f"{previous_amount_usd.quantize(Decimal('0.01')):.2f}",
                    "eth_usd_price_live": "",
                    "eth_usd_price_at_event": previous_historical_usd["eth_usd_price_at_event"],
                    "eth_usd_price_date_utc": previous_historical_usd["eth_usd_price_date_utc"],
                    "usd_estimate_source": previous_historical_usd["usd_estimate_source"],
                    "usd_estimate_source_detail": previous_historical_usd.get("usd_estimate_source_detail", ""),
                    "usd_estimate_confidence": previous_historical_usd.get("usd_estimate_confidence", "medium"),
                    "usd_estimate_basis": previous_historical_usd.get("usd_estimate_basis", "settlement_block_time"),
                    "last_bid_utc": previous_last_bid,
                }
            )
    new_feed = dict(old_current)
    new_feed.update(
        {
            "status": surface_status,
            "dog": f"Dog #{token_id}",
            "dog_image_url": current_row["dog_image_url"],
            "dog_external_url": current_row["dog_external_url"],
            "dog_opensea_url": current_row["dog_opensea_url"],
            "bidder_winner": bidder,
            "bidder_winner_url": bidder_url,
            "bidder_winner_wallet": wallet,
            "bid": bid_text,
            "amount_eth": float(amount_eth),
            "amount_usd": float(amount_usd),
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "settlement_block_time" if state == "settled" else "current_eth_usd_price",
            "auction_time_utc": (current_settlement or {}).get("block_time_utc", bid_time) if state == "settled" else bid_time,
            "last_bid_utc": bid_time,
            "auction_end_utc": current.get("end_time_utc", ""),
            "settled_time_utc": (current_settlement or {}).get("block_time_utc", "") if state == "settled" else "",
            "time_remaining": current_row["time_remaining"],
            "rarity": rarity,
            "traits": traits,
            "trait_rarity": trait_rarity,
        }
    )
    if state == "settled":
        if current_historical_usd is None:
            raise FullRefreshRequired(f"Dog #{token_id} settlement lost historical USD provenance")
        settled_usd = Decimal(current_historical_usd["amount_usd_at_event"])
        new_feed.update(
            {
                "bid": f"{format_eth_amount(amount_eth)} ETH (${settled_usd:,.0f})",
                "amount_usd": f"{settled_usd.quantize(Decimal('0.01')):.2f}",
                "amount_usd_at_event": f"{settled_usd.quantize(Decimal('0.01')):.2f}",
                "eth_usd_price_live": "",
                "eth_usd_price_at_event": current_historical_usd["eth_usd_price_at_event"],
                "eth_usd_price_date_utc": current_historical_usd["eth_usd_price_date_utc"],
                "usd_estimate_source": current_historical_usd["usd_estimate_source"],
                "usd_estimate_source_detail": current_historical_usd.get("usd_estimate_source_detail", ""),
                "usd_estimate_confidence": current_historical_usd.get("usd_estimate_confidence", "medium"),
                "usd_estimate_basis": current_historical_usd.get("usd_estimate_basis", "settlement_block_time"),
            }
        )
    remaining_feed = [row for row in feed_rows if str(row.get("dog")) not in {f"Dog #{token_id}", f"Dog #{previous_token_id}"}]
    output_feed = [new_feed]
    if settled_previous is not None:
        output_feed.append(settled_previous)
    output_feed.extend(remaining_feed)
    write_table("auction_feed", feed_columns, output_feed)

    # Add the newly minted/auctioned Dog to the unified searchable table.
    history_columns, history_rows = read_table("historical_dog_search")
    if rarity_rebase_required:
        for row in history_rows:
            historical_token_id = int_value(row.get("token_id"), -1)
            if historical_token_id in rarity_universe:
                apply_rarity_fields(row, rarity_universe[historical_token_id])
                row["search_text"] = " ".join(
                    str(row.get(key, "")) for key in row if key != "search_text"
                ).lower()
    history_rows = [row for row in history_rows if int_value(row.get("token_id"), -1) != token_id]
    if previous_settlement:
        previous_wallet = str(previous_settlement.get("winner") or "").lower()
        previous_bidder, previous_bidder_url = display_for(previous_wallet, profiles)
        previous_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
        previous_amount_display = historical_settlement_amount_display(
            previous_token_id,
            previous_amount_eth,
            previous_historical_usd,
        )
        for row in history_rows:
            if int_value(row.get("token_id"), -1) != previous_token_id:
                continue
            row.update(
                {
                    "status": "settled",
                    "winner": previous_bidder,
                    "winner_url": previous_bidder_url,
                    "winner_wallet": previous_wallet,
                    "amount": previous_amount_display,
                    "amount_raw": str(previous_settlement.get("amount_wei") or row.get("amount_raw") or ""),
                    "bid_count": len(previous_bids) or int_value(row.get("bid_count")),
                    "unique_bidder_count": len({bid_wallet(item) for item in previous_bids if bid_wallet(item)}) or int_value(row.get("unique_bidder_count")),
                    "settled_time_utc": previous_settlement.get("block_time_utc", ""),
                    "confidence": "verified_live_base_logs",
                    "sources": "base_logs,dashboard_builder",
                }
            )
            row["search_text"] = " ".join(str(row.get(key, "")) for key in row if key != "search_text")
    template = dict(history_rows[-1] if history_rows else {})
    new_search = dict(template)
    new_search.update(
        {
            "mission": 3,
            "chain": "Base",
            "chain_id": 8453,
            "token_id": token_id,
            "dog": f"Dog #{token_id}",
            "dog_image_url": current_row["dog_image_url"],
            "dog_external_url": current_row["dog_external_url"],
            "dog_opensea_url": current_row["dog_opensea_url"],
            "status": surface_status,
            "winner": bidder,
            "winner_url": bidder_url,
            "winner_wallet": wallet,
            "amount": bid_text,
            "amount_raw": str(current.get("amount_wei", "")),
            "bid_count": len(current_bids),
            "unique_bidder_count": len({bid_wallet(row) for row in current_bids if bid_wallet(row)}),
            "auction_created_time_utc": current.get("start_time_utc", ""),
            "settled_time_utc": (current_settlement or {}).get("block_time_utc", "") if state == "settled" else "",
            "rarity": rarity,
            "traits": traits,
            "trait_rarity": trait_rarity,
            "confidence": "live_base_contract_call",
            "sources": "base_rpc,dog_metadata_api",
            "search_text": f"3 Base 8453 {token_id} Dog #{token_id} {traits} {bidder} {wallet}".strip(),
        }
    )
    write_table("historical_dog_search", history_columns, [*history_rows, new_search])

    report_columns, report_rows = read_table("historical_dog_report")
    report_groups = {"all": history_rows + [new_search]}
    report_groups.update({mission: [row for row in report_groups["all"] if str(row.get("mission")) == mission] for mission in ("1", "2", "3")})
    for report in report_rows:
        group = report_groups.get(str(report.get("mission")))
        if group is None:
            continue
        statuses = [str(row.get("status") or "").lower() for row in group]
        report["dogs"] = len(group)
        report["settled"] = sum(1 for status in statuses if status == "settled" or (status.startswith("settled") and "unsettled" not in status))
        report["live_or_unsettled"] = sum(1 for status in statuses if "live" in status or "ongoing" in status or "unsettled" in status or "pending settlement" in status or "created" in status)
        report["metadata_only"] = sum(1 for status in statuses if status == "metadata_only")
        report["bid_count"] = sum(int_value(row.get("bid_count")) for row in group)
        if str(report.get("mission")) == "all":
            report["latest_activity_utc"] = latest_time
        elif str(report.get("mission")) == "3":
            report["latest_activity_utc"] = bid_time
    write_table("historical_dog_report", report_columns, report_rows)

    # Reconcile the specialized Mission 3 tables too. These are the source
    # tables used to build the unified archive rows and must move together with
    # the homepage feed when a previous auction settles.
    if rarity_rebase_required:
        for row in timeline_rows:
            timeline_token_id = int_value(row.get("token_id"), -1)
            if timeline_token_id in rarity_universe:
                apply_rarity_fields(row, rarity_universe[timeline_token_id])
    timeline_rows = [row for row in timeline_rows if int_value(row.get("token_id"), -1) not in {previous_token_id, token_id}]
    if previous_settlement:
        previous_wallet = str(previous_settlement.get("winner") or "").lower()
        previous_bidder, previous_bidder_url = display_for(previous_wallet, profiles)
        previous_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
        previous_latest_bid = previous_bids[-1] if previous_bids else {}
        previous_timeline_row = dict(previous_timeline)
        previous_timeline_row.update(
            {
                "auction_state": "settled",
                "bids": len(previous_bids) or int_value(previous_timeline.get("bids")),
                "unique_bidders": len({bid_wallet(item) for item in previous_bids if bid_wallet(item)}) or int_value(previous_timeline.get("unique_bidders")),
                "high_bid_eth": format_eth_amount(max([Decimal(str(item.get('bid_eth') or 0)) for item in previous_bids] or [previous_amount_eth])),
                "total_bid_eth": f"{sum((Decimal(str(item.get('bid_eth') or 0)) for item in previous_bids), Decimal(0)):.8f}" if previous_bids else previous_timeline.get("total_bid_eth", ""),
                "latest_bidder": display_for(bid_wallet(previous_latest_bid), profiles)[0] if previous_latest_bid else previous_timeline.get("latest_bidder", ""),
                "latest_bidder_url": display_for(bid_wallet(previous_latest_bid), profiles)[1] if previous_latest_bid else previous_timeline.get("latest_bidder_url", ""),
                "latest_bid_eth": previous_latest_bid.get("bid_eth", previous_timeline.get("latest_bid_eth", "")),
                "latest_bid_utc": previous_latest_bid.get("bid_time_utc", previous_timeline.get("latest_bid_utc", "")),
                "winner": previous_bidder,
                "winner_url": previous_bidder_url,
                "settled_eth": format_eth_amount(previous_amount_eth),
                "settled_time_utc": previous_settlement.get("block_time_utc", ""),
                "settled_tx_hash": previous_settlement.get("tx_hash", ""),
            }
        )
        timeline_rows.append(previous_timeline_row)
    current_timeline_row = dict(timeline_by_token.get(token_id) or {})
    settled_wallet = str((current_settlement or {}).get("winner") or "").lower()
    settled_bidder, settled_bidder_url = display_for(settled_wallet, profiles) if settled_wallet else ("", "")
    settled_amount = Decimal(str((current_settlement or {}).get("amount_eth") or 0))
    current_timeline_row.update(
        {
            "token_id": str(token_id),
            "dog_image_url": current_row["dog_image_url"],
            "start_time_utc": current.get("start_time_utc", ""),
            "end_time_utc": current.get("end_time_utc", ""),
            "auction_state": state,
            "bids": len(current_bids),
            "unique_bidders": len({bid_wallet(item) for item in current_bids if bid_wallet(item)}),
            "high_bid_eth": f"{max((Decimal(str(item.get('bid_eth') or 0)) for item in current_bids), default=amount_eth):.8f}",
            "total_bid_eth": f"{sum((Decimal(str(item.get('bid_eth') or 0)) for item in current_bids), Decimal(0)):.8f}",
            "latest_bidder": bidder,
            "latest_bidder_url": bidder_url,
            "latest_bid_eth": f"{amount_eth:.8f}",
            "latest_bid_utc": bid_time,
            "winner": settled_bidder if state == "settled" else "",
            "winner_url": settled_bidder_url if state == "settled" else "",
            "settled_eth": f"{settled_amount:.8f}" if state == "settled" else "",
            "settled_time_utc": (current_settlement or {}).get("block_time_utc", "") if state == "settled" else "",
            "rarity": rarity,
            "created_tx_hash": current_auction_created["tx_hash"],
            "settled_tx_hash": (current_settlement or {}).get("tx_hash", "") if state == "settled" else "",
        }
    )
    timeline_rows.append(current_timeline_row)
    timeline_rows.sort(key=lambda row: int_value(row.get("token_id"), -1), reverse=True)
    daily_rows = recompute_daily_activity(
        baseline_daily_rows,
        timeline_rows,
        known_bid_rows,
        affected_days,
    )
    write_table("auction_timeline", timeline_columns, timeline_rows)
    write_table("auction_daily_activity", daily_columns, daily_rows)

    winner_columns, winner_rows = read_table("auction_winners")
    if rarity_rebase_required:
        for row in winner_rows:
            winner_token_id = int_value(row.get("token_id"), -1)
            if winner_token_id in rarity_universe:
                apply_rarity_fields(row, rarity_universe[winner_token_id])
    winner_by_token = {int_value(row.get("token_id"), -1): dict(row) for row in winner_rows}
    replace_winner_tokens = {previous_token_id}
    if current_settlement:
        replace_winner_tokens.add(token_id)
    winner_rows = [row for row in winner_rows if int_value(row.get("token_id"), -1) not in replace_winner_tokens]
    if previous_settlement:
        previous_wallet = str(previous_settlement.get("winner") or "").lower()
        previous_bidder, previous_bidder_url = display_for(previous_wallet, profiles)
        previous_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
        if previous_historical_usd is None:
            raise FullRefreshRequired(f"Dog #{previous_token_id} settlement lost historical USD provenance")
        previous_amount_usd = Decimal(previous_historical_usd["amount_usd_at_event"])
        existing_winner_row = dict(winner_by_token.get(previous_token_id, {}))
        winner_row = merge_settled_winner_row(
            existing_winner_row,
            previous_settlement,
            previous_historical_usd,
            {
                "settled_time_utc": previous_settlement.get("block_time_utc", ""),
                "token_id": previous_token_id,
                "dog_name": f"Degen Dog #{previous_token_id}",
                "dog_image_url": settled_previous.get("dog_image_url", previous_timeline.get("dog_image_url", "")),
                "dog_external_url": settled_previous.get("dog_external_url", f"https://degendogs.club/#dog{previous_token_id}"),
                "dog_opensea_url": settled_previous.get("dog_opensea_url", builder.dog_opensea_url(previous_token_id)),
                "winner_wallet": previous_wallet,
                "winner": previous_bidder,
                "winner_url": previous_bidder_url,
                "winning_bid": f"{format_eth_amount(previous_amount_eth)} ETH (${previous_amount_usd:,.0f})",
                "winning_bid_eth": format_eth_amount(previous_amount_eth),
                "bid_count": len(previous_bids) or int_value(previous_timeline.get("bids")),
                "unique_bidders": len({bid_wallet(item) for item in previous_bids if bid_wallet(item)}) or int_value(previous_timeline.get("unique_bidders")),
                "first_bid_utc": previous_bids[0].get("bid_time_utc", "") if previous_bids else "",
                "last_bid_utc": previous_bids[-1].get("bid_time_utc", "") if previous_bids else "",
            },
        )
        winner_rows.append(winner_row)
    if current_settlement:
        current_winner_wallet = str(current_settlement.get("winner") or "").lower()
        current_winner, current_winner_url = display_for(current_winner_wallet, profiles)
        current_winning_eth = Decimal(str(current_settlement.get("amount_eth") or 0))
        if current_historical_usd is None:
            raise FullRefreshRequired(f"Dog #{token_id} settlement lost historical USD provenance")
        current_winning_usd = Decimal(current_historical_usd["amount_usd_at_event"])
        existing_current_winner = dict(winner_by_token.get(token_id, {}))
        current_winner_row = merge_settled_winner_row(
            existing_current_winner,
            current_settlement,
            current_historical_usd,
            {
                "settled_time_utc": current_settlement.get("block_time_utc", ""),
                "token_id": token_id,
                "dog_name": current_row["dog_name"],
                "dog_image_url": current_row["dog_image_url"],
                "dog_external_url": current_row["dog_external_url"],
                "dog_opensea_url": current_row["dog_opensea_url"],
                "traits": traits,
                "trait_rarity": trait_rarity,
                "rarity": rarity,
                "rarity_score": rarity_score,
                "winner_wallet": current_winner_wallet,
                "winner": current_winner,
                "winner_url": current_winner_url,
                "winning_bid": f"{format_eth_amount(current_winning_eth)} ETH (${current_winning_usd:,.0f})",
                "winning_bid_eth": format_eth_amount(current_winning_eth),
                "bid_count": len(current_bids),
                "unique_bidders": len({bid_wallet(item) for item in current_bids if bid_wallet(item)}),
                "first_bid_utc": current_bids[0].get("bid_time_utc", "") if current_bids else "",
                "last_bid_utc": current_bids[-1].get("bid_time_utc", "") if current_bids else "",
            },
        )
        winner_rows.append(current_winner_row)
    winner_rows.sort(key=lambda row: str(row.get("settled_time_utc", "")), reverse=True)
    write_table("auction_winners", winner_columns, winner_rows)
    leaderboard_rows = apply_winner_stats_to_leaderboard(leaderboard_rows, winner_rows)
    write_table("auction_bidder_leaderboard", leaderboard_columns, leaderboard_rows)

    recent_columns, _ = read_table("recent_auction_winners")
    recent_rows = []
    for row in winner_rows[:10]:
        recent_rows.append({
            "dog": f"Dog #{int_value(row.get('token_id'), -1)}",
            "dog_image_url": row.get("dog_image_url", ""),
            "dog_external_url": row.get("dog_external_url", ""),
            "dog_opensea_url": row.get("dog_opensea_url", ""),
            "winner": row.get("winner", ""),
            "winner_url": row.get("winner_url", ""),
            "winning_bid": row.get("winning_bid", ""),
            "winning_bid_eth": row.get("winning_bid_eth", ""),
            "winning_bid_usd": row.get("winning_bid_usd", ""),
            "winning_bid_usd_at_settlement": row.get("winning_bid_usd_at_settlement", ""),
            "eth_usd_price_at_event": row.get("eth_usd_price_at_event", ""),
            "eth_usd_price_date_utc": row.get("eth_usd_price_date_utc", ""),
            "usd_estimate_source": row.get("usd_estimate_source", ""),
            "usd_estimate_source_detail": row.get("usd_estimate_source_detail", ""),
            "usd_estimate_confidence": row.get("usd_estimate_confidence", ""),
            "usd_estimate_basis": row.get("usd_estimate_basis", ""),
            "rarity": row.get("rarity", ""),
            "last_bid_utc": row.get("last_bid_utc", ""),
            "settled_time_utc": row.get("settled_time_utc", ""),
        })
    write_table("recent_auction_winners", recent_columns, recent_rows)

    unified_rows = json.loads(json.dumps(baseline_unified_rows))
    if rarity_rebase_required:
        for row in unified_rows:
            if not isinstance(row, dict):
                continue
            unified_token_id = int_value(row.get("dog_id"), -1)
            if unified_token_id in rarity_universe:
                apply_unified_rarity_fields(row, rarity_universe[unified_token_id])
    unified_rows = [row for row in unified_rows if not (int_value(row.get("mission"), -1) == 3 and int_value(row.get("dog_id"), -1) == token_id)]
    should_reconcile_previous_unified = (
        reconcile_previous
        or token_id != baseline_token_id
        or previous_unified_requires_reconcile
    )
    for row in unified_rows:
        if (
            should_reconcile_previous_unified
            and int_value(row.get("mission"), -1) == 3
            and (
                int_value(row.get("dog_id"), -1) == previous_token_id
                or str(row.get("status", "")).lower() == "live"
                or "ongoing" in str(row.get("status", "")).lower()
            )
        ):
            row["status"] = "settled"
            settlement_time = str((previous_settlement or {}).get("block_time_utc") or current.get("start_time_utc", latest_time)).replace(" ", "T") + "Z"
            row["settlement"] = {"block_number": int((previous_settlement or {}).get("block_number") or 0) or None, "block_time_utc": settlement_time, "settled": True, "tx_hash": (previous_settlement or {}).get("tx_hash"), "tx_url": f"https://basescan.org/tx/{(previous_settlement or {}).get('tx_hash')}" if (previous_settlement or {}).get("tx_hash") else None}
            row["activity_time_basis"] = "settlement_block_time"
            row["activity_time_utc"] = settlement_time
            if previous_settlement:
                prior_wallet = str(previous_settlement.get("winner") or "").lower()
                prior_display, prior_url = display_for(prior_wallet, profiles)
                prior_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
                if previous_historical_usd is None:
                    raise FullRefreshRequired(f"Dog #{previous_token_id} settlement lost historical USD provenance")
                prior_amount_usd = Decimal(previous_historical_usd["amount_usd_at_event"])
                event_price_usd = previous_historical_usd["eth_usd_price_at_event"]
                event_price_date = previous_historical_usd["eth_usd_price_date_utc"]
                existing_identity = row.get("winner_or_high_bidder") if isinstance(row.get("winner_or_high_bidder"), dict) else {}
                is_farcaster_profile = str(prior_url).startswith("https://farcaster.xyz/")
                row["winner_or_high_bidder"] = {
                    "display": prior_display,
                    "farcaster_fid": existing_identity.get("farcaster_fid"),
                    "farcaster_handle": prior_display.lstrip("@") if is_farcaster_profile else None,
                    "profile_url": prior_url,
                    "wallet": prior_wallet,
                    "wallet_explorer_url": f"https://basescan.org/address/{prior_wallet}" if prior_wallet else "",
                }
                prior_amount = row.get("amount") if isinstance(row.get("amount"), dict) else {}
                prior_amount.update(
                    {
                        "native": format_eth_amount(prior_amount_eth),
                        "native_symbol": "ETH",
                        "price_asset_key": "ETH",
                        "raw": str(int(prior_amount_eth * Decimal(10**18))),
                        "amount_usd_at_event": previous_historical_usd["amount_usd_at_event"],
                        "eth_usd_price_at_event": event_price_usd,
                        "eth_usd_price_date_utc": event_price_date,
                        "usd_estimate": f"{prior_amount_usd:.8f}",
                        "usd_estimate_confidence": previous_historical_usd.get("usd_estimate_confidence", "medium"),
                        "usd_estimate_display": f"${prior_amount_usd:.2f}",
                        "usd_estimate_notes": previous_historical_usd.get("usd_estimate_notes", "Historical ETH/USD event price applied."),
                        "usd_estimate_price_date_utc": event_price_date,
                        "usd_estimate_price_usd": str(event_price_usd),
                        "usd_estimate_source": previous_historical_usd["usd_estimate_source"],
                        "usd_estimate_source_detail": previous_historical_usd.get("usd_estimate_source_detail", ""),
                        "usd_estimate_time_basis": "settlement_block_time",
                    }
                )
                row["amount"] = prior_amount
                existing_stats = row.get("bid_stats") if isinstance(row.get("bid_stats"), dict) else {}
                prior_bid_count = len(previous_bids) or int_value(existing_stats.get("bid_count")) or int_value(historical_snapshot.get("bid_count")) or int_value(previous_timeline.get("bids"))
                prior_unique_bidder_count = len({bid_wallet(item) for item in previous_bids if bid_wallet(item)}) or int_value(existing_stats.get("unique_bidder_count")) or int_value(historical_snapshot.get("unique_bidder_count")) or int_value(previous_timeline.get("unique_bidders"))
                prior_last_bid_time = previous_bids[-1].get("bid_time_utc") if previous_bids else existing_stats.get("last_bid_time_utc", "")
                row["bid_stats"] = {"bid_count": prior_bid_count, "last_bid_time_utc": prior_last_bid_time, "unique_bidder_count": prior_unique_bidder_count}
                if previous_bids:
                    row["bid_tx_hashes"] = [str(item.get("tx_hash")) for item in previous_bids if item.get("tx_hash")]
                elif not isinstance(row.get("bid_tx_hashes"), list):
                    row["bid_tx_hashes"] = []
                row["links"] = dict(row.get("links") or {})
                settlement_tx_hash = str((previous_settlement or {}).get("tx_hash") or "")
                row["links"]["settlement_tx"] = f"https://basescan.org/tx/{settlement_tx_hash}" if settlement_tx_hash else None
                source = dict(row.get("source") or {})
                source.update(
                    {
                        "confidence": "verified",
                        "notes": "Mission 3 source of truth is Base auction logs reconciled with generated canonical tables.",
                        "raw_confidence": "verified_onchain_generated_tables",
                    }
                )
                sources = [str(value) for value in source.get("sources", []) if str(value)]
                for required_source in (
                    "base_logs",
                    "dashboard_builder",
                    "generated_auction_timeline",
                    "generated_auction_feed",
                    "generated_auction_winners",
                ):
                    if required_source not in sources:
                        sources.append(required_source)
                source["sources"] = sources
                row["source"] = source
                trait_text = " ".join(str(item.get("display", "")) for item in (row.get("traits") if isinstance(row.get("traits"), list) else []))
                rarity_text = str((row.get("rarity") or {}).get("display", "")) if isinstance(row.get("rarity"), dict) else str(row.get("rarity", ""))
                row["search_text"] = " ".join(str(value) for value in [
                    "dog", previous_token_id, f"dog #{previous_token_id}", "mission 3", "base", "settled",
                    prior_display, prior_wallet, f"{prior_amount_eth:.8f} ETH", f"${prior_amount_usd:.2f}", settlement_time,
                    rarity_text, trait_text, (previous_settlement or {}).get("tx_hash", ""),
                ] if value).lower()

    unified_template = baseline_unified_current or next((row for row in unified_rows if int_value(row.get("mission"), -1) == 3), {})
    trait_items = unified_trait_items(trait_rarity)
    tx_hashes = [str(row.get("tx_hash")) for row in current_bids if row.get("tx_hash")]
    if not tx_hashes:
        tx_hashes = [str(row.get("tx_hash")) for row in current_history_rows if row.get("tx_hash")]
    if state == "settled":
        if current_historical_usd is None:
            raise FullRefreshRequired(f"Dog #{token_id} settlement lost historical USD provenance")
        unified_usd = Decimal(current_historical_usd["amount_usd_at_event"])
        unified_amount = {
            "amount_usd_at_event": current_historical_usd["amount_usd_at_event"],
            "eth_usd_price_at_event": current_historical_usd["eth_usd_price_at_event"],
            "eth_usd_price_date_utc": current_historical_usd["eth_usd_price_date_utc"],
            "native": str(amount_eth),
            "native_symbol": "ETH",
            "price_asset_key": "ETH",
            "raw": str(current.get("amount_wei", "")),
            "usd_estimate": f"{unified_usd:.8f}",
            "usd_estimate_confidence": current_historical_usd.get("usd_estimate_confidence", "medium"),
            "usd_estimate_display": f"${unified_usd:.2f}",
            "usd_estimate_notes": current_historical_usd.get("usd_estimate_notes", "Historical ETH/USD event price applied."),
            "usd_estimate_price_date_utc": current_historical_usd["eth_usd_price_date_utc"],
            "usd_estimate_price_usd": current_historical_usd["eth_usd_price_at_event"],
            "usd_estimate_source": current_historical_usd["usd_estimate_source"],
            "usd_estimate_source_detail": current_historical_usd.get("usd_estimate_source_detail", ""),
            "usd_estimate_time_basis": "settlement_block_time",
        }
    else:
        unified_amount = {
            "native": str(amount_eth),
            "native_symbol": "ETH",
            "price_asset_key": "ETH",
            "raw": str(current.get("amount_wei", "")),
            "usd_estimate": f"{amount_usd:.8f}",
            "usd_estimate_confidence": "live_current",
            "usd_estimate_display": f"${amount_usd:.2f}",
            "usd_estimate_price_date_utc": latest_time[:10],
            "usd_estimate_price_usd": str(eth_usd),
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_time_basis": "last_bid_block_time",
            "eth_usd_price_date_utc": latest_time[:10],
        }
    unified_source = {
        "confidence": "verified",
        "notes": "Mission 3 live/current source of truth is Base auction logs and current auction contract state.",
        "raw_confidence": "verified_onchain_generated_tables",
        "sources": [
            "base_logs",
            "dashboard_builder",
            "generated_auction_timeline",
            "generated_auction_feed",
            *(["generated_auction_winners"] if state == "settled" else []),
        ],
    }
    unified_row = json.loads(json.dumps(unified_template))
    unified_row.update(
        {
            "activity_time_basis": "last_bid_block_time",
            "activity_time_utc": str(bid_time).replace(" ", "T") + "Z",
            "amount": unified_amount,
            "auction_created": current_auction_created,
            "bid_stats": {"bid_count": len(current_bids), "last_bid_time_utc": str(bid_time).replace(" ", "T") + "Z", "unique_bidder_count": len({bid_wallet(row) for row in current_bids if bid_wallet(row)})},
            "bid_tx_hashes": tx_hashes,
            "chain": "Base",
            "chain_id": 8453,
            "dog_id": token_id,
            "dog_image_url": current_row["dog_image_url"],
            "dog_item_url": current_row["dog_opensea_url"],
            "era_label": "Mission 3",
            "links": {"auction_tx": current_auction_created["tx_url"], "dog_page": current_row["dog_external_url"], "explorer": f"https://basescan.org/address/{wallet}", "item": current_row["dog_opensea_url"], "repo_archive": f"archive/dogs/by-id/{token_id:03d}.json", "settlement_tx": None},
            "mission": 3,
            "rarity": {"display": rarity, "rank": int(rarity.split("/")[0].lstrip("#")), "total": total_supply},
            "search_text": (
                f"dog {token_id} dog #{token_id} {token_id} mission 3 mission 3 base ongoing "
                f"{wallet} {bidder} {amount_eth} eth {amount_usd} ${amount_usd:.2f} "
                f"{bid_time} {rarity} {traits} {bidder} {current_auction_created['tx_hash']} {' '.join(tx_hashes)}"
            ).strip(),
            "source": unified_source,
            "status": surface_status,
            "traits": trait_items,
            "winner_or_high_bidder": {"display": bidder, "farcaster_fid": None, "farcaster_handle": bidder.lstrip("@"), "profile_url": bidder_url, "wallet": wallet, "wallet_explorer_url": f"https://basescan.org/address/{wallet}"},
            "settlement": {
                "block_number": int_value((current_settlement or {}).get("block_number")) or None,
                "block_time_utc": (
                    str((current_settlement or {}).get("block_time_utc") or "").replace(" ", "T") + "Z"
                    if current_settlement
                    else None
                ),
                "settled": state == "settled",
                "tx_hash": (current_settlement or {}).get("tx_hash") or None,
                "tx_url": (
                    f"https://basescan.org/tx/{current_settlement.get('tx_hash')}"
                    if current_settlement and current_settlement.get("tx_hash")
                    else None
                ),
            },
        }
    )
    unified_rows.append(unified_row)
    unified_rows.sort(key=lambda row: (1 if str(row.get("status", "")).lower() == "live" or "ongoing" in str(row.get("status", "")).lower() else 0, str(row.get("activity_time_utc", "")), int_value(row.get("dog_id"), -1)), reverse=True)
    unified_payload = json.dumps(unified_rows, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    for unified_path in unified_paths:
        atomic_write_text(unified_path, unified_payload)
    unified_record_ids = {
        int_value(row.get("dog_id"), -1)
        for row in unified_rows
        if isinstance(row, dict) and int_value(row.get("dog_id"), -1) >= 0
    }
    # The unified archive intentionally contains only auction-backed records;
    # Mission 2 has a small set of metadata-only Dogs with no unified/by-id
    # record. Rebase every record that actually exists without inventing a
    # synthetic auction record for those sparse IDs.
    affected_unified_tokens = (
        set(rarity_universe) & unified_record_ids
        if rarity_rebase_required
        else {token_id}
    )
    if should_reconcile_previous_unified:
        affected_unified_tokens.add(previous_token_id)
    write_unified_by_id_records(unified_rows, affected_unified_tokens)
    # The fast path can add a newly minted Dog, so keep the cached historical
    # USD estimate table in the same transaction boundary as unified/by-id.
    # This is local-only and performs no network fetch.
    import archive_apply_usd_estimates as archive_prices

    archive_prices.main()

    metrics_rows = read_json("mission3_metrics")
    metrics = {str(row.get("metric")): str(row.get("value", "")) for row in metrics_rows}
    metrics["eth_usd_price"] = str(eth_usd)
    season6_settled_rows = [
        {
            "token_id": int_value(row.get("token_id"), -1),
            "winner": str(row.get("winner_wallet") or "").lower(),
            "amount_eth": row.get("winning_bid_eth") or 0,
            "settled_time_utc": row.get("settled_time_utc") or "",
        }
        for row in winner_rows
    ]
    season6_current = {
        "token_id": token_id,
        "bidder_wallet": wallet if state != "settled" and amount_eth > 0 else "",
        "amount_eth": amount_eth if state != "settled" else Decimal(0),
        "end_time_utc": current.get("end_time_utc", ""),
    }
    season6_profiles = [
        {
            "address": address,
            "username": label.lstrip("@") if label.startswith("@") else "",
        }
        for address, (label, _url) in profiles.items()
    ]
    season6_outputs = builder.build_season6_sup_outputs(
        season6_settled_rows,
        season6_current,
        metrics,
        snapshot_time_utc=latest_time,
        profiles=season6_profiles,
    )
    metrics.update({str(key): str(value) for key, value in season6_outputs["season6_metrics"].items()})
    season6_tables = {
        "season6_sup_by_winner": builder.SEASON6_BY_WINNER_COLUMNS,
        "season6_sup_rewards_by_auction": builder.SEASON6_REWARDS_BY_AUCTION_COLUMNS,
        "season6_sup_current_bidder_status": builder.SEASON6_CURRENT_BIDDER_STATUS_COLUMNS,
    }
    for table_name, fallback_columns in season6_tables.items():
        table_columns, _old_rows = read_table(table_name)
        write_table(
            table_name,
            table_columns or list(fallback_columns),
            list(season6_outputs.get(table_name) or []),
        )
    season6_metric_columns, _old_season6_metrics = read_table("season6_metrics")
    write_table(
        "season6_metrics",
        season6_metric_columns or ["metric", "value"],
        [
            {"metric": str(key), "value": str(value)}
            for key, value in sorted(season6_outputs["season6_metrics"].items())
        ],
    )
    metrics.update(builder.current_bid_reward_stats(current, metrics))
    metrics["created_auctions"] = str(len(timeline_rows))
    metrics["settled_auctions"] = str(sum(1 for row in timeline_rows if str(row.get("auction_state", "")).lower() == "settled"))
    metrics["total_bid_eth"] = f"{sum((Decimal(str(row.get('total_bid_eth') or 0)) for row in timeline_rows), Decimal(0)):.8f}"
    settled_amounts = [Decimal(str(row.get("settled_eth"))) for row in timeline_rows if row.get("settled_eth")]
    if settled_amounts:
        metrics["total_settled_eth"] = f"{sum(settled_amounts, Decimal(0)):.8f}"
        metrics["highest_bid_eth"] = f"{max((Decimal(str(row.get('high_bid_eth') or 0)) for row in timeline_rows), default=Decimal(0)):.8f}"
    metrics.update(
        {
            "current_auction_token_id": str(token_id),
            "dog_total_supply": str(total_supply),
            "current_auction_status": state,
            "current_auction_end_utc": str(current.get("end_time_utc", "")),
            "current_bid_eth": str(current.get("amount_eth", "")),
            "current_bid_usd": str(amount_usd),
            "current_bidder": bidder,
            "current_bidder_wallet": wallet,
            "latest_block": str(latest_block),
            "latest_block_time_utc": latest_time,
            **{str(key): str(value) for key, value in verification.items()},
        }
    )
    metric_rows = [{"metric": key, "value": value} for key, value in metrics.items()]
    metric_columns, _ = read_table("mission3_metrics")
    write_table("mission3_metrics", metric_columns or ["metric", "value"], metric_rows)
    update_readme_snapshot(current_row, metrics, builder)
    refresh_status = read_json("refresh_status")
    refresh_status.update(
        {
            "last_refresh_result": "success_generated",
            "last_successful_refresh_time_utc": now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "latest_generated_block": latest_block,
            "latest_generated_block_time_utc": latest_time,
            "current_dog_token_id": token_id,
            "current_auction_end_time_utc": current.get("end_time_utc", ""),
            "current_auction_status": state,
            "current_bid_eth": str(current.get("amount_eth", "")),
            "current_high_bidder": bidder,
            "current_high_bidder_wallet": wallet,
            "refresh_reason": "current_surface_incremental",
            **{str(key): str(value) for key, value in verification.items()},
        }
    )
    write_json("refresh_status", refresh_status)

    manifest_rows = []
    for name in builder.OUTPUT_TABLES:
        csv_path = GENERATED / f"{name}.csv"
        with csv_path.open(newline="", encoding="utf-8") as handle:
            row_count = max(0, sum(1 for _ in handle) - 1)
        manifest_rows.append({"table": name, "file": f"generated/{name}.csv", "rows": row_count})
    for directory in (GENERATED, PUBLIC_GENERATED):
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=["table", "file", "rows"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest_rows)
        atomic_write_text(directory / "manifest.csv", buffer.getvalue())
    write_json("manifest", manifest_rows)

    tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    for name in builder.OUTPUT_TABLES:
        columns, rows = read_table(name)
        tables[name] = (columns, [tuple(row.get(column, "") for column in columns) for row in rows])
    builder.write_html(tables)
    print(json.dumps({"status": "success_current_surface", "token_id": token_id, "latest_block": latest_block, "new_logs": len(logs), "new_bids": len(current_bids), "current_bid_eth": str(amount_eth)}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except FullRefreshRequired as exc:
        print(json.dumps({"status": "full_refresh_required", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(75) from exc
