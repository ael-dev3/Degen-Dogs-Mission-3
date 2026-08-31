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
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timezone
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
RARITY_TRAIT_TYPES = (
    "Background",
    "Body",
    "Neck",
    "Mouth",
    "Ears",
    "Head",
    "Eyes",
)
FULL_TOKEN_URI_VERIFICATION_STATUS = "hash_pinned_cross_provider_exact_outcome_quorum"
CONTINUITY_TOKEN_URI_VERIFICATION_STATUS = (
    "baseline_hash_pinned_quorum_plus_cross_provider_rarity_event_continuity"
)
FULL_EXISTENCE_VERIFICATION_STATUS = "hash_pinned_cross_provider_exists_token_uri_parity_quorum"
CONTINUITY_EXISTENCE_VERIFICATION_STATUS = (
    "baseline_exists_token_uri_quorum_plus_cross_provider_rarity_event_continuity"
)
FULL_RARITY_CONTINUITY_STATUS = "full_snapshot_exists_token_uri_content_schema_attested"
INCREMENTAL_RARITY_CONTINUITY_STATUS = (
    "hash_pinned_cross_provider_no_existence_or_token_uri_mutation_events_since_attestation"
)
EXTENDED_RARITY_CONTINUITY_STATUS = (
    "hash_pinned_cross_provider_canonical_mint_extension_plus_no_other_rarity_mutations"
)
ZERO_ADDRESS_TOPIC = "0x" + "0" * 64
RARITY_MUTATION_TOPICS = (
    # Transfer(address,address,uint256)
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    # BaseURIUpdated(string,string)
    "0x309b29ded109b9e28fb9885757b3e0096eb75c51d23aa4635d68bcd569f6adc1",
    # MetadataUpdate(uint256)
    "0xf8e1a15aba9398e019f0b49df1a4fde98ee17ae345cb5f6b5e2c27f5033e8ce7",
    # BatchMetadataUpdate(uint256,uint256)
    "0x6bd5c950a8d8df17f772f5af37cb3655737899cbf903264b9795592da439661c",
)

REFRESH_STATUS_INTEGER_FIELDS = frozenset(
    {
        "latest_generated_block",
        "current_dog_token_id",
        "onchain_chain_id",
        "snapshot_confirmations",
        "rpc_quorum_size",
        "dog_total_supply",
        "dog_id_ceiling",
        "dog_token_uri_present_count",
        "dog_token_uri_unavailable_count",
        "dog_base_existing_count",
        "dog_base_unclaimed_count",
        "dog_metadata_onchain_verified_count",
        "dog_metadata_unavailable_count",
        "dog_metadata_content_observed_count",
        "dog_rarity_universe_count",
        "dog_rarity_excluded_nonexistent_count",
        "dog_rarity_incomplete_metadata_count",
        "dog_rarity_attested_block",
        "dog_rarity_continuity_through_block",
        "dog_rarity_extension_mint_count",
        "live_snapshot_bundle_bytes",
        "live_snapshot_bundle_schema_version",
        "unified_dog_search_bytes",
    }
)


class FullRefreshRequired(RuntimeError):
    """Raised before writes when committed artifacts cannot support an exact delta."""


class TransientSnapshotLag(RuntimeError):
    """Raised when the verified RPC tip is older than the persisted checkpoint."""


def auction_feed_status(auction_state: str) -> str:
    return {
        "live": "ongoing",
        "ended_unsettled": "ended pending settlement",
    }.get(auction_state, auction_state)


def bid_tx_hash(row: dict[str, Any]) -> str:
    return str(row.get("tx_hash") or row.get("transaction_hash") or "").strip().lower()


def bid_wallet(row: dict[str, Any]) -> str:
    return str(row.get("bidder_wallet") or row.get("bidder") or "").strip().lower()


def bid_amount(row: dict[str, Any]) -> Decimal:
    return Decimal(str(row.get("bid_eth") or row.get("amount_eth") or 0))


_SQLITE_FORMATTER = sqlite3.connect(":memory:")


def sqlite_real(value: Any) -> float:
    number = Decimal(str(value or 0))
    if not number.is_finite():
        raise FullRefreshRequired("dashboard numeric value is not finite")
    return float(number)


def numeric_8(value: Any) -> float:
    """Use the same binary REAL and ROUND(..., 8) path as the full SQL build."""
    return float(_SQLITE_FORMATTER.execute("SELECT ROUND(?, 8)", (sqlite_real(value),)).fetchone()[0])


def require_exact_8_decimal_amount(value: Any, label: str) -> None:
    """Fail to the full builder before bounded aggregates discard sub-1e-8 residuals."""
    amount = Decimal(str(value or 0))
    scaled = amount * Decimal(100_000_000)
    if not amount.is_finite() or amount < 0 or scaled != scaled.to_integral_value():
        raise FullRefreshRequired(f"{label} amount requires more than 8 decimal places")


def numeric_2(value: Any) -> float:
    """Use the same binary REAL and ROUND(..., 2) path as the full SQL build."""
    return float(_SQLITE_FORMATTER.execute("SELECT ROUND(?, 2)", (sqlite_real(value),)).fetchone()[0])


def format_eth_5(value: Any) -> str:
    """Match the full dashboard's printf('%.5f ETH', ...) display contract."""
    return str(
        _SQLITE_FORMATTER.execute("SELECT printf('%.5f', ?)", (sqlite_real(value),)).fetchone()[0]
    )


def format_usd_0(value: Any) -> str:
    """Match the full dashboard's printf('$%.0f', ...) display contract."""
    return str(
        _SQLITE_FORMATTER.execute("SELECT printf('%.0f', ?)", (sqlite_real(value),)).fetchone()[0]
    )


def numeric_usd_2(amount_eth: Any, eth_usd: Any) -> float:
    """Match SQLite REAL multiplication followed by ROUND(..., 2)."""
    return float(
        _SQLITE_FORMATTER.execute(
            "SELECT ROUND(? * ?, 2)",
            (sqlite_real(amount_eth), sqlite_real(eth_usd)),
        ).fetchone()[0]
    )


def format_usd_product_0(amount_eth: Any, eth_usd: Any) -> str:
    """Match SQLite printf('%.0f', amount_eth * eth_usd)."""
    return str(
        _SQLITE_FORMATTER.execute(
            "SELECT printf('%.0f', ? * ?)",
            (sqlite_real(amount_eth), sqlite_real(eth_usd)),
        ).fetchone()[0]
    )


def reconcile_timeline_extensions(
    baseline: dict[str, Any],
    created: dict[str, Any],
    canonical_rows: list[dict[str, Any]],
    *,
    authenticated_checkpoint_block: int,
    snapshot_block: int,
    expected_end_time_utc: Any = None,
) -> dict[str, Any]:
    """Rebuild a schedule from its raw extension export and a re-proved checkpoint."""
    if authenticated_checkpoint_block <= 0 or snapshot_block < authenticated_checkpoint_block:
        raise FullRefreshRequired("auction timeline checkpoint was not authenticated")
    baseline_initial = str(baseline.get("initial_end_time_utc") or "")
    created_initial = str(created.get("end_time_utc") or "")
    if baseline_initial and created_initial and baseline_initial != created_initial:
        raise FullRefreshRequired("AuctionCreated initial end time disagrees with the timeline")
    initial_end = baseline_initial or created_initial
    if not initial_end or parse_utc(initial_end) is None:
        raise FullRefreshRequired("auction timeline is missing its canonical initial end time")
    if not baseline and int_value(created.get("block_number"), -1) <= authenticated_checkpoint_block:
        raise FullRefreshRequired("persisted timeline omitted a pre-checkpoint AuctionCreated log")

    raw_count = baseline.get("extension_count")
    if raw_count in (None, ""):
        baseline_count = 0
    else:
        try:
            baseline_count = int(str(raw_count))
        except ValueError as exc:
            raise FullRefreshRequired("auction timeline extension count is malformed") from exc
    if baseline_count < 0:
        raise FullRefreshRequired("auction timeline extension count is negative")

    latest_keys = (
        "latest_extension_end_time_utc",
        "latest_extension_block",
        "latest_extension_tx_hash",
        "latest_extension_log_index",
    )
    if baseline_count:
        if any(baseline.get(key) in (None, "") for key in latest_keys):
            raise FullRefreshRequired("auction timeline extension provenance is incomplete")
        baseline_position = (
            int_value(baseline.get("latest_extension_block"), -1),
            int_value(baseline.get("latest_extension_log_index"), -1),
        )
        if min(baseline_position) < 0:
            raise FullRefreshRequired("auction timeline extension position is malformed")
        if baseline_position[0] > authenticated_checkpoint_block:
            raise FullRefreshRequired("persisted AuctionExtended log is newer than its checkpoint")
        effective_end = str(baseline["latest_extension_end_time_utc"])
    else:
        if any(baseline.get(key) not in (None, "") for key in latest_keys):
            raise FullRefreshRequired("zero-extension timeline has contradictory provenance")
        baseline_position = (-1, -1)
        effective_end = initial_end

    ordered = sorted(
        (dict(row) for row in canonical_rows),
        key=lambda row: (int_value(row.get("block_number"), -1), int_value(row.get("log_index"), -1)),
    )
    positions: set[tuple[int, int]] = set()
    for row in ordered:
        position = (int_value(row.get("block_number"), -1), int_value(row.get("log_index"), -1))
        tx_hash = str(row.get("tx_hash") or "").lower()
        if (
            min(position) < 0
            or position[0] > snapshot_block
            or position in positions
            or not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)
        ):
            raise FullRefreshRequired("AuctionExtended log ordering is malformed or duplicated")
        positions.add(position)

    pre_checkpoint_rows = [
        row for row in ordered if int_value(row.get("block_number"), -1) <= authenticated_checkpoint_block
    ]
    if len(pre_checkpoint_rows) != baseline_count:
        raise FullRefreshRequired("raw extension export and checkpointed timeline count disagree")
    if baseline_count:
        persisted_latest = pre_checkpoint_rows[-1]
        if (
            (int_value(persisted_latest.get("block_number"), -1), int_value(persisted_latest.get("log_index"), -1))
            != baseline_position
            or str(persisted_latest.get("tx_hash") or "").lower()
            != str(baseline.get("latest_extension_tx_hash") or "").lower()
            or str(persisted_latest.get("end_time_utc") or "") != effective_end
        ):
            raise FullRefreshRequired("raw extension export and checkpointed timeline latest event disagree")

    previous_end = parse_utc(initial_end)
    created_block = int_value(created.get("block_number"), -1)
    for row in ordered:
        if created_block >= 0 and int_value(row.get("block_number"), -1) < created_block:
            raise FullRefreshRequired("AuctionExtended log precedes AuctionCreated")
        next_end = parse_utc(row.get("end_time_utc"))
        if next_end is None or previous_end is None or next_end <= previous_end:
            raise FullRefreshRequired("AuctionExtended end time did not increase monotonically")
        previous_end = next_end
        effective_end = str(row["end_time_utc"])

    extension_count = len(ordered)
    latest_row = ordered[-1] if ordered else None
    if latest_row is not None:
        latest_end: Any = latest_row["end_time_utc"]
        latest_block: Any = int_value(latest_row.get("block_number"), -1)
        latest_tx: Any = str(latest_row.get("tx_hash") or "").lower()
        latest_log_index: Any = int_value(latest_row.get("log_index"), -1)
    else:
        latest_end = latest_block = latest_tx = latest_log_index = None

    expected_end = str(expected_end_time_utc or "")
    if expected_end and effective_end != expected_end:
        raise FullRefreshRequired(
            f"effective AuctionExtended end time disagrees with auction(): logs={effective_end} contract={expected_end}"
        )
    return {
        "initial_end_time_utc": initial_end,
        "end_time_utc": effective_end,
        "extension_count": extension_count,
        "latest_extension_end_time_utc": latest_end,
        "latest_extension_block": latest_block,
        "latest_extension_tx_hash": latest_tx,
        "latest_extension_log_index": latest_log_index,
    }


def merge_extension_overlap(
    existing: list[dict[str, Any]],
    fresh: list[dict[str, Any]],
    coverage_ranges: list[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Replace every covered raw extension row with the canonical quorum result."""
    def covered(block_number: int) -> bool:
        return any(start <= block_number <= end for start, end in coverage_ranges)

    for row in fresh:
        if not covered(int_value(row.get("block_number"), -1)):
            raise FullRefreshRequired("fresh AuctionExtended log is outside the verified ranges")
    output = [
        dict(row)
        for row in existing
        if not covered(int_value(row.get("block_number"), -1))
    ]
    output.extend(dict(row) for row in fresh)
    output.sort(key=lambda row: (int_value(row.get("block_number"), -1), int_value(row.get("log_index"), -1)))
    identities: set[tuple[str, int]] = set()
    positions: set[tuple[int, int]] = set()
    for row in output:
        tx_hash = str(row.get("tx_hash") or "").lower()
        log_index = int_value(row.get("log_index"), -1)
        block_number = int_value(row.get("block_number"), -1)
        identity = (tx_hash, log_index)
        position = (block_number, log_index)
        if (
            not re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)
            or min(position) < 0
            or identity in identities
            or position in positions
        ):
            raise FullRefreshRequired("raw AuctionExtended export is malformed or duplicated")
        identities.add(identity)
        positions.add(position)
    return output


def validate_extended_bid_pairs(
    bids: list[dict[str, Any]],
    extensions: list[dict[str, Any]],
    token_ids: set[int],
) -> None:
    bids_by_position: dict[tuple[int, str, int], dict[str, Any]] = {}
    for row in bids:
        extended = int_value(row.get("extended"), -1)
        if extended not in {0, 1}:
            raise FullRefreshRequired("AuctionBid has a non-boolean extension flag")
        token_id = int_value(row.get("token_id"), -1)
        tx_hash = str(row.get("tx_hash") or "").lower()
        if token_id in token_ids:
            block_number = int_value(row.get("block_number"), -1)
            log_index = int_value(row.get("log_index"), -1)
            if not tx_hash or block_number < 0 or log_index < 0:
                raise FullRefreshRequired("AuctionBid is missing its canonical log identity")
            position = (block_number, tx_hash, log_index)
            if position in bids_by_position:
                raise FullRefreshRequired("auction extension transaction has duplicate bid logs")
            bids_by_position[position] = row

    extensions_by_position: dict[tuple[int, str, int], dict[str, Any]] = {}
    for row in extensions:
        token_id = int_value(row.get("token_id"), -1)
        tx_hash = str(row.get("tx_hash") or "").lower()
        block_number = int_value(row.get("block_number"), -1)
        log_index = int_value(row.get("log_index"), -1)
        if token_id not in token_ids or not tx_hash or block_number < 0 or log_index < 0:
            raise FullRefreshRequired("AuctionExtended log is outside the active auction scope")
        position = (block_number, tx_hash, log_index)
        if position in extensions_by_position:
            raise FullRefreshRequired("auction extension transaction has duplicate extension logs")
        extensions_by_position[position] = row

    for position, bid in bids_by_position.items():
        extension = extensions_by_position.get((position[0], position[1], position[2] + 1))
        should_extend = int_value(bid.get("extended")) == 1
        if should_extend != (
            extension is not None
            and int_value(extension.get("token_id"), -1)
            == int_value(bid.get("token_id"), -1)
        ):
            raise FullRefreshRequired("AuctionBid extension flags and AuctionExtended logs disagree")
    for position, extension in extensions_by_position.items():
        bid = bids_by_position.get((position[0], position[1], position[2] - 1))
        if (
            bid is None
            or int_value(bid.get("extended"), -1) != 1
            or int_value(bid.get("token_id"), -1)
            != int_value(extension.get("token_id"), -1)
        ):
            raise FullRefreshRequired("AuctionExtended log does not follow its paired AuctionBid")


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


def high_bid_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the current-auction public contract: high/latest bid first."""
    return sorted(rows, key=bid_sort_key, reverse=True)


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
        amount_usd = numeric_usd_2(amount, eth_usd)
        token_id = int_value(bid.get("token_id"), -1)
        formatted.append(
            {
                "bid_time_utc": bid.get("block_time_utc") or bid.get("bid_time_utc") or "",
                "token_id": token_id,
                "dog": f"Dog #{token_id}",
                "bidder": name,
                "bidder_url": url,
                "bidder_wallet": wallet,
                "bid": f"{format_eth_5(amount)} ETH (${format_usd_product_0(amount, eth_usd)})",
                "bid_eth": numeric_8(amount),
                "bid_usd": amount_usd,
                "bid_usd_at_event": amount_usd,
                "eth_usd_price_live": str(eth_usd),
                "eth_usd_price_at_event": str(eth_usd),
                "eth_usd_price_date_utc": price_date_utc,
                "usd_estimate_source": "current_eth_usd_price",
                "usd_estimate_source_detail": eth_source,
                "usd_estimate_confidence": "live_current",
                "usd_estimate_basis": "current_auction_bid_history_live_eth_usd",
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
    # Active-auction tables are live-priced, while ``fresh_rows`` carries the
    # immutable event-date quote required by recent_bids. Seed any recovered
    # active-only identities first, then let the canonical historical row win
    # when both surfaces describe the same onchain event.
    for row in [*complete_active_history, *fresh_rows]:
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
        target["bid_eth"] = numeric_8(
            Decimal(str(target.get("bid_eth") or 0)) + eth_delta
        )

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
        target["high_bid_eth"] = numeric_8(max(prior_high, fresh_high))

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
            -Decimal(str(row.get("bid_eth") or 0)),
            -int_value(row.get("bids")),
            bid_wallet(row),
        )
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
            "bid_eth": numeric_8(sum((bid_amount(row) for row in day_bids), Decimal(0))),
            "high_bid_eth": numeric_8(
                max((bid_amount(row) for row in day_bids), default=Decimal(0))
            ),
            "settled_eth": numeric_8(
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
        row["winning_eth"] = numeric_8(amount)
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


def unified_by_id_mismatches(records: list[dict[str, Any]]) -> set[int]:
    """Find master records whose per-Dog mirror is absent or crash-stale."""
    mismatches: set[int] = set()
    by_id = ROOT / "archive" / "dogs" / "by-id"
    for record in records:
        if not isinstance(record, dict):
            continue
        token_id = int_value(record.get("dog_id"), -1)
        if token_id < 0:
            continue
        payload = load_json_file(by_id / f"{token_id:03d}.json", {})
        if not isinstance(payload, dict) or payload.get("record") != record:
            mismatches.add(token_id)
    return mismatches


def write_json(name: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    for directory in (GENERATED, PUBLIC_GENERATED):
        atomic_write_text(directory / f"{name}.json", payload)


TABLE_JSON_TYPE_CONTRACTS: dict[str, dict[str, tuple[type, bool]]] = {
    "auction_extensions": {
        "token_id": (int, False), "block_number": (int, False), "log_index": (int, False),
    },
    "auction_daily_activity": {
        "created_auctions": (int, False), "settled_auctions": (int, False),
        "bids": (int, False), "unique_bidders": (int, False),
        "bid_eth": (float, False), "high_bid_eth": (float, False), "settled_eth": (float, False),
    },
    "auction_bidder_leaderboard": {
        "bids": (int, False), "auctions_bid": (int, False), "auction_wins": (int, False),
        "latest_bid_token_id": (int, False), "bid_eth": (float, False),
        "high_bid_eth": (float, False), "winning_eth": (float, False),
    },
    "auction_feed": {
        "amount_eth": (float, False), "amount_usd": (float, True),
        "amount_usd_at_event": (float, True),
    },
    "auction_timeline": {
        "token_id": (int, False), "extension_count": (int, False),
        "latest_extension_block": (int, True), "latest_extension_log_index": (int, True),
        "bids": (int, False), "unique_bidders": (int, False),
        "high_bid_eth": (float, False), "total_bid_eth": (float, False),
        "latest_bid_eth": (float, False), "settled_eth": (float, False),
    },
    "auction_winners": {
        "token_id": (int, False), "rarity_score": (float, True),
        "winning_bid_eth": (float, False), "winning_bid_usd": (float, True),
        "winning_bid_usd_at_settlement": (float, True), "bid_count": (int, False),
        "unique_bidders": (int, False), "block_number": (int, False),
    },
    "recent_auction_winners": {
        "winning_bid_eth": (float, False), "winning_bid_usd": (float, True),
        "winning_bid_usd_at_settlement": (float, True),
    },
    "current_auction": {
        "token_id": (int, False), "rarity_score": (float, True),
        "current_bid_eth": (float, False), "current_bid_usd": (float, False),
        "current_bid_usd_live": (float, False), "seconds_remaining": (int, True),
        "settled": (int, False), "latest_block": (int, False),
    },
    "current_latest_bid": {
        "latest_bid_eth": (float, False), "latest_bid_usd": (float, False),
        "latest_bid_usd_live": (float, False),
    },
    "current_auction_bid_history": {
        "token_id": (int, False), "bid_eth": (float, False), "bid_usd": (float, False),
        "extended": (int, False), "block_number": (int, False), "log_index": (int, False),
    },
    "recent_bids": {
        "token_id": (int, False), "bid_eth": (float, False), "bid_usd": (float, True),
        "bid_usd_at_event": (float, True), "extended": (int, False), "block_number": (int, False),
    },
}


def read_table(name: str) -> tuple[list[str], list[dict[str, Any]]]:
    path = GENERATED / f"{name}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        csv_rows = list(reader)
        columns = list(reader.fieldnames or [])
    json_path = GENERATED / f"{name}.json"
    try:
        json_rows = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FullRefreshRequired(f"{json_path.relative_to(ROOT)} is missing or invalid: {exc}") from exc
    if not isinstance(json_rows, list) or len(json_rows) != len(csv_rows):
        raise FullRefreshRequired(f"generated/{name} CSV/JSON row counts disagree")
    expected_columns = set(columns)
    type_contract = TABLE_JSON_TYPE_CONTRACTS.get(name, {})
    missing_contract_columns = sorted(set(type_contract) - expected_columns)
    if missing_contract_columns:
        raise FullRefreshRequired(
            f"generated/{name} is missing typed column(s): {','.join(missing_contract_columns)}"
        )
    for index, (csv_row, json_row) in enumerate(zip(csv_rows, json_rows, strict=True)):
        if not isinstance(json_row, dict) or set(json_row) != expected_columns:
            raise FullRefreshRequired(
                f"generated/{name} CSV/JSON schema disagrees at row {index}"
            )
        for column in columns:
            json_value = json_row.get(column)
            if isinstance(json_value, (dict, list)):
                raise FullRefreshRequired(
                    f"generated/{name} has a non-scalar table value at row {index} column {column}"
                )
            json_csv_value = "" if json_value is None else str(json_value)
            if json_csv_value != str(csv_row.get(column) or ""):
                raise FullRefreshRequired(
                    f"generated/{name} CSV/JSON values disagree at row {index} column {column}"
                )
        for column, (expected_type, nullable) in type_contract.items():
            value = json_row.get(column)
            if value is None and nullable:
                continue
            if type(value) is not expected_type:
                null_label = " or null" if nullable else ""
                raise FullRefreshRequired(
                    f"generated/{name} row {index} column {column} must be "
                    f"{expected_type.__name__}{null_label}"
                )
    # CSV declares column order while JSON preserves the canonical scalar
    # types. Reusing both prevents bounded refreshes from flipping integers,
    # decimals, and nulls into strings/blanks after a full build.
    return columns, json_rows


def write_table(name: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    projected_rows = [
        {column: row.get(column, "") for column in columns}
        for row in rows
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(projected_rows)
    payload = buffer.getvalue()
    for directory in (GENERATED, PUBLIC_GENERATED):
        atomic_write_text(directory / f"{name}.csv", payload)
    # CSV is the table schema authority. Project JSON through the same columns
    # so bounded and full builds cannot expose different API fields.
    write_json(name, projected_rows)


def ensure_table_column(
    columns: list[str],
    column: str,
    *,
    after: str | None = None,
) -> list[str]:
    """Return a schema containing ``column`` without mutating the input list."""
    if column in columns:
        return list(columns)
    migrated = list(columns)
    if after in migrated:
        migrated.insert(migrated.index(after) + 1, column)
    else:
        migrated.append(column)
    return migrated


def prepare_historical_rarity_table(
    columns: list[str],
    rows: list[dict[str, Any]],
    rarity_universe: dict[int, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Migrate the historical schema and normalize every verified rarity row."""
    migrated_columns = ensure_table_column(
        columns,
        "rarity_score",
        after="rarity",
    )
    # JSON and CSV can be split across a hard crash because they are separate
    # atomic files. Normalize from the independently recomputed universe every
    # run instead of trusting either surface as the rebase signal.
    for row in rows:
        historical_token_id = int_value(row.get("token_id"), -1)
        if historical_token_id in rarity_universe:
            expected = rarity_universe[historical_token_id]
            if (
                rarity_row_requires_rebase(row, expected)
                or str(row.get("traits") or "") != str(expected["traits"])
                or str(row.get("metadata_verification_status") or "")
                != "onchain_token_uri_verified"
            ):
                apply_rarity_fields(row, expected)
                row["search_text"] = " ".join(
                    str(row.get(key, "")) for key in row if key != "search_text"
                ).lower()
    return migrated_columns, rows


def int_value(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def canonical_historical_search_text(row: dict[str, Any]) -> str:
    """Match the full builder's complete scalar search corpus."""
    return " ".join(
        str(value)
        for key, value in row.items()
        if key != "search_text"
        and value is not None
        and not isinstance(value, (list, dict))
        and str(value)
    )


def apply_historical_provenance_defaults(
    row: dict[str, Any],
    archive_source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prefer independent archive provenance, otherwise preserve or fill it."""
    archive_source = archive_source or {}
    archive_confidence = str(archive_source.get("confidence") or "")
    archive_sources_raw = archive_source.get("sources")
    if isinstance(archive_sources_raw, list):
        archive_sources = ",".join(str(value) for value in archive_sources_raw if str(value))
    else:
        archive_sources = str(archive_sources_raw or "")
    if archive_confidence:
        row["confidence"] = archive_confidence
    if archive_sources:
        row["sources"] = archive_sources
    if not row.get("confidence"):
        row["confidence"] = "verified_live_base_logs"
    if not row.get("sources"):
        row["sources"] = "base_logs,dashboard_builder"
    return row


def recompute_historical_report_rows(
    report_rows: list[dict[str, Any]],
    history_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild report aggregates with the same event-time basis as the full builder."""
    report_groups = {"all": history_rows}
    report_groups.update(
        {
            mission: [row for row in history_rows if str(row.get("mission")) == mission]
            for mission in ("1", "2", "3")
        }
    )
    for report in report_rows:
        group = report_groups.get(str(report.get("mission")))
        if group is None:
            continue
        statuses = [str(row.get("status") or "").lower() for row in group]
        created_times = [
            str(row.get("auction_created_time_utc"))
            for row in group
            if row.get("auction_created_time_utc")
        ]
        activity_times = [
            str(value)
            for row in group
            for value in (row.get("settled_time_utc"), row.get("auction_created_time_utc"))
            if value
        ]
        winners = {
            str(row.get("winner_wallet") or row.get("winner"))
            for row in group
            if row.get("winner_wallet") or row.get("winner")
        }
        report.update(
            {
                "dogs": len(group),
                "auctions_or_records": sum(
                    1
                    for row in group
                    if row.get("auction_created_time_utc")
                    or row.get("settled_time_utc")
                    or row.get("amount")
                ),
                "settled": sum(
                    1
                    for status in statuses
                    if status == "settled"
                    or (status.startswith("settled") and "unsettled" not in status)
                ),
                "live_or_unsettled": sum(
                    1
                    for status in statuses
                    if "live" in status
                    or "ongoing" in status
                    or "unsettled" in status
                    or "pending settlement" in status
                    or "created" in status
                ),
                "metadata_only": sum(1 for status in statuses if status == "metadata_only"),
                "bid_count": sum(int_value(row.get("bid_count")) for row in group),
                "unique_winners_or_high_bidders": len(winners),
                "first_auction_utc": min(created_times) if created_times else "",
                "latest_activity_utc": max(activity_times) if activity_times else "",
            }
        )
    return report_rows


def canonical_unified_current_search_text(row: dict[str, Any], token_id: int) -> str:
    """Match the unified builder's live-row search corpus without stale overlay terms."""
    who = row.get("winner_or_high_bidder") if isinstance(row.get("winner_or_high_bidder"), dict) else {}
    amount = row.get("amount") if isinstance(row.get("amount"), dict) else {}
    rarity = row.get("rarity") if isinstance(row.get("rarity"), dict) else {}
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    created = row.get("auction_created") if isinstance(row.get("auction_created"), dict) else {}
    settlement = row.get("settlement") if isinstance(row.get("settlement"), dict) else {}
    traits = row.get("traits") if isinstance(row.get("traits"), list) else []
    parts: list[Any] = [
        f"dog {token_id}",
        f"dog #{token_id}",
        str(token_id),
        "Mission 3",
        "mission 3",
        "Base",
        row.get("status"),
        who.get("wallet"),
        who.get("display"),
        who.get("farcaster_handle"),
        amount.get("native"),
        amount.get("native_symbol"),
        amount.get("raw"),
        amount.get("usd_estimate"),
        amount.get("usd_estimate_display"),
        row.get("activity_time_utc"),
        created.get("tx_hash"),
        settlement.get("tx_hash"),
        rarity.get("display"),
        "generated auction feed",
    ]
    parts.extend(row.get("bid_tx_hashes") or [])
    for trait in traits:
        if isinstance(trait, dict):
            parts.extend((trait.get("display"), trait.get("trait_type"), trait.get("value")))
        else:
            parts.append(trait)
    parts.extend(source.get("sources") if isinstance(source.get("sources"), list) else [])
    return " ".join(str(part) for part in parts if part not in (None, "")).lower()


def canonical_unified_source(
    existing_source: dict[str, Any],
    archive_source: dict[str, Any],
    *,
    settled: bool,
    baseline_exists: bool,
) -> dict[str, Any]:
    """Preserve/recover independent archive attribution for a live overlay."""
    source = dict(existing_source)
    source.setdefault("confidence", "verified")
    source.setdefault(
        "notes",
        "Mission 3 live/current source of truth is Base auction logs and current auction contract state.",
    )
    source.setdefault("raw_confidence", "verified_onchain_generated_tables")
    archive_sources = (
        [str(value) for value in archive_source.get("sources", []) if str(value)]
        if isinstance(archive_source.get("sources"), list)
        else []
    )
    existing_sources = [
        str(value)
        for value in source.get("sources", [])
        if str(value)
    ]
    sources = list(archive_sources)
    for existing in existing_sources:
        if existing == "dashboard_builder" and archive_sources:
            continue
        if existing not in sources:
            sources.append(existing)
    required = [
        "base_logs",
        *([] if baseline_exists else ["dashboard_builder"]),
        "generated_auction_timeline",
        "generated_auction_feed",
        *(["generated_auction_winners"] if settled else []),
    ]
    for value in required:
        if value not in sources:
            sources.append(value)
    source["sources"] = sources
    if archive_source.get("confidence"):
        source["confidence"] = str(archive_source["confidence"])
    return source


def normalize_refresh_status_integer_fields(status: dict[str, Any]) -> dict[str, Any]:
    """Keep the public status API's integer fields stable across build paths."""
    normalized = dict(status)
    for key in REFRESH_STATUS_INTEGER_FIELDS.intersection(normalized):
        value = normalized[key]
        if isinstance(value, bool) or not re.fullmatch(r"(?:0|[1-9][0-9]*)", str(value)):
            raise FullRefreshRequired(f"refresh_status {key} is not a non-negative integer")
        normalized[key] = int(value)
    return normalized


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
    event_price = decimal_or_none(historical_usd.get("eth_usd_price_at_event"))
    if event_price is None or event_price <= 0:
        raise FullRefreshRequired(f"Dog #{token_id} settlement has invalid historical USD provenance")
    return f"{format_eth_5(amount_eth)} ETH (${format_usd_product_0(amount_eth, event_price)})"


def dashboard_settlement_usd_basis(historical_usd: dict[str, Any], event_time: Any) -> str:
    """Match the SQL winner/feed label for exact-day versus nearest-day prices."""
    event_dt = parse_utc(event_time)
    price_raw = str(historical_usd.get("eth_usd_price_date_utc") or "")
    try:
        price_date = date.fromisoformat(price_raw)
    except ValueError as exc:
        raise FullRefreshRequired("settlement has invalid historical USD date provenance") from exc
    if event_dt is None:
        raise FullRefreshRequired("settlement has invalid historical USD date provenance")
    if event_dt.date() == price_date:
        return "settlement_date_eth_usd"
    return "nearest_settlement_date_eth_usd"


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


def canonical_utc_z(value: Any, label: str) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        raise FullRefreshRequired(f"{label} is not a canonical UTC timestamp")
    return parsed.replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def normalize_rarity_attributes(token_id: int, attrs: dict[str, str]) -> dict[str, str]:
    expected_trait_types = set(RARITY_TRAIT_TYPES)
    if set(attrs) != expected_trait_types:
        raise FullRefreshRequired(f"Dog #{token_id} metadata has an invalid rarity trait schema")
    if any(";" in key or ";" in value for key, value in attrs.items()):
        raise FullRefreshRequired(f"Dog #{token_id} metadata contains an unsafe rarity trait delimiter")
    return {trait_type: attrs[trait_type] for trait_type in RARITY_TRAIT_TYPES}


def rarity_attributes_from_metadata(token_id: int, metadata: dict[str, Any]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    raw_attributes = metadata.get("attributes", [])
    if not isinstance(raw_attributes, list):
        raise FullRefreshRequired(f"Dog #{token_id} metadata attributes are not a list")
    for item in raw_attributes:
        if not isinstance(item, dict):
            raise FullRefreshRequired(f"Dog #{token_id} metadata contains a malformed trait")
        trait_type = str(item.get("trait_type") or "").strip()
        value = str(item.get("value") or "").strip()
        if not trait_type or not value:
            continue
        if trait_type in attrs:
            raise FullRefreshRequired(f"Dog #{token_id} metadata repeats rarity trait {trait_type!r}")
        attrs[trait_type] = value
    return normalize_rarity_attributes(token_id, attrs)


def verified_rarity_history_attributes(
    history: list[dict[str, Any]],
) -> dict[int, dict[str, str]]:
    all_attrs: dict[int, dict[str, str]] = {}
    for row in history:
        historical_token_id = int_value(row.get("token_id"), -1)
        if historical_token_id < 0:
            continue
        verification_status = str(row.get("metadata_verification_status") or "").strip()
        raw_traits = str(row.get("traits") or "")
        if verification_status == "onchain_token_uri_unavailable":
            continue
        if verification_status != "onchain_token_uri_verified":
            if raw_traits.strip():
                raise FullRefreshRequired(
                    f"Dog #{historical_token_id} has rarity traits without verified metadata provenance"
                )
            continue
        attributes = traits_from_text(raw_traits)
        if not attributes:
            raise FullRefreshRequired(
                f"verified Dog #{historical_token_id} has no usable rarity traits"
            )
        attributes = normalize_rarity_attributes(historical_token_id, attributes)
        if historical_token_id in all_attrs:
            raise FullRefreshRequired(f"rarity history repeats Dog #{historical_token_id}")
        all_attrs[historical_token_id] = attributes
    return all_attrs


def rarity_token_ids_sha256(token_ids: set[int] | list[int] | tuple[int, ...]) -> str:
    return hashlib.sha256(
        ",".join(str(candidate_id) for candidate_id in sorted(token_ids)).encode("ascii")
    ).hexdigest()


def score_rarity_universe(
    all_attrs: dict[int, dict[str, str]],
    rarity_universe_size: int,
) -> dict[int, dict[str, Any]]:
    if rarity_universe_size <= 0:
        raise FullRefreshRequired("rarity universe must contain at least one Base-existing Dog")
    if len(all_attrs) != rarity_universe_size:
        raise FullRefreshRequired(
            f"rarity universe has {len(all_attrs)} Base-existing Dogs, expected {rarity_universe_size}; "
            "full refresh required"
        )
    counts: Counter[tuple[str, str]] = Counter()
    for attrs in all_attrs.values():
        counts.update(attrs.items())
    scores: dict[int, Decimal] = {}
    for candidate_id, attrs in all_attrs.items():
        scores[candidate_id] = sum(
            (
                Decimal(rarity_universe_size) / Decimal(max(1, counts[(key, value)]))
                for key, value in attrs.items()
            ),
            Decimal(0),
        )
    ranks: dict[int, int] = {}
    previous_score: Decimal | None = None
    competition_rank = 0
    for position, candidate_id in enumerate(
        sorted(scores, key=lambda candidate: (-scores[candidate], candidate)),
        start=1,
    ):
        if previous_score is None or scores[candidate_id] != previous_score:
            competition_rank = position
            previous_score = scores[candidate_id]
        ranks[candidate_id] = competition_rank
    universe: dict[int, dict[str, Any]] = {}
    for candidate_id, attrs in all_attrs.items():
        traits = "; ".join(f"{key}: {value}" for key, value in attrs.items())
        trait_rarity = "; ".join(
            f"{key}: {value} ({counts[(key, value)] * 100 / rarity_universe_size:.1f}%)"
            for key, value in attrs.items()
        )
        universe[candidate_id] = {
            "rarity": f"#{ranks[candidate_id]}/{rarity_universe_size}",
            "rarity_score": round(float(scores[candidate_id]), 6),
            "trait_rarity": trait_rarity,
            "traits": traits,
        }
    return universe


def build_rarity(
    history: list[dict[str, Any]],
    token_id: int,
    new_attrs: dict[str, str],
    rarity_universe_size: int,
) -> tuple[str, float, str, str]:
    universe = build_rarity_universe(history, token_id, new_attrs, rarity_universe_size)
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
    rarity_universe_size: int,
    expected_token_ids_sha256: str | None = None,
) -> dict[int, dict[str, Any]]:
    """Recompute the exact verified Base-existing rarity permutation."""
    all_attrs = verified_rarity_history_attributes(history)
    # The current Dog may already be present after a full rebuild. Replacing its
    # attributes avoids scoring a phantom `max_id + 1` Dog and duplicating a rank.
    all_attrs[token_id] = normalize_rarity_attributes(token_id, new_attrs)
    actual_token_ids_sha256 = rarity_token_ids_sha256(set(all_attrs))
    if expected_token_ids_sha256 is not None and actual_token_ids_sha256 != expected_token_ids_sha256:
        raise FullRefreshRequired("rarity history token IDs do not match the verified Base existence set")
    return score_rarity_universe(all_attrs, rarity_universe_size)


def build_extended_rarity_universe(
    history: list[dict[str, Any]],
    verified_attrs_by_token: dict[int, dict[str, str]],
    minted_token_ids: tuple[int, ...],
    baseline_universe_size: int,
    expected_baseline_token_ids_sha256: str,
) -> dict[int, dict[str, Any]]:
    """Extend an exactly attested cached universe with verified canonical mints."""
    if tuple(sorted(set(minted_token_ids))) != minted_token_ids or any(
        token_id < 0 for token_id in minted_token_ids
    ):
        raise FullRefreshRequired("canonical Dog mint IDs are not unique, sorted non-negative integers")
    all_attrs = verified_rarity_history_attributes(history)
    if len(all_attrs) != baseline_universe_size:
        raise FullRefreshRequired(
            f"cached rarity universe has {len(all_attrs)} Base-existing Dogs, expected "
            f"{baseline_universe_size}; full refresh required"
        )
    if rarity_token_ids_sha256(set(all_attrs)) != expected_baseline_token_ids_sha256:
        raise FullRefreshRequired("cached rarity token IDs do not match the verified Base existence set")
    minted = set(minted_token_ids)
    if not minted.issubset(verified_attrs_by_token):
        missing = sorted(minted.difference(verified_attrs_by_token))
        raise FullRefreshRequired(f"canonical Dog mints are missing verified metadata: {missing}")
    for token_id, attrs in verified_attrs_by_token.items():
        if token_id in minted:
            if token_id in all_attrs:
                raise FullRefreshRequired(f"canonical mint Dog #{token_id} already exists in cached rarity")
        elif token_id not in all_attrs:
            raise FullRefreshRequired(
                f"Dog #{token_id} is not covered by the cached existence set or canonical mint proof"
            )
        all_attrs[token_id] = normalize_rarity_attributes(token_id, attrs)
    return score_rarity_universe(all_attrs, baseline_universe_size + len(minted_token_ids))


def validate_rarity_cache_entry(
    builder: Any,
    token_id: int,
    entry: Any,
    expected_attrs: dict[str, str],
    *,
    observed_at: datetime,
    max_verified_block: int,
    expected_token_uri: str | None = None,
) -> None:
    """Validate one fresh, content-bound metadata-cache record."""
    if not isinstance(entry, dict) or not isinstance(entry.get("metadata"), dict):
        raise FullRefreshRequired(f"fresh metadata cache is missing Dog #{token_id}")
    metadata = entry["metadata"]
    if int_value(metadata.get("token_id"), -1) != token_id:
        raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} has a mismatched token ID")
    for key in ("token_uri_sha256", "content_sha256", "metadata_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get(key) or "")):
            raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} has an invalid {key}")
    metadata_payload = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if hashlib.sha256(metadata_payload.encode("utf-8")).hexdigest() != entry["metadata_sha256"]:
        raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} metadata hash changed")
    verified_block = entry.get("verified_block")
    if (
        isinstance(verified_block, bool)
        or not isinstance(verified_block, int)
        or verified_block < 0
        or verified_block > max_verified_block
    ):
        raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} verified block is invalid")
    if expected_token_uri is not None and entry["token_uri_sha256"] != hashlib.sha256(
        expected_token_uri.encode("utf-8")
    ).hexdigest():
        raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} tokenURI binding changed")
    raw_fetched_at = str(entry.get("fetched_at_utc") or "")
    try:
        fetched_at = datetime.fromisoformat(raw_fetched_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise FullRefreshRequired(
            f"fresh metadata cache Dog #{token_id} has a malformed observation time"
        ) from exc
    if fetched_at.tzinfo is None:
        raise FullRefreshRequired(
            f"fresh metadata cache Dog #{token_id} has a timezone-less observation time"
        )
    observed_at_utc = observed_at.astimezone(timezone.utc)
    age_seconds = (observed_at_utc - fetched_at.astimezone(timezone.utc)).total_seconds()
    try:
        max_age_seconds = int(builder.DOG_METADATA_CACHE_MAX_AGE_SECONDS)
    except (TypeError, ValueError, AttributeError) as exc:
        raise FullRefreshRequired("Dog metadata cache freshness limit is invalid") from exc
    if max_age_seconds <= 0:
        raise FullRefreshRequired("Dog metadata cache freshness limit is invalid")
    if age_seconds < 0:
        raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} is future-dated")
    if age_seconds > max_age_seconds:
        raise FullRefreshRequired(f"fresh metadata cache Dog #{token_id} is expired")
    if rarity_attributes_from_metadata(token_id, metadata) != expected_attrs:
        raise FullRefreshRequired(
            f"fresh metadata cache Dog #{token_id} traits differ from historical rarity"
        )


def validate_fresh_rarity_cache(
    builder: Any,
    history: list[dict[str, Any]],
    baseline_universe_size: int,
    expected_token_ids_sha256: str,
    max_verified_block: int,
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Require one unexpired, trait-identical cache record per baseline rarity Dog."""
    historical_attrs = verified_rarity_history_attributes(history)
    if len(historical_attrs) != baseline_universe_size:
        raise FullRefreshRequired(
            "cached historical rarity rows do not cover the attested Base-existing universe"
        )
    if rarity_token_ids_sha256(set(historical_attrs)) != expected_token_ids_sha256:
        raise FullRefreshRequired(
            "cached historical rarity token IDs differ from the attested Base existence set"
        )
    reference_time = observed_at or now_utc()
    try:
        cache = builder.load_dog_cache()
    except Exception as exc:  # noqa: BLE001
        raise FullRefreshRequired(f"cannot load the fresh Dog metadata cache: {exc}") from exc
    if not isinstance(cache, dict):
        raise FullRefreshRequired("fresh Dog metadata cache is not an object")
    expected_cache_keys = {str(token_id) for token_id in historical_attrs}
    if not expected_cache_keys.issubset(cache):
        missing = sorted(expected_cache_keys.difference(cache), key=int)
        raise FullRefreshRequired(
            "fresh Dog metadata cache does not cover the rarity universe: "
            f"missing={missing[:10]}"
        )
    projected_cache = {key: cache[key] for key in expected_cache_keys}
    for token_id, expected_attrs in historical_attrs.items():
        validate_rarity_cache_entry(
            builder,
            token_id,
            projected_cache[str(token_id)],
            expected_attrs,
            observed_at=reference_time,
            max_verified_block=max_verified_block,
        )
    return projected_cache


def extend_rarity_aggregate_metrics(
    metrics_rows: list[dict[str, Any]],
    minted_token_ids: tuple[int, ...],
    verified_token_ids: set[int],
) -> dict[str, str]:
    """Return aggregate metric deltas for metadata-verified canonical mints."""
    if not minted_token_ids:
        return {}
    metrics = {
        str(row.get("metric")): str(row.get("value", ""))
        for row in metrics_rows
        if isinstance(row, dict)
    }

    def count(key: str) -> int:
        raw = metrics.get(key, "")
        if not raw.isdigit():
            raise FullRefreshRequired(f"baseline metadata metric {key} is not a non-negative integer")
        return int(raw)

    minted = set(minted_token_ids)
    baseline_ids = verified_token_ids.difference(minted)
    if len(minted) != len(minted_token_ids) or not minted.issubset(verified_token_ids):
        raise FullRefreshRequired("canonical Dog mint metric extension has invalid verified token IDs")
    baseline_existing = count("dog_base_existing_count")
    if len(baseline_ids) != baseline_existing:
        raise FullRefreshRequired("cached Base existence count differs from the verified rarity universe")
    if rarity_token_ids_sha256(baseline_ids) != metrics.get("dog_base_existing_token_ids_sha256"):
        raise FullRefreshRequired("cached Base existence hash differs from the verified rarity universe")
    delta = len(minted_token_ids)
    updates = {
        "dog_token_uri_present_count": str(count("dog_token_uri_present_count") + delta),
        "dog_base_existing_count": str(baseline_existing + delta),
        "dog_base_existing_token_ids_sha256": rarity_token_ids_sha256(verified_token_ids),
        "dog_metadata_onchain_verified_count": str(
            count("dog_metadata_onchain_verified_count") + delta
        ),
        "dog_metadata_content_observed_count": str(
            count("dog_metadata_content_observed_count") + delta
        ),
        "dog_rarity_universe_count": str(count("dog_rarity_universe_count") + delta),
    }
    return updates


def verify_rarity_universe_continuity(
    builder: Any,
    metrics_rows: list[dict[str, Any]],
    *,
    latest_block: int,
    latest_block_hash: str,
    latest_dog_code_sha256: str,
    latest_total_supply: int,
) -> dict[str, Any]:
    """Prove that a full rarity attestation is still valid at a newer block.

    Historical claims do not advance this collection's nonstandard
    ``totalSupply()`` counter, and token URIs can be changed independently of
    minting. The fast path may therefore reuse the full-set hashes only after
    independent log providers prove that the only existence changes are a
    contiguous canonical mint range. Burns, metadata updates, gaps, and every
    malformed or mismatched event force the exact full builder instead of
    publishing a stale rank under a fresh snapshot.
    """

    metrics = {
        str(row.get("metric")): str(row.get("value", ""))
        for row in metrics_rows
        if isinstance(row, dict)
    }
    raw_baseline_supply = metrics.get("dog_total_supply", "")
    if not raw_baseline_supply.isdigit():
        raise FullRefreshRequired("baseline rarity metric dog_total_supply is not a non-negative integer")
    baseline_total_supply = int(raw_baseline_supply)
    if latest_total_supply < baseline_total_supply:
        raise FullRefreshRequired("Dog total supply decreased after the rarity attestation")
    expected_minted_token_ids = tuple(range(baseline_total_supply, latest_total_supply))

    def positive_block(key: str) -> int:
        raw = metrics.get(key, "")
        if not raw.isdigit() or int(raw) <= 0:
            raise FullRefreshRequired(f"baseline rarity metric {key} is not a positive block")
        return int(raw)

    attested_block = positive_block("dog_rarity_attested_block")
    continuity_through = positive_block("dog_rarity_continuity_through_block")
    baseline_latest_block = positive_block("latest_block")
    attested_hash = metrics.get("dog_rarity_attested_block_hash", "").lower()
    continuity_hash = metrics.get("dog_rarity_continuity_through_block_hash", "").lower()
    baseline_snapshot_hash = metrics.get("snapshot_block_hash", "").lower()
    latest_hash = str(latest_block_hash or "").lower()
    for label, value in (
        ("attested block hash", attested_hash),
        ("continuity block hash", continuity_hash),
        ("baseline snapshot block hash", baseline_snapshot_hash),
        ("latest block hash", latest_hash),
    ):
        if not re.fullmatch(r"0x[0-9a-f]{64}", value):
            raise FullRefreshRequired(f"baseline rarity {label} is invalid")
    if continuity_through > latest_block:
        raise TransientSnapshotLag(
            "verified snapshot is behind the persisted rarity checkpoint; preserving newer artifacts"
        )
    if attested_block > continuity_through:
        raise FullRefreshRequired("baseline rarity continuity block range is invalid")
    if continuity_through != baseline_latest_block or continuity_hash != baseline_snapshot_hash:
        raise FullRefreshRequired("baseline rarity continuity checkpoint differs from the persisted snapshot")
    if metrics.get("dog_nft_code_sha256") != latest_dog_code_sha256:
        raise FullRefreshRequired("Dog NFT bytecode changed after the rarity attestation")

    continuity_status = metrics.get("dog_rarity_continuity_verification_status", "")
    if continuity_status == FULL_RARITY_CONTINUITY_STATUS:
        if continuity_through != attested_block or continuity_hash != attested_hash:
            raise FullRefreshRequired("full rarity attestation checkpoint is internally inconsistent")
        if metrics.get("dog_token_uri_verification_status") != FULL_TOKEN_URI_VERIFICATION_STATUS:
            raise FullRefreshRequired("full rarity attestation tokenURI status is inconsistent")
        if metrics.get("dog_base_existence_verification_status") != FULL_EXISTENCE_VERIFICATION_STATUS:
            raise FullRefreshRequired("full rarity attestation existence status is inconsistent")
    elif continuity_status in {
        INCREMENTAL_RARITY_CONTINUITY_STATUS,
        EXTENDED_RARITY_CONTINUITY_STATUS,
    }:
        if metrics.get("dog_token_uri_verification_status") != CONTINUITY_TOKEN_URI_VERIFICATION_STATUS:
            raise FullRefreshRequired("incremental rarity continuity tokenURI status is inconsistent")
        if metrics.get("dog_base_existence_verification_status") != CONTINUITY_EXISTENCE_VERIFICATION_STATUS:
            raise FullRefreshRequired("incremental rarity continuity existence status is inconsistent")
    else:
        raise FullRefreshRequired("baseline rarity continuity verification status is unsupported")

    # Re-prove the most recent continuity checkpoint against the currently
    # qualified log quorum. Its block hash commits to every prior checkpoint,
    # including the full-attestation anchor, so a reorg cannot silently carry
    # stale no-mutation evidence forward. Starting the next scan immediately
    # after this checkpoint keeps every fast refresh bounded to only new blocks.
    if continuity_through == latest_block:
        observed_continuity_hash = latest_hash
    else:
        block_data, _agreeing_urls = builder.rpc_quorum(
            "eth_getBlockByNumber",
            [hex(continuity_through), False],
            urls=builder.VERIFIED_LOG_URLS,
            min_agreement=builder.RPC_QUORUM_SIZE,
            timeout=builder.LOG_RPC_TIMEOUT,
        )
        if not isinstance(block_data, dict):
            raise FullRefreshRequired("rarity continuity checkpoint quorum returned a non-object")
        try:
            observed_number = int(str(block_data.get("number") or ""), 16)
        except (TypeError, ValueError) as exc:
            raise FullRefreshRequired("rarity continuity checkpoint quorum returned a malformed number") from exc
        observed_continuity_hash = str(block_data.get("hash") or "").lower()
        if observed_number != continuity_through:
            raise FullRefreshRequired("rarity continuity checkpoint quorum returned the wrong block")
    if observed_continuity_hash != continuity_hash:
        raise FullRefreshRequired("rarity continuity checkpoint hash changed; full refresh required")

    # Use direct quorum calls for every chunk instead of the general history
    # cache, whose deliberately cold prefix may be served by one archive
    # provider on very large ranges. Rarity continuity must never inherit that
    # optimization: an empty result is security-critical evidence here. Parse
    # each chunk before requesting the next one so a burn, metadata mutation,
    # or malformed response fails immediately instead of extending an outage.
    allowed_topics = set(RARITY_MUTATION_TOPICS)
    transfer_topic = RARITY_MUTATION_TOPICS[0]
    dog_address = str(builder.DEGEN_DOGS).lower()
    auction_address = str(builder.AUCTION_HOUSE).lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", dog_address) or not re.fullmatch(
        r"0x[0-9a-f]{40}", auction_address
    ):
        raise FullRefreshRequired("rarity continuity contract configuration is malformed")
    auction_address_topic = "0x" + "0" * 24 + auction_address[2:]
    address_topic_pattern = re.compile(r"0x0{24}[0-9a-f]{40}")
    quantity_pattern = re.compile(r"0x(?:0|[1-9a-f][0-9a-f]*)")
    observed_minted_token_ids: list[int] = []
    seen_log_identities: set[tuple[str, str, int]] = set()
    observed_block_hashes: dict[int, str] = {}
    previous_log_position: tuple[int, int, int] | None = None

    def rpc_quantity(row: dict[str, Any], key: str) -> int:
        raw = str(row.get(key) or "").lower()
        if not quantity_pattern.fullmatch(raw):
            raise FullRefreshRequired(f"Dog rarity continuity log has malformed {key}")
        return int(raw, 16)

    start_block = continuity_through + 1
    chunk_size = max(
        1,
        min(
            int(builder.LOG_QUORUM_MAX_BLOCKS),
            int(getattr(builder, "VERIFIED_LOG_MAX_BLOCKS", builder.LOG_QUORUM_MAX_BLOCKS)),
        ),
    )
    while start_block <= latest_block:
        end_block = min(latest_block, start_block + chunk_size - 1)
        try:
            result, _agreeing_urls = builder.rpc_quorum(
                "eth_getLogs",
                [builder.log_filter(builder.DEGEN_DOGS, list(RARITY_MUTATION_TOPICS), start_block, end_block)],
                urls=builder.VERIFIED_LOG_URLS,
                min_agreement=builder.RPC_QUORUM_SIZE,
                timeout=builder.LOG_RPC_TIMEOUT,
            )
        except builder.RpcLogRangeLimit:
            # Only a typed, explicit provider range/response-size rejection is
            # safe to amplify into smaller queries. Transport and quorum
            # failures propagate unchanged so an outage cannot create a burst.
            attempted_span = end_block - start_block + 1
            if attempted_span <= 1:
                raise
            chunk_size = max(1, attempted_span // 2)
            continue
        if not isinstance(result, list):
            raise FullRefreshRequired("Dog rarity continuity log quorum returned a non-list")
        for row in result:
            if not isinstance(row, dict):
                raise FullRefreshRequired("Dog rarity continuity log is not an object")
            if str(row.get("address") or "").lower() != dog_address:
                raise FullRefreshRequired("Dog rarity continuity log has the wrong contract address")
            if type(row.get("removed")) is not bool or row["removed"] is not False:
                raise FullRefreshRequired("Dog rarity continuity log has a removed or malformed flag")
            block_number = rpc_quantity(row, "blockNumber")
            transaction_index = rpc_quantity(row, "transactionIndex")
            log_index = rpc_quantity(row, "logIndex")
            if not start_block <= block_number <= end_block:
                raise FullRefreshRequired("Dog rarity continuity log is outside its requested block range")
            block_hash = str(row.get("blockHash") or "").lower()
            transaction_hash = str(row.get("transactionHash") or "").lower()
            if not re.fullmatch(r"0x[0-9a-f]{64}", block_hash) or not re.fullmatch(
                r"0x[0-9a-f]{64}", transaction_hash
            ):
                raise FullRefreshRequired("Dog rarity continuity log has a malformed block or transaction hash")
            known_block_hash = observed_block_hashes.setdefault(block_number, block_hash)
            if known_block_hash != block_hash:
                raise FullRefreshRequired("Dog rarity continuity logs disagree on a block hash")
            identity = (block_hash, transaction_hash, log_index)
            if identity in seen_log_identities:
                raise FullRefreshRequired("Dog rarity continuity log is duplicated")
            seen_log_identities.add(identity)
            position = (block_number, transaction_index, log_index)
            if previous_log_position is not None and position <= previous_log_position:
                raise FullRefreshRequired("Dog rarity continuity logs are not in canonical chain order")
            previous_log_position = position
            topics = row.get("topics")
            if not isinstance(topics, list) or not topics:
                raise FullRefreshRequired("Dog rarity continuity log has malformed topics")
            normalized_topics = [str(topic).lower() for topic in topics]
            if any(not re.fullmatch(r"0x[0-9a-f]{64}", topic) for topic in normalized_topics):
                raise FullRefreshRequired("Dog rarity continuity log has a malformed topic")
            event_topic = normalized_topics[0]
            if event_topic not in allowed_topics:
                raise FullRefreshRequired("Dog rarity continuity scan returned an unexpected event")
            if event_topic != transfer_topic:
                raise FullRefreshRequired("Dog token URI metadata changed after the rarity attestation")
            if len(normalized_topics) != 4:
                raise FullRefreshRequired("Dog Transfer continuity event has malformed indexed fields")
            from_topic, to_topic, token_topic = normalized_topics[1:]
            if not address_topic_pattern.fullmatch(from_topic) or not address_topic_pattern.fullmatch(
                to_topic
            ):
                raise FullRefreshRequired("Dog Transfer continuity event has malformed address topics")
            if str(row.get("data") or "").lower() != "0x":
                raise FullRefreshRequired("Dog Transfer continuity event has unexpected data")
            transfer_token_id = int(token_topic, 16)
            if transfer_token_id >= latest_total_supply:
                raise FullRefreshRequired("Dog Transfer continuity event token ID exceeds total supply")
            from_zero = from_topic == ZERO_ADDRESS_TOPIC
            to_zero = to_topic == ZERO_ADDRESS_TOPIC
            if from_zero and to_zero:
                raise FullRefreshRequired("Dog Transfer continuity event has zero sender and recipient")
            if to_zero:
                raise FullRefreshRequired("Dog burn changed the rarity existence set")
            if from_zero:
                if to_topic != auction_address_topic:
                    raise FullRefreshRequired("Dog mint recipient is not the canonical auction house")
                observed_minted_token_ids.append(transfer_token_id)
        start_block = end_block + 1

    observed_mints = tuple(observed_minted_token_ids)
    if observed_mints != expected_minted_token_ids:
        raise FullRefreshRequired(
            "Dog canonical mint continuity is gapped or mismatched: "
            f"observed={list(observed_mints)} expected={list(expected_minted_token_ids)}"
        )
    prior_extension_mints: tuple[int, ...] = ()
    if continuity_status == EXTENDED_RARITY_CONTINUITY_STATUS:
        raw_prior_ids = metrics.get("dog_rarity_extension_mint_token_ids", "")
        raw_prior_count = metrics.get("dog_rarity_extension_mint_count", "")
        raw_prior_hash = metrics.get("dog_rarity_extension_mint_token_ids_sha256", "")
        if not raw_prior_count.isdigit() or not re.fullmatch(
            r"(?:0|[1-9][0-9]*)(?:,(?:0|[1-9][0-9]*))*",
            raw_prior_ids,
        ):
            raise FullRefreshRequired("baseline rarity mint-extension provenance is missing")
        try:
            prior_extension_mints = tuple(int(value) for value in raw_prior_ids.split(","))
        except ValueError as exc:
            raise FullRefreshRequired("baseline rarity mint-extension token IDs are malformed") from exc
        prior_extension_count = int(raw_prior_count)
        if len(prior_extension_mints) != prior_extension_count:
            raise FullRefreshRequired("baseline rarity mint-extension provenance is inconsistent")
        expected_prior_extension_mints = tuple(
            range(
                baseline_total_supply - prior_extension_count,
                baseline_total_supply,
            )
        )
        if (
            tuple(sorted(set(prior_extension_mints))) != prior_extension_mints
            or any(token_id < 0 or token_id >= baseline_total_supply for token_id in prior_extension_mints)
            or prior_extension_mints != expected_prior_extension_mints
            or rarity_token_ids_sha256(prior_extension_mints) != raw_prior_hash
        ):
            raise FullRefreshRequired("baseline rarity mint-extension provenance is inconsistent")
    elif any(
        metrics.get(key, "")
        for key in (
            "dog_rarity_extension_mint_count",
            "dog_rarity_extension_mint_token_ids",
            "dog_rarity_extension_mint_token_ids_sha256",
        )
    ):
        raise FullRefreshRequired("baseline rarity mint-extension provenance contradicts its status")
    cumulative_extension_mints = (*prior_extension_mints, *observed_mints)
    output_continuity_status = (
        EXTENDED_RARITY_CONTINUITY_STATUS
        if cumulative_extension_mints
        else INCREMENTAL_RARITY_CONTINUITY_STATUS
    )
    continuity_metric_updates = {
        "dog_token_uri_verification_status": CONTINUITY_TOKEN_URI_VERIFICATION_STATUS,
        "dog_base_existence_verification_status": CONTINUITY_EXISTENCE_VERIFICATION_STATUS,
        "dog_rarity_attested_block": str(attested_block),
        "dog_rarity_attested_block_hash": attested_hash,
        "dog_rarity_continuity_through_block": str(latest_block),
        "dog_rarity_continuity_through_block_hash": latest_hash,
        "dog_rarity_continuity_verification_status": output_continuity_status,
    }
    if cumulative_extension_mints:
        continuity_metric_updates.update(
            {
                "dog_rarity_extension_mint_count": str(len(cumulative_extension_mints)),
                "dog_rarity_extension_mint_token_ids": ",".join(
                    str(token_id) for token_id in cumulative_extension_mints
                ),
                "dog_rarity_extension_mint_token_ids_sha256": rarity_token_ids_sha256(
                    cumulative_extension_mints
                ),
            }
        )
    return {
        "metrics": continuity_metric_updates,
        "minted_token_ids": observed_mints,
        "baseline_total_supply": baseline_total_supply,
    }


def baseline_rarity_universe_size(metrics_rows: list[dict[str, Any]], total_supply: int) -> int:
    """Validate the full-build envelope and return its Base-existing rarity size."""
    metrics = {
        str(row.get("metric")): str(row.get("value", ""))
        for row in metrics_rows
        if isinstance(row, dict)
    }
    if metrics.get("dog_token_uri_verification_status") not in {
        FULL_TOKEN_URI_VERIFICATION_STATUS,
        CONTINUITY_TOKEN_URI_VERIFICATION_STATUS,
    }:
        raise FullRefreshRequired("baseline tokenURI outcomes are not hash-pinned and cross-provider verified")
    if metrics.get("dog_base_existence_verification_status") not in {
        FULL_EXISTENCE_VERIFICATION_STATUS,
        CONTINUITY_EXISTENCE_VERIFICATION_STATUS,
    }:
        raise FullRefreshRequired("baseline Base existence/tokenURI parity is not cross-provider verified")

    def count(key: str) -> int:
        raw = metrics.get(key, "")
        if not raw.isdigit():
            raise FullRefreshRequired(f"baseline metadata metric {key} is not a non-negative integer")
        return int(raw)

    baseline_supply = count("dog_total_supply")
    id_ceiling = count("dog_id_ceiling")
    token_present = count("dog_token_uri_present_count")
    token_unavailable = count("dog_token_uri_unavailable_count")
    base_existing = count("dog_base_existing_count")
    base_unclaimed = count("dog_base_unclaimed_count")
    metadata_verified = count("dog_metadata_onchain_verified_count")
    metadata_unavailable = count("dog_metadata_unavailable_count")
    metadata_content_observed = count("dog_metadata_content_observed_count")
    if baseline_supply != total_supply:
        raise FullRefreshRequired(
            f"baseline metadata covers supply {baseline_supply}, but onchain total supply is {total_supply}"
        )
    if id_ceiling != baseline_supply:
        raise FullRefreshRequired("baseline Dog ID ceiling contradicts the legacy total-supply metric")
    if base_existing != token_present or base_unclaimed != token_unavailable:
        raise FullRefreshRequired("baseline Base existence counts contradict tokenURI outcomes")
    for key in ("dog_base_existing_token_ids_sha256", "dog_base_unclaimed_token_ids_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", metrics.get(key, "")):
            raise FullRefreshRequired(f"baseline Base existence metric {key} is invalid")
    if token_present + token_unavailable != total_supply:
        raise FullRefreshRequired("baseline tokenURI counts do not equal onchain total supply")
    if metadata_verified + metadata_unavailable != total_supply:
        raise FullRefreshRequired("baseline metadata counts do not equal onchain total supply")
    if metadata_content_observed != metadata_verified:
        raise FullRefreshRequired("baseline observed metadata content count contradicts verified tokenURIs")
    if (
        metrics.get("dog_metadata_content_verification_status")
        != "verified_token_uri_offchain_content_hash_observed"
    ):
        raise FullRefreshRequired("baseline metadata content verification class is missing or unsupported")
    if metadata_unavailable < token_unavailable:
        raise FullRefreshRequired("baseline metadata counts hide tokenURI-unavailable Dogs")
    expected_status = (
        "complete_onchain_token_uri_verified"
        if metadata_unavailable == 0
        else "partial_onchain_token_uri_unavailable"
        if metadata_unavailable == token_unavailable
        else "incomplete_metadata_unavailable"
    )
    if metrics.get("dog_metadata_verification_status") != expected_status:
        raise FullRefreshRequired("baseline metadata status contradicts its verified aggregate counts")
    rarity_universe = count("dog_rarity_universe_count")
    rarity_excluded = count("dog_rarity_excluded_nonexistent_count")
    rarity_incomplete = count("dog_rarity_incomplete_metadata_count")
    if rarity_universe != metadata_verified:
        raise FullRefreshRequired("baseline rarity universe does not equal verified Base metadata count")
    if rarity_excluded != token_unavailable:
        raise FullRefreshRequired("baseline rarity exclusions do not equal nonexistent Base token count")
    if rarity_incomplete != metadata_unavailable - token_unavailable:
        raise FullRefreshRequired("baseline rarity incomplete count contradicts metadata outcomes")
    expected_rarity_status = (
        "complete_verified_existing_token_universe"
        if rarity_universe > 0 and rarity_incomplete == 0
        else "unavailable_no_verified_existing_tokens"
        if rarity_universe == 0
        else "incomplete_existing_token_metadata"
    )
    if metrics.get("dog_rarity_verification_status") != expected_rarity_status:
        raise FullRefreshRequired("baseline rarity status contradicts its aggregate counts")
    if metrics.get("dog_rarity_scope") != "base_existing":
        raise FullRefreshRequired("baseline rarity scope is missing or unsupported")
    if metrics.get("dog_rarity_score_method") != "sum_existing_token_count_divided_by_trait_frequency_v1":
        raise FullRefreshRequired("baseline rarity score method is missing or unsupported")
    if metrics.get("dog_rarity_tie_policy") != "competition_rank_equal_scores_share_rank":
        raise FullRefreshRequired("baseline rarity tie policy is missing or unsupported")
    if metrics.get("dog_rarity_trait_schema") != "|".join(RARITY_TRAIT_TYPES):
        raise FullRefreshRequired("baseline rarity trait schema is missing or unsupported")
    return rarity_universe if rarity_incomplete == 0 else 0


def apply_rarity_fields(row: dict[str, Any], rarity_row: dict[str, Any]) -> None:
    row.update(
        {
            "rarity": rarity_row["rarity"],
            "rarity_score": rarity_row["rarity_score"],
            "trait_rarity": rarity_row["trait_rarity"],
            "traits": rarity_row["traits"],
            "metadata_verification_status": "onchain_token_uri_verified",
        }
    )


def normalize_table_rarity_rows(
    rows: list[dict[str, Any]],
    rarity_universe: dict[int, dict[str, Any]],
    *,
    token_from_dog_label: bool = False,
) -> list[dict[str, Any]]:
    """Normalize rarity rows independently of an earlier surface write."""
    for row in rows:
        if token_from_dog_label:
            match = re.search(r"(\d+)", str(row.get("dog") or ""))
            token_id = int(match.group(1)) if match else -1
        else:
            token_id = int_value(row.get("token_id"), -1)
        if token_id in rarity_universe:
            apply_rarity_fields(row, rarity_universe[token_id])
    return rows


def apply_unified_rarity_fields(
    row: dict[str, Any],
    rarity_row: dict[str, Any],
) -> bool:
    old_rarity = row.get("rarity") if isinstance(row.get("rarity"), dict) else {}
    old_traits = row.get("traits") if isinstance(row.get("traits"), list) else []
    structured_traits = unified_trait_items(str(rarity_row["trait_rarity"]))
    expected_rarity = {
        "display": rarity_row["rarity"],
        "rank": int(str(rarity_row["rarity"]).split("/", 1)[0].lstrip("#")),
        "total": int(str(rarity_row["rarity"]).split("/", 1)[1]),
        "scope": "base_existing",
    }
    # The canonical full builder already embeds the authoritative structured
    # fields in its own search-text order. Preserve that byte-for-byte so a
    # no-change bounded refresh cannot create master/by-id divergence.
    if old_rarity == expected_rarity and old_traits == structured_traits:
        return False
    search_text = str(row.get("search_text") or "")
    stale_terms = [str(old_rarity.get("display") or "")]
    stale_terms.extend(str(item.get("display") or "") for item in old_traits if isinstance(item, dict))
    for term in stale_terms:
        if term:
            search_text = re.sub(re.escape(term), " ", search_text, flags=re.IGNORECASE)
    row["rarity"] = expected_rarity
    row["traits"] = structured_traits
    fresh_terms = [str(rarity_row["rarity"]), *(str(item.get("display") or "") for item in structured_traits)]
    row["search_text"] = " ".join([search_text, *fresh_terms]).lower().split()
    row["search_text"] = " ".join(row["search_text"])
    return True


def decimal_or_none(value: Any) -> Decimal | None:
    raw = str(value or "").replace(",", "").strip()
    if not raw:
        return None
    try:
        parsed = Decimal(raw)
    except Exception:  # noqa: BLE001
        return None
    return parsed if parsed.is_finite() else None


def rarity_row_requires_rebase(row: dict[str, Any], rarity_row: dict[str, Any]) -> bool:
    return (
        str(row.get("rarity") or "") != str(rarity_row["rarity"])
        or str(row.get("trait_rarity") or "") != str(rarity_row["trait_rarity"])
        or decimal_or_none(row.get("rarity_score"))
        != decimal_or_none(rarity_row.get("rarity_score"))
    )


def historical_usd_candidate(row: Any) -> dict[str, Any] | None:
    """Normalize complete, non-live event-price provenance from an archive surface."""
    if not isinstance(row, dict):
        return None
    if isinstance(row.get("record"), dict):
        row = row["record"]
    amount = row.get("amount") if isinstance(row.get("amount"), dict) else row
    event_amount = decimal_or_none(
        amount.get("amount_usd_at_event")
        or amount.get("bid_usd_at_event")
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


def dashboard_bid_usd_basis(price_date_utc: Any, event_time: Any) -> str:
    event_dt = parse_utc(event_time)
    try:
        price_date = date.fromisoformat(str(price_date_utc or ""))
    except ValueError as exc:
        raise FullRefreshRequired("bid has invalid historical USD date provenance") from exc
    if event_dt is None:
        raise FullRefreshRequired("bid has invalid historical USD event time")
    return "bid_date_eth_usd" if event_dt.date() == price_date else "nearest_bid_date_eth_usd"


def format_historical_bid_rows(
    rows: list[dict[str, Any]],
    existing_rows: list[dict[str, Any]],
    profiles: dict[str, tuple[str, str]],
) -> list[dict[str, Any]]:
    """Format recent-bid exports with immutable event-date price provenance."""
    existing_by_identity = {bid_public_identity(row): row for row in existing_rows}
    output: list[dict[str, Any]] = []
    for bid in rows:
        amount = bid_amount(bid)
        event_time = bid.get("block_time_utc") or bid.get("bid_time_utc") or ""
        quote: dict[str, Any] | None = None
        prior = existing_by_identity.get(bid_public_identity(bid))
        if prior is not None:
            candidate = historical_usd_candidate(prior)
            if candidate is not None:
                event_price = decimal_or_none(candidate.get("eth_usd_price_at_event"))
                event_amount = decimal_or_none(candidate.get("amount_usd_at_event"))
                price_dt = parse_utc(candidate.get("eth_usd_price_date_utc"))
                event_dt = parse_utc(event_time)
                if (
                    event_price is not None
                    and event_price > 0
                    and event_amount is not None
                    and event_amount == Decimal(str(numeric_usd_2(amount, event_price)))
                    and price_dt is not None
                    and event_dt is not None
                    and abs((price_dt - event_dt).total_seconds()) <= 3 * 86400
                ):
                    quote = candidate
        if quote is None:
            quote = local_historical_usd(amount, event_time)
        if quote is None:
            raise FullRefreshRequired(
                f"Dog #{int_value(bid.get('token_id'), -1)} bid has no canonical historical ETH/USD provenance"
            )
        event_price = decimal_or_none(quote.get("eth_usd_price_at_event"))
        if event_price is None or event_price <= 0:
            raise FullRefreshRequired("bid has invalid historical ETH/USD price provenance")
        token_id = int_value(bid.get("token_id"), -1)
        wallet = bid_wallet(bid)
        bidder, bidder_url = display_for(wallet, profiles)
        amount_usd = numeric_usd_2(amount, event_price)
        output.append(
            {
                "bid_time_utc": event_time,
                "token_id": token_id,
                "bidder": bidder,
                "bidder_url": bidder_url,
                "bidder_wallet": wallet,
                "bid": f"{format_eth_5(amount)} ETH (${format_usd_product_0(amount, event_price)})",
                "bid_eth": numeric_8(amount),
                "bid_usd": amount_usd,
                "bid_usd_at_event": amount_usd,
                "eth_usd_price_at_event": str(quote["eth_usd_price_at_event"]),
                "eth_usd_price_date_utc": str(quote["eth_usd_price_date_utc"]),
                "usd_estimate_source": str(quote["usd_estimate_source"]),
                "usd_estimate_source_detail": str(quote.get("usd_estimate_source_detail") or ""),
                "usd_estimate_confidence": str(quote.get("usd_estimate_confidence") or "medium"),
                "usd_estimate_basis": dashboard_bid_usd_basis(
                    quote["eth_usd_price_date_utc"],
                    event_time,
                ),
                "extended": int_value(bid.get("extended")),
                "block_number": bid_block(bid),
                "tx_hash": bid_tx_hash(bid),
            }
        )
    return output


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
    event_price = decimal_or_none(historical_usd.get("eth_usd_price_at_event"))
    settlement_amount = decimal_or_none(settlement.get("amount_eth"))
    if event_amount is None or event_price is None or event_price <= 0 or settlement_amount is None:
        raise FullRefreshRequired("settled winner is missing canonical event USD")
    row.update(
        {
            "winning_bid_usd": numeric_usd_2(settlement_amount, event_price),
            "winning_bid_usd_at_settlement": numeric_usd_2(settlement_amount, event_price),
            "eth_usd_price_at_event": historical_usd["eth_usd_price_at_event"],
            "eth_usd_price_date_utc": historical_usd["eth_usd_price_date_utc"],
            "usd_estimate_source": historical_usd["usd_estimate_source"],
            "usd_estimate_source_detail": historical_usd.get("usd_estimate_source_detail", ""),
            "usd_estimate_confidence": historical_usd.get("usd_estimate_confidence", "medium"),
            "usd_estimate_basis": dashboard_settlement_usd_basis(
                historical_usd,
                settlement.get("block_time_utc"),
            ),
            "block_number": settlement.get("block_number") or existing.get("block_number", ""),
            "tx_hash": settlement.get("tx_hash") or existing.get("tx_hash", ""),
        }
    )
    return row


def display_for(wallet: str, profiles: dict[str, tuple[str, str]]) -> tuple[str, str]:
    return profiles.get(wallet.lower(), (f"{short_wallet(wallet)}", f"https://basescan.org/address/{wallet}"))


def unified_bidder_projection(
    contract_wallet: str,
    display: str,
    profile_url: str,
    *,
    has_bid: bool,
) -> tuple[str | None, dict[str, Any]]:
    explorer = f"https://basescan.org/address/{contract_wallet}" if has_bid else None
    return explorer, {
        "display": display,
        "farcaster_fid": None,
        "farcaster_handle": display.lstrip("@") if display.startswith("@") else None,
        "profile_url": profile_url or None,
        "wallet": contract_wallet,
        "wallet_explorer_url": explorer,
    }


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


def authenticated_surface_checkpoint(
    current_row: dict[str, Any],
    metrics_rows: list[dict[str, Any]],
) -> int:
    """Require the current surface and hash-attested metric checkpoint to be atomic."""
    current_block = int_value(current_row.get("latest_block"), -1)
    metric_values = {
        str(row.get("metric")): str(row.get("value") or "")
        for row in metrics_rows
        if isinstance(row, dict)
    }
    raw_metric_block = metric_values.get("latest_block", "")
    if current_block <= 0 or not raw_metric_block.isdigit() or int(raw_metric_block) != current_block:
        raise FullRefreshRequired("current auction and mission metrics checkpoints disagree")
    return current_block


def validate_current_auction_coverage_contract(
    current_row: dict[str, Any],
) -> dict[str, Any]:
    """Fail to the full builder unless the exact queue-proof tuple is durable."""
    if not isinstance(current_row, dict):
        raise FullRefreshRequired("current_auction coverage row is not an object")
    token_id = current_row.get("token_id")
    amount_wei = current_row.get("amount_wei")
    start_time_unix = current_row.get("start_time_unix")
    end_time_unix = current_row.get("end_time_unix")
    bidder_wallet = current_row.get("bidder_wallet")
    settled = current_row.get("settled")
    if type(token_id) is not int or token_id < 1:
        raise FullRefreshRequired("current_auction coverage token_id is invalid")
    if not isinstance(amount_wei, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)", amount_wei) is None:
        raise FullRefreshRequired("current_auction coverage amount_wei is not canonical")
    if type(start_time_unix) is not int or start_time_unix < 1:
        raise FullRefreshRequired("current_auction coverage start_time_unix is invalid")
    if type(end_time_unix) is not int or end_time_unix < start_time_unix:
        raise FullRefreshRequired("current_auction coverage end_time_unix is invalid")
    if not isinstance(bidder_wallet, str) or re.fullmatch(r"0x[0-9a-f]{40}", bidder_wallet) is None:
        raise FullRefreshRequired("current_auction coverage bidder_wallet is invalid")
    if type(settled) is not int or settled not in {0, 1}:
        raise FullRefreshRequired("current_auction coverage settled flag is invalid")
    return current_row

def main() -> None:
    current_rows = read_json("current_auction")
    if not isinstance(current_rows, list) or not current_rows:
        raise RuntimeError("generated/current_auction.json has no baseline row")
    validate_current_auction_coverage_contract(current_rows[0])
    metrics_rows = read_json("mission3_metrics")
    if not isinstance(metrics_rows, list) or not metrics_rows:
        raise FullRefreshRequired("generated/mission3_metrics.json has no verified rarity baseline")
    previous_block = authenticated_surface_checkpoint(current_rows[0], metrics_rows)
    baseline_token_id = int_value(current_rows[0].get("token_id"), -1)
    if baseline_token_id < 0:
        raise RuntimeError("current auction baseline has no token_id")
    timeline_columns, timeline_rows = read_table("auction_timeline")
    extension_columns, baseline_extension_rows = read_table("auction_extensions")
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
    canonical_reorg_from_hash = builder.canonical_reorg_marker_from_env()
    latest_time = builder.utc_from_unix(int(latest_block_data["timestamp"], 16))
    current = builder.fetch_current_auction(latest_block, latest_time, hex(latest_block))
    require_exact_8_decimal_amount(
        current.get("amount_eth_exact", current.get("amount_eth")),
        "auction()",
    )
    token_id = int(current["token_id"])
    total_supply = builder.fetch_dog_total_supply(hex(latest_block))
    if token_id not in {baseline_token_id, baseline_token_id + 1}:
        raise FullRefreshRequired(
            f"current token jumped from {baseline_token_id} to {token_id}; bounded refresh can reconcile at most one transition"
        )
    continuity_result = verify_rarity_universe_continuity(
        builder,
        metrics_rows,
        latest_block=latest_block,
        latest_block_hash=str(verification["snapshot_block_hash"]),
        latest_dog_code_sha256=str(verification["dog_nft_code_sha256"]),
        latest_total_supply=total_supply,
    )
    continuity_metrics = dict(continuity_result["metrics"])
    minted_token_ids = tuple(continuity_result["minted_token_ids"])
    baseline_total_supply = int(continuity_result["baseline_total_supply"])
    if baseline_total_supply != baseline_token_id + 1:
        raise FullRefreshRequired(
            "baseline current Dog disagrees with the attested total-supply ceiling: "
            f"current={baseline_token_id} supply={baseline_total_supply}"
        )
    expected_surface_mints = (token_id,) if token_id == baseline_token_id + 1 else ()
    if minted_token_ids != expected_surface_mints:
        raise FullRefreshRequired(
            "current auction transition disagrees with canonical Dog mint continuity: "
            f"current={token_id} baseline={baseline_token_id} mints={list(minted_token_ids)}"
        )
    history = read_json("historical_dog_search")
    if not isinstance(history, list):
        raise FullRefreshRequired("generated/historical_dog_search.json is not a list")
    baseline_rarity_size = baseline_rarity_universe_size(
        metrics_rows,
        baseline_total_supply,
    )
    if minted_token_ids and baseline_rarity_size <= 0:
        raise FullRefreshRequired("canonical Dog mints cannot extend an incomplete cached rarity universe")
    metrics_by_name = {
        str(row.get("metric")): str(row.get("value", ""))
        for row in metrics_rows
        if isinstance(row, dict)
    }
    metrics_by_name.update(continuity_metrics)
    rarity_cache: dict[str, Any] | None = None
    baseline_rarity_attrs: dict[int, dict[str, str]] = {}
    if baseline_rarity_size > 0:
        baseline_rarity_attrs = verified_rarity_history_attributes(history)
        rarity_cache = validate_fresh_rarity_cache(
            builder,
            history,
            baseline_rarity_size,
            metrics_by_name["dog_base_existing_token_ids_sha256"],
            # A failed/rolled-back generation can safely leave this private
            # cache newer than the committed dashboard baseline. Accept only
            # hash-valid, trait-identical records no newer than the current
            # quorum-verified snapshot; rejecting them would force an
            # unnecessary full rebuild after every such recovery.
            latest_block,
        )
    metadata_token_ids = sorted({token_id, *minted_token_ids})
    current_token_uris = builder.fetch_token_uri_bindings(
        metadata_token_ids,
        hex(latest_block),
        block_hash=str(verification["snapshot_block_hash"]),
    )
    verified_metadata_records: dict[int, dict[str, Any]] = {}
    verified_metadata: dict[int, dict[str, Any]] = {}
    verified_rarity_attrs: dict[int, dict[str, str]] = {}
    for metadata_token_id in metadata_token_ids:
        token_uri = current_token_uris[metadata_token_id]
        if token_uri is None:
            raise FullRefreshRequired(
                f"Dog #{metadata_token_id} does not exist on Base at the verified snapshot"
            )
        metadata_record = builder.authoritative_metadata_record(
            metadata_token_id,
            hex(latest_block),
            token_uri,
        )
        metadata_value = metadata_record.get("metadata")
        if not isinstance(metadata_value, dict):
            raise FullRefreshRequired(f"Dog #{metadata_token_id} metadata record is malformed")
        verified_metadata_records[metadata_token_id] = metadata_record
        verified_metadata[metadata_token_id] = metadata_value
        verified_rarity_attrs[metadata_token_id] = rarity_attributes_from_metadata(
            metadata_token_id,
            metadata_value,
        )
    pending_rarity_cache: dict[str, Any] | None = None
    if rarity_cache is not None:
        pending_rarity_cache = dict(rarity_cache)
        cache_observed_at = now_utc()
        for metadata_token_id, metadata_record in verified_metadata_records.items():
            if (
                metadata_token_id not in minted_token_ids
                and verified_rarity_attrs[metadata_token_id]
                != baseline_rarity_attrs.get(metadata_token_id)
            ):
                raise FullRefreshRequired(
                    f"fresh Dog #{metadata_token_id} traits differ from the attested cached rarity universe"
                )
            validate_rarity_cache_entry(
                builder,
                metadata_token_id,
                metadata_record,
                (
                    verified_rarity_attrs[metadata_token_id]
                    if metadata_token_id in minted_token_ids
                    else baseline_rarity_attrs[metadata_token_id]
                ),
                observed_at=cache_observed_at,
                max_verified_block=latest_block,
                expected_token_uri=str(current_token_uris[metadata_token_id]),
            )
            pending_rarity_cache[str(metadata_token_id)] = metadata_record
    metadata = verified_metadata[token_id]

    timeline_by_token = {int_value(row.get("token_id"), -1): dict(row) for row in timeline_rows}
    previous_token_id = token_id - 1
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

    log_coverage_ranges = [(from_block, latest_block)]
    logs = builder.fetch_logs(
        builder.AUCTION_HOUSE,
        [
            builder.TOPIC_AUCTION_CREATED,
            builder.TOPIC_AUCTION_BID,
            builder.TOPIC_AUCTION_EXTENDED,
            builder.TOPIC_AUCTION_SETTLED,
        ],
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
            log_coverage_ranges.append((reconcile_from, reconcile_to))
            logs.extend(
                builder.fetch_logs(
                    builder.AUCTION_HOUSE,
                    [
                        builder.TOPIC_AUCTION_CREATED,
                        builder.TOPIC_AUCTION_BID,
                        builder.TOPIC_AUCTION_EXTENDED,
                        builder.TOPIC_AUCTION_SETTLED,
                    ],
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
    expected_auction_address = str(builder.AUCTION_HOUSE).lower()
    expected_auction_topics = {
        builder.TOPIC_AUCTION_CREATED,
        builder.TOPIC_AUCTION_BID,
        builder.TOPIC_AUCTION_EXTENDED,
        builder.TOPIC_AUCTION_SETTLED,
    }
    for row in logs:
        topics = row.get("topics")
        try:
            block_number = int(str(row.get("blockNumber") or ""), 16)
        except ValueError as exc:
            raise FullRefreshRequired("auction log has a malformed block number") from exc
        if (
            str(row.get("address") or "").lower() != expected_auction_address
            or not isinstance(topics, list)
            or not topics
            or str(topics[0]).lower() not in expected_auction_topics
            or not any(start <= block_number <= end for start, end in log_coverage_ranges)
        ):
            raise FullRefreshRequired("auction log is outside its verified address/topic/range filter")
    created_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_CREATED]
    bid_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_BID]
    extension_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_EXTENDED]
    settled_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_SETTLED]
    extension_block_hashes: dict[int, str] = {}
    for log in extension_logs:
        block_number = int(str(log.get("blockNumber") or ""), 16)
        block_hash = builder.canonical_block_hash(log.get("blockHash"))
        if not block_hash:
            raise FullRefreshRequired("AuctionExtended log is missing its canonical block hash")
        previous_hash = extension_block_hashes.setdefault(block_number, block_hash)
        if previous_hash != block_hash:
            raise FullRefreshRequired("AuctionExtended logs disagree on their canonical block hash")
    if extension_block_hashes:
        builder.fetch_block_times(set(extension_block_hashes), extension_block_hashes)
    created, bids, settled = builder.decode_auction_logs(created_logs, bid_logs, settled_logs)
    for bid in bids:
        require_exact_8_decimal_amount(
            bid.get("bid_eth_exact", bid.get("bid_eth")),
            "AuctionBid",
        )
    for settlement in settled:
        require_exact_8_decimal_amount(
            settlement.get("amount_eth_exact", settlement.get("amount_eth")),
            "AuctionSettled",
        )
    extensions = builder.decode_auction_extension_logs(extension_logs)
    extension_rows = merge_extension_overlap(
        baseline_extension_rows,
        extensions,
        log_coverage_ranges,
    )
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
    validate_extended_bid_pairs(fresh_bids, extensions, {previous_token_id, token_id})

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
    mission3_source_previous = next(
        (
            row
            for row in mission3_source_rows
            if isinstance(row, dict)
            and int_value(row.get("token_id"), -1) == previous_token_id
        ),
        {},
    )
    current_created_rows = [row for row in created if int(row.get("token_id")) == token_id]
    if len(current_created_rows) > 1:
        raise FullRefreshRequired(f"multiple AuctionCreated logs observed for Dog #{token_id}")
    current_created = max(
        current_created_rows,
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
    previous_created_rows = [
        row for row in created if int(row.get("token_id")) == previous_token_id
    ]
    if len(previous_created_rows) > 1:
        raise FullRefreshRequired(f"multiple AuctionCreated logs observed for Dog #{previous_token_id}")
    previous_created = previous_created_rows[0] if previous_created_rows else {}
    current_extension_fields = reconcile_timeline_extensions(
        current_timeline_baseline,
        current_created,
        [row for row in extension_rows if int_value(row.get("token_id"), -1) == token_id],
        authenticated_checkpoint_block=previous_block,
        snapshot_block=latest_block,
        expected_end_time_utc=current.get("end_time_utc"),
    )
    previous_extension_fields = reconcile_timeline_extensions(
        previous_timeline,
        previous_created,
        [row for row in extension_rows if int_value(row.get("token_id"), -1) == previous_token_id],
        authenticated_checkpoint_block=previous_block,
        snapshot_block=latest_block,
    )

    eth_usd, eth_source = builder.fetch_eth_usd_price()
    amount_eth = Decimal(str(current["amount_eth"]))
    amount_usd = numeric_usd_2(amount_eth, eth_usd)
    contract_wallet = str(current.get("bidder") or "").lower()
    wallet = "" if contract_wallet == builder.ZERO else contract_wallet
    profiles = profile_map()
    try:
        for profile in builder.fetch_degendogs_auction_profiles(current):
            address = str(profile.get("address") or "").lower()
            handle = str(profile.get("username") or "").strip().lstrip("@")
            if address and handle:
                profiles[address] = (f"@{handle}", f"https://farcaster.xyz/{handle}")
    except Exception as exc:  # noqa: BLE001
        print(f"warning: current Farcaster identity lookup failed: {exc}", file=sys.stderr)
    bidder, bidder_url = display_for(wallet, profiles) if wallet else ("no bids yet", "")
    formatted_fresh_bids = format_fresh_bid_rows(
        fresh_bids,
        eth_usd=eth_usd,
        eth_source=eth_source,
        price_date_utc=latest_time[:10],
        profiles=profiles,
    )
    formatted_historical_bids = format_historical_bid_rows(
        fresh_bids,
        baseline_recent_bid_rows,
        profiles,
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
    active_history_rows = [
        *history_by_token.get(token_id, []),
        *(
            history_by_token.get(previous_token_id, [])
            if previous_token_id != token_id
            else []
        ),
    ]
    historical_active_bids = format_historical_bid_rows(
        active_history_rows,
        baseline_recent_bid_rows,
        profiles,
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

    ensure_no_untracked_bid_gap(
        timeline_rows,
        baseline_recent_bid_rows,
        [*formatted_historical_bids, *historical_active_bids],
    )
    recent_bid_rows, added_bid_rows, removed_bid_rows, known_bid_rows = reconcile_recent_bid_rows(
        baseline_recent_bid_rows,
        formatted_historical_bids,
        historical_active_bids,
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
    surface_status = auction_feed_status(state)
    bid_text = f"{format_eth_5(amount_eth)} ETH (${format_usd_product_0(amount_eth, eth_usd)})"
    bid_time = current_bids[-1].get("bid_time_utc") if current_bids else latest_time
    dog_attrs = verified_rarity_attrs[token_id]
    rarity_universe_size = (
        baseline_rarity_size + len(minted_token_ids) if baseline_rarity_size > 0 else 0
    )
    rarity_available = rarity_universe_size > 0
    rarity_metric_updates: dict[str, str] = {}
    if rarity_available:
        rarity_universe = build_extended_rarity_universe(
            history,
            verified_rarity_attrs,
            minted_token_ids,
            baseline_rarity_size,
            metrics_by_name["dog_base_existing_token_ids_sha256"],
        )
        rarity_metric_updates = extend_rarity_aggregate_metrics(
            metrics_rows,
            minted_token_ids,
            set(rarity_universe),
        )
        metrics_by_name.update(rarity_metric_updates)
        current_rarity = rarity_universe[token_id]
        rarity = str(current_rarity["rarity"])
        rarity_score: float | None = float(current_rarity["rarity_score"])
        traits = str(current_rarity["traits"])
        trait_rarity = str(current_rarity["trait_rarity"])
    else:
        # Base-existing rarity is universe-wide. Preserve verified traits for
        # display, but withhold every score and frequency while any Base-existing
        # tokenURI metadata remains unresolved.
        rarity_universe = {}
        rarity = "Unavailable"
        rarity_score = None
        traits = "; ".join(f"{key}: {value}" for key, value in dog_attrs.items())
        trait_rarity = ""
    historical_by_token = {
        int_value(row.get("token_id"), -1): row
        for row in history
        if isinstance(row, dict) and int_value(row.get("token_id"), -1) >= 0
    }
    rarity_rebase_required = rarity_available and (
        bool(minted_token_ids)
        or any(
            token not in historical_by_token
            or rarity_row_requires_rebase(historical_by_token[token], values)
            for token, values in rarity_universe.items()
        )
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
            "metadata_verification_status": "onchain_token_uri_verified",
            "current_bid": bid_text,
            "current_bid_eth": numeric_8(amount_eth),
            "amount_wei": str(current.get("amount_wei", "")),
            "current_bid_usd": amount_usd,
            "current_bid_usd_live": amount_usd,
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "bidder": bidder,
            "bidder_url": bidder_url,
            "bidder_wallet": contract_wallet,
            "start_time_utc": current.get("start_time_utc", ""),
            "end_time_utc": current.get("end_time_utc", ""),
            "start_time_unix": current.get("start_time_unix"),
            "end_time_unix": current.get("end_time_unix"),
            "auction_state": state,
            "seconds_remaining": remaining,
            "time_remaining": (
                format_seconds(remaining)
                if state == "live"
                else "ended"
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
            "latest_bid_eth": numeric_8(amount_eth),
            "latest_bid_usd": amount_usd,
            "latest_bid_usd_live": amount_usd,
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "bidder": bidder,
            "bidder_url": bidder_url,
            "bidder_wallet": contract_wallet,
            "bid_time_utc": bid_time,
            "auction_state": state,
            "time_remaining": current_row["time_remaining"],
            "auction_end_utc": current.get("end_time_utc", ""),
            "traits": traits,
            "trait_rarity": trait_rarity,
            "rarity": rarity,
            "metadata_verification_status": "onchain_token_uri_verified",
        }
    )
    latest_columns, _ = read_table("current_latest_bid")
    write_table("current_latest_bid", latest_columns, [latest_row])

    current_history_rows = [
        {column: row.get(column, "") for column in history_columns}
        for row in high_bid_first(current_bids)
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
    feed_rows = normalize_table_rarity_rows(
        feed_rows,
        rarity_universe,
        token_from_dog_label=True,
    )
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
        settled_previous["time_remaining"] = ""
        settled_previous["auction_end_utc"] = ""
        if previous_settlement:
            previous_wallet = str(previous_settlement.get("winner") or "").lower()
            previous_bidder, previous_bidder_url = display_for(previous_wallet, profiles)
            previous_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
            if previous_historical_usd is None:
                raise FullRefreshRequired(f"Dog #{previous_token_id} settlement lost historical USD provenance")
            previous_event_price = Decimal(previous_historical_usd["eth_usd_price_at_event"])
            if previous_bids:
                previous_last_bid = previous_bids[-1].get("bid_time_utc")
            elif int_value(previous_timeline.get("bids")) > 0:
                previous_last_bid = (
                    settled_previous.get("last_bid_utc")
                    or previous_timeline.get("latest_bid_utc")
                )
                if not previous_last_bid:
                    raise FullRefreshRequired(
                        f"Dog #{previous_token_id} has bids but no canonical last-bid timestamp"
                    )
            else:
                previous_last_bid = None
            settled_previous.update(
                {
                    "bidder_winner": previous_bidder,
                    "bidder_winner_url": previous_bidder_url,
                    "bidder_winner_wallet": previous_wallet,
                    "bid": f"{format_eth_5(previous_amount_eth)} ETH (${format_usd_product_0(previous_amount_eth, previous_event_price)})",
                    "amount_eth": numeric_8(previous_amount_eth),
                    "amount_usd": numeric_usd_2(previous_amount_eth, previous_event_price),
                    "amount_usd_at_event": numeric_usd_2(previous_amount_eth, previous_event_price),
                    "eth_usd_price_live": None,
                    "eth_usd_price_at_event": previous_historical_usd["eth_usd_price_at_event"],
                    "eth_usd_price_date_utc": previous_historical_usd["eth_usd_price_date_utc"],
                    "usd_estimate_source": previous_historical_usd["usd_estimate_source"],
                    "usd_estimate_source_detail": previous_historical_usd.get("usd_estimate_source_detail", ""),
                    "usd_estimate_confidence": previous_historical_usd.get("usd_estimate_confidence", "medium"),
                    "usd_estimate_basis": dashboard_settlement_usd_basis(
                        previous_historical_usd,
                        previous_settlement.get("block_time_utc"),
                    ),
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
            "bidder_winner_wallet": contract_wallet,
            "bid": bid_text,
            "amount_eth": numeric_8(amount_eth),
            "amount_usd": amount_usd,
            "amount_usd_at_event": None,
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_at_event": None,
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
            "metadata_verification_status": "onchain_token_uri_verified",
        }
    )
    if state == "settled":
        if current_historical_usd is None:
            raise FullRefreshRequired(f"Dog #{token_id} settlement lost historical USD provenance")
        settled_event_price = Decimal(current_historical_usd["eth_usd_price_at_event"])
        new_feed.update(
            {
                "bid": f"{format_eth_5(amount_eth)} ETH (${format_usd_product_0(amount_eth, settled_event_price)})",
                "amount_usd": numeric_usd_2(amount_eth, settled_event_price),
                "amount_usd_at_event": numeric_usd_2(amount_eth, settled_event_price),
                "eth_usd_price_live": None,
                "eth_usd_price_at_event": current_historical_usd["eth_usd_price_at_event"],
                "eth_usd_price_date_utc": current_historical_usd["eth_usd_price_date_utc"],
                "usd_estimate_source": current_historical_usd["usd_estimate_source"],
                "usd_estimate_source_detail": current_historical_usd.get("usd_estimate_source_detail", ""),
                "usd_estimate_confidence": current_historical_usd.get("usd_estimate_confidence", "medium"),
                "usd_estimate_basis": dashboard_settlement_usd_basis(
                    current_historical_usd,
                    current_settlement.get("block_time_utc"),
                ),
            }
        )
    remaining_feed = [row for row in feed_rows if str(row.get("dog")) not in {f"Dog #{token_id}", f"Dog #{previous_token_id}"}]
    output_feed = [new_feed]
    if settled_previous is not None:
        output_feed.append(settled_previous)
    output_feed.extend(remaining_feed)
    write_table("auction_feed", feed_columns, output_feed)

    # Add the newly minted/auctioned Dog to the unified searchable table.
    history_columns, history_rows = prepare_historical_rarity_table(
        *read_table("historical_dog_search"),
        rarity_universe,
    )
    # Older generated snapshots predate the score column. The bounded writer
    # migrates that CSV schema before writing rebased scores; write_table
    # deliberately ignores dictionary keys outside its explicit field list.
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
                }
            )
            apply_historical_provenance_defaults(row, mission3_source_previous)
            row["search_text"] = canonical_historical_search_text(row)
    new_search = dict(historical_by_token.get(token_id, {}))
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
            "status": (
                "ended pending settlement"
                if state == "ended_unsettled"
                else state
            ),
            "winner": bidder,
            "winner_url": bidder_url,
            "winner_wallet": contract_wallet,
            "amount": bid_text,
            "amount_raw": str(current.get("amount_wei", "")),
            "bid_count": len(current_bids),
            "unique_bidder_count": len({bid_wallet(row) for row in current_bids if bid_wallet(row)}),
            "auction_created_time_utc": current.get("start_time_utc", ""),
            "settled_time_utc": (current_settlement or {}).get("block_time_utc", "") if state == "settled" else "",
            "rarity": rarity,
            "rarity_score": rarity_score,
            "traits": traits,
            "trait_rarity": trait_rarity,
            "metadata_verification_status": "onchain_token_uri_verified",
        }
    )
    apply_historical_provenance_defaults(new_search, mission3_source_current)
    new_search["search_text"] = canonical_historical_search_text(new_search)
    write_table("historical_dog_search", history_columns, [*history_rows, new_search])

    report_columns, report_rows = read_table("historical_dog_report")
    report_rows = recompute_historical_report_rows(
        report_rows,
        [*history_rows, new_search],
    )
    write_table("historical_dog_report", report_columns, report_rows)

    # Reconcile the specialized Mission 3 tables too. These are the source
    # tables used to build the unified archive rows and must move together with
    # the homepage feed when a previous auction settles.
    timeline_rows = normalize_table_rarity_rows(timeline_rows, rarity_universe)
    timeline_rows = [row for row in timeline_rows if int_value(row.get("token_id"), -1) not in {previous_token_id, token_id}]
    if previous_settlement:
        previous_wallet = str(previous_settlement.get("winner") or "").lower()
        previous_bidder, previous_bidder_url = display_for(previous_wallet, profiles)
        previous_amount_eth = Decimal(str(previous_settlement.get("amount_eth") or 0))
        previous_latest_bid = previous_bids[-1] if previous_bids else {}
        previous_timeline_row = dict(previous_timeline)
        previous_timeline_row.update(
            {
                **previous_extension_fields,
                "auction_state": "settled",
                "bids": len(previous_bids) or int_value(previous_timeline.get("bids")),
                "unique_bidders": len({bid_wallet(item) for item in previous_bids if bid_wallet(item)}) or int_value(previous_timeline.get("unique_bidders")),
                "high_bid_eth": numeric_8(max([Decimal(str(item.get('bid_eth') or 0)) for item in previous_bids] or [previous_amount_eth])),
                "total_bid_eth": numeric_8(sum((Decimal(str(item.get('bid_eth') or 0)) for item in previous_bids), Decimal(0))) if previous_bids else previous_timeline.get("total_bid_eth", 0.0),
                "latest_bidder": display_for(bid_wallet(previous_latest_bid), profiles)[0] if previous_latest_bid else previous_timeline.get("latest_bidder", ""),
                "latest_bidder_url": display_for(bid_wallet(previous_latest_bid), profiles)[1] if previous_latest_bid else previous_timeline.get("latest_bidder_url", ""),
                "latest_bid_eth": numeric_8(previous_latest_bid.get("bid_eth", previous_timeline.get("latest_bid_eth", 0.0)) or 0),
                "latest_bid_utc": previous_latest_bid.get("bid_time_utc", previous_timeline.get("latest_bid_utc", "")),
                "winner": previous_bidder,
                "winner_url": previous_bidder_url,
                "settled_eth": numeric_8(previous_amount_eth),
                "settled_time_utc": previous_settlement.get("block_time_utc", ""),
                "settled_tx_hash": previous_settlement.get("tx_hash", ""),
            }
        )
        if previous_token_id in rarity_universe:
            apply_rarity_fields(previous_timeline_row, rarity_universe[previous_token_id])
        timeline_rows.append(previous_timeline_row)
    current_timeline_row = dict(timeline_by_token.get(token_id) or {})
    settled_wallet = str((current_settlement or {}).get("winner") or "").lower()
    settled_bidder, settled_bidder_url = display_for(settled_wallet, profiles) if settled_wallet else ("", "")
    settled_amount = Decimal(str((current_settlement or {}).get("amount_eth") or 0))
    current_timeline_row.update(
        {
            **current_extension_fields,
            "token_id": token_id,
            "dog_image_url": current_row["dog_image_url"],
            "start_time_utc": current.get("start_time_utc", ""),
            "auction_state": state,
            "bids": len(current_bids),
            "unique_bidders": len({bid_wallet(item) for item in current_bids if bid_wallet(item)}),
            "high_bid_eth": numeric_8(max((Decimal(str(item.get('bid_eth') or 0)) for item in current_bids), default=amount_eth)),
            "total_bid_eth": numeric_8(sum((Decimal(str(item.get('bid_eth') or 0)) for item in current_bids), Decimal(0))),
            "latest_bidder": bidder if current_bids else None,
            "latest_bidder_url": bidder_url if current_bids else None,
            "latest_bid_eth": numeric_8(amount_eth),
            "latest_bid_utc": bid_time if current_bids else None,
            "winner": settled_bidder if state == "settled" else None,
            "winner_url": settled_bidder_url if state == "settled" else None,
            "settled_eth": numeric_8(settled_amount) if state == "settled" else 0.0,
            "settled_time_utc": (current_settlement or {}).get("block_time_utc", "") if state == "settled" else None,
            "rarity": rarity,
            "created_tx_hash": current_auction_created["tx_hash"],
            "settled_tx_hash": (current_settlement or {}).get("tx_hash", "") if state == "settled" else None,
        }
    )
    if token_id in rarity_universe:
        apply_rarity_fields(current_timeline_row, rarity_universe[token_id])
    else:
        current_timeline_row["metadata_verification_status"] = "onchain_token_uri_verified"
    timeline_rows.append(current_timeline_row)
    timeline_rows.sort(key=lambda row: int_value(row.get("token_id"), -1), reverse=True)
    daily_rows = recompute_daily_activity(
        baseline_daily_rows,
        timeline_rows,
        known_bid_rows,
        affected_days,
    )
    write_table("auction_extensions", extension_columns, extension_rows)
    write_table("auction_timeline", timeline_columns, timeline_rows)
    write_table("auction_daily_activity", daily_columns, daily_rows)

    winner_columns, winner_rows = read_table("auction_winners")
    winner_rows = normalize_table_rarity_rows(winner_rows, rarity_universe)
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
        previous_event_price = Decimal(previous_historical_usd["eth_usd_price_at_event"])
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
                "winning_bid": f"{format_eth_5(previous_amount_eth)} ETH (${format_usd_product_0(previous_amount_eth, previous_event_price)})",
                "winning_bid_eth": numeric_8(previous_amount_eth),
                "bid_count": len(previous_bids) or int_value(previous_timeline.get("bids")),
                "unique_bidders": len({bid_wallet(item) for item in previous_bids if bid_wallet(item)}) or int_value(previous_timeline.get("unique_bidders")),
                "first_bid_utc": previous_bids[0].get("bid_time_utc", "") if previous_bids else None,
                "last_bid_utc": previous_bids[-1].get("bid_time_utc", "") if previous_bids else None,
            },
        )
        if previous_token_id in rarity_universe:
            apply_rarity_fields(winner_row, rarity_universe[previous_token_id])
        winner_rows.append(winner_row)
    if current_settlement:
        current_winner_wallet = str(current_settlement.get("winner") or "").lower()
        current_winner, current_winner_url = display_for(current_winner_wallet, profiles)
        current_winning_eth = Decimal(str(current_settlement.get("amount_eth") or 0))
        if current_historical_usd is None:
            raise FullRefreshRequired(f"Dog #{token_id} settlement lost historical USD provenance")
        current_event_price = Decimal(current_historical_usd["eth_usd_price_at_event"])
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
                "winning_bid": f"{format_eth_5(current_winning_eth)} ETH (${format_usd_product_0(current_winning_eth, current_event_price)})",
                "winning_bid_eth": numeric_8(current_winning_eth),
                "bid_count": len(current_bids),
                "unique_bidders": len({bid_wallet(item) for item in current_bids if bid_wallet(item)}),
                "first_bid_utc": current_bids[0].get("bid_time_utc", "") if current_bids else None,
                "last_bid_utc": current_bids[-1].get("bid_time_utc", "") if current_bids else None,
            },
        )
        if token_id in rarity_universe:
            apply_rarity_fields(current_winner_row, rarity_universe[token_id])
        else:
            current_winner_row["metadata_verification_status"] = (
                "onchain_token_uri_verified"
            )
        winner_rows.append(current_winner_row)
    winner_rows.sort(key=lambda row: str(row.get("settled_time_utc", "")), reverse=True)
    write_table("auction_winners", winner_columns, winner_rows)
    leaderboard_rows = apply_winner_stats_to_leaderboard(leaderboard_rows, winner_rows)
    write_table("auction_bidder_leaderboard", leaderboard_columns, leaderboard_rows)

    recent_columns, _ = read_table("recent_auction_winners")
    recent_rows = []
    for row in winner_rows[:10]:
        winner_token_id = int_value(row.get("token_id"), -1)
        recent_row = {
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
            "metadata_verification_status": row.get("metadata_verification_status", ""),
            "rarity": row.get("rarity", ""),
            "last_bid_utc": row.get("last_bid_utc", ""),
            "settled_time_utc": row.get("settled_time_utc", ""),
        }
        if winner_token_id in rarity_universe:
            recent_row["rarity"] = rarity_universe[winner_token_id]["rarity"]
            recent_row["metadata_verification_status"] = "onchain_token_uri_verified"
        recent_rows.append(recent_row)
    write_table("recent_auction_winners", recent_columns, recent_rows)

    unified_rows = json.loads(json.dumps(baseline_unified_rows))
    changed_unified_tokens: set[int] = set()
    for row in unified_rows:
        if not isinstance(row, dict):
            continue
        unified_token_id = int_value(row.get("dog_id"), -1)
        if (
            unified_token_id in rarity_universe
            and apply_unified_rarity_fields(row, rarity_universe[unified_token_id])
        ):
            changed_unified_tokens.add(unified_token_id)
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
    trait_items = (
        unified_trait_items(trait_rarity)
        if rarity_available
        else [
            {"display": f"{key}: {value}", "trait_type": key, "value": value}
            for key, value in dog_attrs.items()
        ]
    )
    unified_rarity = (
        {
            "display": rarity,
            "rank": int(rarity.split("/")[0].lstrip("#")),
            "total": rarity_universe_size,
            "scope": "base_existing",
        }
        if rarity_available
        else {"display": "Unavailable", "rank": None, "total": None}
    )
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
    baseline_source = (
        baseline_unified_current.get("source")
        if isinstance(baseline_unified_current.get("source"), dict)
        else {}
    )
    unified_source = canonical_unified_source(
        baseline_source,
        mission3_source_current,
        settled=state == "settled",
        baseline_exists=bool(baseline_unified_current),
    )
    unified_explorer, unified_bidder = unified_bidder_projection(
        contract_wallet,
        bidder,
        bidder_url,
        has_bid=bool(wallet),
    )
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
            "links": {"auction_tx": current_auction_created["tx_url"], "dog_page": current_row["dog_external_url"], "explorer": unified_explorer, "item": current_row["dog_opensea_url"], "repo_archive": f"archive/dogs/by-id/{token_id:03d}.json", "settlement_tx": None},
            "mission": 3,
            "rarity": unified_rarity,
            "source": unified_source,
            "status": surface_status,
            "traits": trait_items,
            "winner_or_high_bidder": unified_bidder,
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
    unified_row["search_text"] = canonical_unified_current_search_text(
        unified_row,
        token_id,
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
    affected_unified_tokens = {
        token_id,
        *changed_unified_tokens,
        *unified_by_id_mismatches(unified_rows),
    }
    if should_reconcile_previous_unified:
        affected_unified_tokens.add(previous_token_id)
    write_unified_by_id_records(unified_rows, affected_unified_tokens)
    # The fast path can add a newly minted Dog, so keep the cached historical
    # USD estimate table in the same transaction boundary as unified/by-id.
    # This is local-only and performs no network fetch.
    import archive_apply_usd_estimates as archive_prices

    archive_prices.main()

    metrics = {str(row.get("metric")): str(row.get("value", "")) for row in metrics_rows}
    metrics.update(rarity_metric_updates)
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
            "dog_id_ceiling": str(total_supply),
            "current_auction_status": state,
            "current_auction_end_utc": str(current.get("end_time_utc", "")),
            "current_bid_eth": str(current.get("amount_eth", "")),
            "current_bid_usd": str(amount_usd),
            "current_bidder": bidder,
            "current_bidder_wallet": contract_wallet,
            "latest_block": str(latest_block),
            "latest_block_time_utc": latest_time,
            "canonical_reorg_from_hash": canonical_reorg_from_hash,
            **{str(key): str(value) for key, value in verification.items()},
            **continuity_metrics,
        }
    )
    metric_rows = [{"metric": key, "value": value} for key, value in metrics.items()]
    metric_columns, _ = read_table("mission3_metrics")
    write_table("mission3_metrics", metric_columns or ["metric", "value"], metric_rows)
    update_readme_snapshot(current_row, metrics, builder)
    refresh_status = read_json("refresh_status")
    refresh_status.pop("canonical_reorg_from_hash", None)
    refresh_status.update(
        {
            "last_refresh_result": "success_generated",
            "last_successful_refresh_time_utc": now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "latest_generated_block": latest_block,
            "latest_generated_block_time_utc": canonical_utc_z(
                latest_time,
                "latest generated block time",
            ),
            "current_dog_token_id": token_id,
            "current_auction_end_time_utc": canonical_utc_z(
                current.get("end_time_utc", ""),
                "current auction end time",
            ),
            "current_auction_status": state,
            "current_bid_eth": str(current.get("amount_eth", "")),
            "current_high_bidder": bidder,
            "current_high_bidder_wallet": contract_wallet,
            "refresh_reason": "current_surface_incremental",
            "dog_total_supply": str(total_supply),
            "dog_id_ceiling": str(total_supply),
            **rarity_metric_updates,
            **{str(key): str(value) for key, value in verification.items()},
            **continuity_metrics,
        }
    )
    if canonical_reorg_from_hash:
        refresh_status["canonical_reorg_from_hash"] = canonical_reorg_from_hash
    write_json("refresh_status", normalize_refresh_status_integer_fields(refresh_status))

    # Repoint the dashboard only after all four live source mirrors and the
    # verified refresh status have reached the same bounded-refresh checkpoint.
    from build_live_snapshot_bundle import build_live_snapshot_bundle

    build_live_snapshot_bundle(root=ROOT)

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
    if pending_rarity_cache is not None:
        builder.write_dog_cache(pending_rarity_cache)
    print(json.dumps({"status": "success_current_surface", "token_id": token_id, "latest_block": latest_block, "new_logs": len(logs), "new_bids": len(current_bids), "current_bid_eth": str(amount_eth)}, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except TransientSnapshotLag as exc:
        print(json.dumps({"status": "transient_snapshot_lag", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(76) from exc
    except FullRefreshRequired as exc:
        print(json.dumps({"status": "full_refresh_required", "reason": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(75) from exc
