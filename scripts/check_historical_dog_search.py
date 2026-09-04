#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_COLUMNS = {
    "mission",
    "chain",
    "token_id",
    "dog",
    "status",
    "winner",
    "amount",
    "bid_count",
    "unique_bidder_count",
    "auction_created_time_utc",
    "settled_time_utc",
    "dog_opensea_url",
    "traits",
    "rarity",
    "rarity_score",
    "confidence",
    "search_text",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise AssertionError(f"missing {path.relative_to(ROOT)}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_json_list(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise AssertionError(f"missing {path.relative_to(ROOT)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise AssertionError(f"{path.relative_to(ROOT)} must be a non-empty JSON list")
    return data


def read_metric(key: str) -> str:
    for row in read_csv(ROOT / "generated" / "mission3_metrics.csv"):
        if row.get("metric") == key:
            return row.get("value", "")
    raise AssertionError(f"missing mission3 metric {key}")


def assert_json_matches_csv(csv_path: Path, json_path: Path, expected_rows: int) -> None:
    if not json_path.exists():
        raise AssertionError(f"missing {json_path.relative_to(ROOT)}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise AssertionError(f"{json_path.relative_to(ROOT)} must be a JSON list")
    if len(data) != expected_rows:
        raise AssertionError(f"{json_path.relative_to(ROOT)} rows {len(data)} != CSV rows {expected_rows}")


def assert_artifact_pair_matches(generated_path: Path, public_path: Path) -> None:
    if generated_path.read_bytes() != public_path.read_bytes():
        raise AssertionError(f"public artifact {public_path.relative_to(ROOT)} differs from {generated_path.relative_to(ROOT)}")


def int_field(row: dict[str, str], key: str) -> int:
    raw = row.get(key, "0") or "0"
    return int(raw)


def dog_id_from_feed(row: dict[str, object]) -> int:
    for key in ("dog_id", "token_id"):
        raw = row.get(key)
        if raw not in (None, ""):
            return int(str(raw))
    text = str(row.get("dog") or row.get("dog_name") or "")
    parts = "".join(ch if ch.isdigit() else " " for ch in text).split()
    return int(parts[-1]) if parts else -1


def iso_utc(value: object) -> str:
    text = str(value or "").strip().replace(" ", "T")
    return text if not text or text.endswith("Z") else f"{text}Z"


def expected_native(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text.rstrip("0").rstrip(".") if "." in text else text


def decimal_value(value: object) -> Decimal | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def historical_native(value: object) -> Decimal | None:
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s+[A-Za-z]+", str(value or ""))
    return decimal_value(match.group(1)) if match else None


def mission3_expected_status(value: object) -> str:
    status = str(value or "").strip().lower().replace("_", " ")
    if status in {"live", "ongoing"}:
        return "ongoing"
    if status in {"ended unsettled", "ended pending settlement", "live or unsettled"}:
        return "ended pending settlement"
    return status


def indexed_token_rows(
    rows: list[dict[str, object]],
    *,
    label: str,
    mission: int | None = None,
) -> dict[int, dict[str, object]]:
    indexed: dict[int, dict[str, object]] = {}
    for offset, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AssertionError(f"{label} row {offset} is not an object")
        if mission is not None and str(row.get("mission")) != str(mission):
            continue
        dog_id = dog_id_from_feed(row)
        if dog_id < 0:
            raise AssertionError(f"{label} row {offset} has no token id")
        if dog_id in indexed:
            raise AssertionError(f"{label} contains duplicate Dog #{dog_id}")
        indexed[dog_id] = row
    if not indexed:
        raise AssertionError(f"{label} has no token rows")
    return indexed


def assert_mission3_onchain_parity(
    unified_rows: list[dict[str, object]],
    historical_rows: list[dict[str, object]],
    timeline_rows: list[dict[str, object]],
    winner_rows: list[dict[str, object]],
    mission3_source_rows: list[dict[str, object]],
) -> None:
    historical = indexed_token_rows(historical_rows, label="historical_dog_search Mission 3", mission=3)
    timeline = indexed_token_rows(timeline_rows, label="auction_timeline")
    winners = indexed_token_rows(winner_rows, label="auction_winners")
    unified = indexed_token_rows(unified_rows, label="unified Mission 3", mission=3)
    source = indexed_token_rows(mission3_source_rows, label="Mission 3 source index")
    if set(timeline) != set(historical):
        raise AssertionError(
            "Mission 3 auction_timeline coverage differs from historical_dog_search "
            f"missing={sorted(set(historical) - set(timeline))[:20]} extra={sorted(set(timeline) - set(historical))[:20]}"
        )
    if set(unified) != set(historical):
        raise AssertionError(
            "Mission 3 unified coverage differs from historical_dog_search "
            f"missing={sorted(set(historical) - set(unified))[:20]} extra={sorted(set(unified) - set(historical))[:20]}"
        )
    settled_ids = {dog_id for dog_id, row in timeline.items() if mission3_expected_status(row.get("auction_state")) == "settled"}
    if set(winners) != settled_ids:
        raise AssertionError(
            "auction_winners coverage differs from settled Mission 3 timeline "
            f"missing={sorted(settled_ids - set(winners))[:20]} extra={sorted(set(winners) - settled_ids)[:20]}"
        )

    settlement_blocks_by_tx: dict[str, int] = {}
    for dog_id, winner in winners.items():
        tx_hash = str(winner.get("tx_hash") or "").lower()
        block_number = int(winner.get("block_number") or 0)
        if not tx_hash or block_number <= 0:
            raise AssertionError(f"Mission 3 Dog #{dog_id} winner row is missing settlement tx/block")
        prior_block = settlement_blocks_by_tx.get(tx_hash)
        if prior_block is not None and prior_block != block_number:
            raise AssertionError(f"Mission 3 settlement transaction {tx_hash} maps to conflicting blocks")
        settlement_blocks_by_tx[tx_hash] = block_number

    for dog_id, timeline_row in timeline.items():
        historical_row = historical[dog_id]
        unified_row = unified[dog_id]
        expected_status = mission3_expected_status(timeline_row.get("auction_state"))
        historical_status = mission3_expected_status(historical_row.get("status"))
        unified_status = mission3_expected_status(unified_row.get("status"))
        if historical_status != expected_status or unified_status != expected_status:
            raise AssertionError(
                f"Mission 3 Dog #{dog_id} status parity failed: "
                f"timeline={expected_status!r} historical={historical_status!r} unified={unified_status!r}"
            )

        raw_stats = unified_row.get("bid_stats")
        stats = raw_stats if isinstance(raw_stats, dict) else {}
        expected_bids = int(timeline_row.get("bids") or 0)
        expected_unique = int(timeline_row.get("unique_bidders") or 0)
        if int(historical_row.get("bid_count") or 0) != expected_bids or int(stats.get("bid_count") or 0) != expected_bids:
            raise AssertionError(f"Mission 3 Dog #{dog_id} bid_count differs across timeline/history/unified")
        if int(historical_row.get("unique_bidder_count") or 0) != expected_unique or int(stats.get("unique_bidder_count") or 0) != expected_unique:
            raise AssertionError(f"Mission 3 Dog #{dog_id} unique_bidder_count differs across timeline/history/unified")

        raw_created = unified_row.get("auction_created")
        created = raw_created if isinstance(raw_created, dict) else {}
        expected_created_time = iso_utc(timeline_row.get("start_time_utc"))
        if iso_utc(historical_row.get("auction_created_time_utc")) != expected_created_time:
            raise AssertionError(f"Mission 3 Dog #{dog_id} created time differs between timeline and history")
        if iso_utc(created.get("block_time_utc")) != expected_created_time:
            raise AssertionError(f"Mission 3 Dog #{dog_id} created time differs between timeline and unified")
        created_tx = str(timeline_row.get("created_tx_hash") or "")
        if not created_tx:
            raise AssertionError(f"Mission 3 Dog #{dog_id} timeline is missing created transaction")
        if str(created.get("tx_hash") or "").lower() != created_tx.lower():
            raise AssertionError(f"Mission 3 Dog #{dog_id} created transaction missing or stale in unified archive")
        source_created_block = (source.get(dog_id) or {}).get("auction_created_block")
        tx_derived_block = settlement_blocks_by_tx.get(created_tx.lower())
        if tx_derived_block and source_created_block not in (None, "") and tx_derived_block != int(source_created_block):
            raise AssertionError(f"Mission 3 Dog #{dog_id} tx-derived created block differs from Mission 3 source")
        expected_created_block = tx_derived_block or int(source_created_block or 0)
        if expected_created_block <= 0:
            raise AssertionError(f"Mission 3 Dog #{dog_id} created transaction has no verified block mapping")
        if int(created.get("block_number") or 0) != expected_created_block:
            raise AssertionError(f"Mission 3 Dog #{dog_id} created block differs from verified onchain mapping")

        raw_amount = unified_row.get("amount")
        amount = raw_amount if isinstance(raw_amount, dict) else {}
        raw_who = unified_row.get("winner_or_high_bidder")
        who = raw_who if isinstance(raw_who, dict) else {}
        if expected_status == "settled":
            winner = winners[dog_id]
            if int(winner.get("bid_count") or 0) != expected_bids:
                raise AssertionError(f"Mission 3 Dog #{dog_id} bid_count differs between timeline and winner table")
            if int(winner.get("unique_bidders") or 0) != expected_unique:
                raise AssertionError(f"Mission 3 Dog #{dog_id} unique bidders differ between timeline and winner table")
            expected_wallet = str(winner.get("winner_wallet") or "").lower()
            if str(historical_row.get("winner_wallet") or "").lower() != expected_wallet:
                raise AssertionError(f"Mission 3 Dog #{dog_id} winner wallet differs between history and winner table")
            if str(who.get("wallet") or "").lower() != expected_wallet:
                raise AssertionError(f"Mission 3 Dog #{dog_id} winner wallet differs between unified and winner table")
            expected_native_amount = decimal_value(winner.get("winning_bid_eth"))
            if historical_native(historical_row.get("amount")) != expected_native_amount or decimal_value(amount.get("native")) != expected_native_amount:
                raise AssertionError(f"Mission 3 Dog #{dog_id} winning amount differs across history/winner/unified")
            raw_settlement = unified_row.get("settlement")
            settlement = raw_settlement if isinstance(raw_settlement, dict) else {}
            expected_settlement_time = iso_utc(winner.get("settled_time_utc"))
            if iso_utc(timeline_row.get("settled_time_utc")) != expected_settlement_time:
                raise AssertionError(f"Mission 3 Dog #{dog_id} settlement time differs between timeline and winner table")
            if iso_utc(historical_row.get("settled_time_utc")) != expected_settlement_time or iso_utc(settlement.get("block_time_utc")) != expected_settlement_time:
                raise AssertionError(f"Mission 3 Dog #{dog_id} settlement time differs across history/winner/unified")
            expected_settlement_tx = str(winner.get("tx_hash") or "").lower()
            if str(timeline_row.get("settled_tx_hash") or "").lower() != expected_settlement_tx:
                raise AssertionError(f"Mission 3 Dog #{dog_id} settlement transaction differs between timeline and winner table")
            if str(settlement.get("tx_hash") or "").lower() != expected_settlement_tx:
                raise AssertionError(f"Mission 3 Dog #{dog_id} settlement transaction missing or stale in unified archive")
            if int(settlement.get("block_number") or 0) != int(winner.get("block_number") or 0):
                raise AssertionError(f"Mission 3 Dog #{dog_id} settlement block differs between unified and winner table")
        else:
            expected_native_amount = historical_native(historical_row.get("amount"))
            if decimal_value(amount.get("native")) != expected_native_amount:
                raise AssertionError(f"Mission 3 Dog #{dog_id} live amount differs between history and unified")

        sources = (unified_row.get("source") or {}).get("sources") if isinstance(unified_row.get("source"), dict) else []
        if "generated_auction_timeline" not in (sources or []):
            raise AssertionError(f"Mission 3 Dog #{dog_id} unified provenance omits generated_auction_timeline")
        if expected_status == "settled" and "generated_auction_winners" not in (sources or []):
            raise AssertionError(f"Mission 3 Dog #{dog_id} unified provenance omits generated_auction_winners")


def parse_rarity_display(value: object, *, label: str) -> tuple[int, int]:
    text = str(value or "").strip()
    if not text.startswith("#") or "/" not in text:
        raise AssertionError(f"{label} has invalid rarity display {text!r}")
    rank_text, total_text = text[1:].split("/", 1)
    try:
        return int(rank_text), int(total_text)
    except ValueError as exc:
        raise AssertionError(f"{label} has invalid rarity display {text!r}") from exc


def assert_exact_rarity_permutation(rows: list[dict[str, str]], total_supply: int) -> None:
    ranks: list[int] = []
    for row in rows:
        token_id = int(row["token_id"])
        rank, total = parse_rarity_display(row.get("rarity"), label=f"Dog #{token_id}")
        if total != total_supply:
            raise AssertionError(f"Dog #{token_id} rarity total {total} != dog_total_supply {total_supply}")
        ranks.append(rank)
    ordered = sorted(ranks)
    previous: int | None = None
    for position, rank in enumerate(ordered, start=1):
        if rank < 1 or rank > total_supply:
            raise AssertionError(f"historical rarity rank {rank} is outside 1..{total_supply}")
        if rank != previous and rank != position:
            raise AssertionError(
                f"historical rarity has invalid competition rank {rank} at position {position}"
            )
        previous = rank


def assert_withheld_rarity(row: dict[str, object], *, label: str) -> None:
    raw_rarity = row.get("rarity")
    score_values: list[object] = []
    if isinstance(raw_rarity, dict):
        if str(raw_rarity.get("display") or "").strip() != "Unavailable":
            raise AssertionError(f"{label} exposes a partial rarity display")
        for key in ("rank", "total"):
            if raw_rarity.get(key) not in (None, ""):
                raise AssertionError(f"{label} exposes a partial rarity {key}")
        if "score" in raw_rarity:
            score_values.append(raw_rarity.get("score"))
    elif str(raw_rarity or "").strip() != "Unavailable":
        raise AssertionError(f"{label} exposes a partial rarity display")
    if "rarity_score" in row:
        score_values.append(row.get("rarity_score"))
    for raw_score in score_values:
        if raw_score in (None, ""):
            continue
        raise AssertionError(f"{label} exposes a partial rarity score")


def assert_metadata_rarity_state(
    rows: list[dict[str, str]],
    total_supply: int,
    metrics: dict[str, str],
) -> str:
    if metrics.get("dog_token_uri_verification_status") not in {
        "hash_pinned_cross_provider_exact_outcome_quorum",
        "baseline_hash_pinned_quorum_plus_cross_provider_rarity_event_continuity",
    }:
        raise AssertionError("dog tokenURI outcomes are not hash-pinned and cross-provider verified")
    if metrics.get("dog_base_existence_verification_status") not in {
        "hash_pinned_cross_provider_exists_token_uri_parity_quorum",
        "baseline_exists_token_uri_quorum_plus_cross_provider_rarity_event_continuity",
    }:
        raise AssertionError("dog Base existence/tokenURI parity is not cross-provider verified")

    count_keys = (
        "dog_token_uri_present_count",
        "dog_token_uri_unavailable_count",
        "dog_metadata_onchain_verified_count",
        "dog_metadata_unavailable_count",
        "dog_rarity_universe_count",
        "dog_rarity_excluded_nonexistent_count",
        "dog_rarity_incomplete_metadata_count",
    )
    counts: dict[str, int] = {}
    for key in count_keys:
        raw = str(metrics.get(key) or "")
        if not raw.isdigit():
            raise AssertionError(f"mission3 metric {key} is not a non-negative integer")
        counts[key] = int(raw)
    token_present = counts["dog_token_uri_present_count"]
    token_unavailable = counts["dog_token_uri_unavailable_count"]
    metadata_verified = counts["dog_metadata_onchain_verified_count"]
    metadata_unavailable = counts["dog_metadata_unavailable_count"]
    rarity_universe = counts["dog_rarity_universe_count"]
    rarity_excluded = counts["dog_rarity_excluded_nonexistent_count"]
    rarity_incomplete = counts["dog_rarity_incomplete_metadata_count"]
    if token_present + token_unavailable != total_supply:
        raise AssertionError("dog tokenURI aggregate counts do not equal dog_total_supply")
    if metadata_verified + metadata_unavailable != total_supply:
        raise AssertionError("dog metadata aggregate counts do not equal dog_total_supply")
    if metadata_unavailable < token_unavailable:
        raise AssertionError("dog metadata aggregates hide tokenURI-unavailable Dogs")
    if rarity_universe != metadata_verified:
        raise AssertionError("dog rarity universe differs from verified Base metadata")
    if rarity_excluded != token_unavailable:
        raise AssertionError("dog rarity exclusions differ from nonexistent Base tokens")
    if rarity_incomplete != metadata_unavailable - token_unavailable:
        raise AssertionError("dog rarity incomplete count contradicts metadata outcomes")
    if metrics.get("dog_rarity_scope") != "base_existing":
        raise AssertionError("dog rarity scope is not Base-existing")

    expected_status = (
        "complete_onchain_token_uri_verified"
        if metadata_unavailable == 0
        else "partial_onchain_token_uri_unavailable"
        if metadata_unavailable == token_unavailable
        else "incomplete_metadata_unavailable"
    )
    if metrics.get("dog_metadata_verification_status") != expected_status:
        raise AssertionError("dog metadata aggregate status contradicts its counts")

    row_statuses = Counter(str(row.get("metadata_verification_status") or "") for row in rows)
    allowed_statuses = {
        "onchain_token_uri_verified",
        "onchain_token_uri_unavailable",
        "unavailable",
    }
    unexpected_statuses = sorted(set(row_statuses).difference(allowed_statuses))
    if unexpected_statuses:
        raise AssertionError(f"historical metadata rows contain unexpected statuses: {unexpected_statuses}")
    if row_statuses["onchain_token_uri_verified"] != metadata_verified:
        raise AssertionError("historical metadata verified rows differ from mission3 metrics")
    if row_statuses["onchain_token_uri_unavailable"] != token_unavailable:
        raise AssertionError("historical tokenURI-unavailable rows differ from mission3 metrics")
    if len(rows) - row_statuses["onchain_token_uri_verified"] != metadata_unavailable:
        raise AssertionError("historical metadata unavailable rows differ from mission3 metrics")

    rarity_complete = rarity_universe > 0 and rarity_incomplete == 0
    expected_rarity_status = (
        "complete_verified_existing_token_universe"
        if rarity_complete
        else "unavailable_no_verified_existing_tokens"
        if rarity_universe == 0
        else "incomplete_existing_token_metadata"
    )
    if metrics.get("dog_rarity_verification_status") != expected_rarity_status:
        raise AssertionError("dog rarity aggregate status contradicts its counts")

    if rarity_complete:
        verified_rows = [
            row
            for row in rows
            if row.get("metadata_verification_status") == "onchain_token_uri_verified"
        ]
        assert_exact_rarity_permutation(verified_rows, rarity_universe)
        for row in verified_rows:
            score = decimal_value(row.get("rarity_score"))
            if score is None or score <= 0:
                raise AssertionError(
                    f"Dog #{int(row['token_id'])} has no numeric rarity score"
                )
        for row in rows:
            if row.get("metadata_verification_status") != "onchain_token_uri_verified":
                assert_withheld_rarity(row, label=f"Dog #{int(row['token_id'])}")
        return "complete"

    # Any unresolved metadata inside the Base-existing scope makes subset ranks
    # misleading, so every surface must withhold rank and score.
    for row in rows:
        assert_withheld_rarity(row, label=f"Dog #{int(row['token_id'])}")
    return "unavailable"


def rarity_display_from_archive(row: dict[str, object]) -> str:
    raw = row.get("rarity")
    if isinstance(raw, dict):
        return str(raw.get("display") or "")
    return str(raw or "")


DASHBOARD_ARCHIVE_MARKERS = [
    'data-table="auction_feed"',
    "fetchGenerated('unified_dog_search_index',target.block)",
    'generatedUrls=(name,version)=>',
    "fetch(url,{cache:'no-store'",
    'missionMatch=remaining.match',
    'dogMatch=remaining.match',
    'restoreAuctionRows=()=>{archiveState.query=',
    'Search all missions: Dog #, wallet, handle, tx, chain, status',
    'Latest 10 archive records',
    'data-mission-filter="1"',
    'id="auction-page-size"',
    '<option value="highest_usd">Highest USD bid</option>',
    'getUsdSortValue=record=>',
    'Missing estimates sort last.',
]


def assert_dashboard_archive_wiring(html: str) -> None:
    for marker in DASHBOARD_ARCHIVE_MARKERS:
        if marker not in html:
            raise AssertionError(f"index.html missing {marker}")


def expected_report_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    statuses = [(row.get("status") or "").lower() for row in rows]
    return {
        "dogs": len(rows),
        "settled": sum(1 for status in statuses if status == "settled" or (status.startswith("settled") and "unsettled" not in status)),
        "live_or_unsettled": sum(1 for status in statuses if "live" in status or "ongoing" in status or "unsettled" in status or "pending settlement" in status or "created" in status),
        "metadata_only": sum(1 for status in statuses if status == "metadata_only"),
        "bid_count": sum(int_field(row, "bid_count") for row in rows),
    }


def assert_report_counts(report: dict[str, str], rows: list[dict[str, str]]) -> None:
    expected = expected_report_counts(rows)
    for key, value in expected.items():
        actual = int_field(report, key)
        if actual != value:
            raise AssertionError(f"historical_dog_report mission {report.get('mission')} {key} {actual} != recomputed {value}")


def main() -> int:
    total_supply = int(read_metric("dog_total_supply"))
    rarity_metrics = {
        key: read_metric(key)
        for key in (
            "dog_token_uri_verification_status",
            "dog_base_existence_verification_status",
            "dog_token_uri_present_count",
            "dog_token_uri_unavailable_count",
            "dog_metadata_verification_status",
            "dog_metadata_onchain_verified_count",
            "dog_metadata_unavailable_count",
            "dog_rarity_verification_status",
            "dog_rarity_universe_count",
            "dog_rarity_excluded_nonexistent_count",
            "dog_rarity_incomplete_metadata_count",
            "dog_rarity_scope",
        )
    }
    generated_rows = read_csv(ROOT / "generated" / "historical_dog_search.csv")
    public_rows = read_csv(ROOT / "public" / "generated" / "historical_dog_search.csv")
    report_rows = read_csv(ROOT / "generated" / "historical_dog_report.csv")

    if len(generated_rows) != total_supply:
        raise AssertionError(f"historical_dog_search rows {len(generated_rows)} != dog_total_supply {total_supply}")
    if len(public_rows) != len(generated_rows):
        raise AssertionError("public historical_dog_search row count differs from generated")
    if not REQUIRED_COLUMNS.issubset(generated_rows[0].keys()):
        missing = sorted(REQUIRED_COLUMNS - set(generated_rows[0].keys()))
        raise AssertionError(f"historical_dog_search missing columns: {missing}")

    ids = {int(row["token_id"]): row for row in generated_rows}
    expected_ids = set(range(total_supply))
    if set(ids) != expected_ids:
        missing = sorted(expected_ids - set(ids))[:20]
        extra = sorted(set(ids) - expected_ids)[:20]
        raise AssertionError(f"historical_dog_search token coverage mismatch missing={missing} extra={extra}")
    rarity_state = assert_metadata_rarity_state(generated_rows, total_supply, rarity_metrics)

    missions = {row["mission"] for row in generated_rows}
    if not {"1", "2", "3"}.issubset(missions):
        raise AssertionError(f"historical_dog_search mission coverage incomplete: {sorted(missions)}")
    for token_id in [0, 201, 590, total_supply - 1]:
        row = ids[token_id]
        if row.get("dog") != f"Dog #{token_id}":
            raise AssertionError(f"Dog #{token_id} label mismatch: {row.get('dog')}")
        if f"/{token_id}" not in row.get("dog_opensea_url", ""):
            raise AssertionError(f"Dog #{token_id} missing exact OpenSea URL")
        if f"dog #{token_id}" not in row.get("search_text", "").lower():
            raise AssertionError(f"Dog #{token_id} not included in search_text")

    report_by_mission = {row.get("mission"): row for row in report_rows}
    for mission in ["all", "1", "2", "3"]:
        if mission not in report_by_mission:
            raise AssertionError(f"historical_dog_report missing mission {mission}")
    if int(report_by_mission["all"].get("dogs", "0")) != total_supply:
        raise AssertionError("historical_dog_report all row must equal dog_total_supply")
    assert_report_counts(report_by_mission["all"], generated_rows)
    for mission in ["1", "2", "3"]:
        assert_report_counts(report_by_mission[mission], [row for row in generated_rows if row.get("mission") == mission])

    manifest = read_csv(ROOT / "generated" / "manifest.csv")
    manifest_rows = {row["table"]: row for row in manifest}
    for table_name, row_count in [("historical_dog_search", total_supply), ("historical_dog_report", len(report_rows))]:
        row = manifest_rows.get(table_name)
        if not row:
            raise AssertionError(f"manifest missing {table_name}")
        if int(row.get("rows", "-1")) != row_count:
            raise AssertionError(f"manifest {table_name} rows {row.get('rows')} != {row_count}")
        assert_json_matches_csv(ROOT / "generated" / f"{table_name}.csv", ROOT / "generated" / f"{table_name}.json", row_count)
        assert_json_matches_csv(ROOT / "public" / "generated" / f"{table_name}.csv", ROOT / "public" / "generated" / f"{table_name}.json", row_count)
        assert_artifact_pair_matches(ROOT / "generated" / f"{table_name}.csv", ROOT / "public" / "generated" / f"{table_name}.csv")
        assert_artifact_pair_matches(ROOT / "generated" / f"{table_name}.json", ROOT / "public" / "generated" / f"{table_name}.json")

    unified_archive_path = ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json"
    unified_public_path = ROOT / "public" / "generated" / "unified_dog_search_index.json"
    if not unified_archive_path.exists() or not unified_public_path.exists():
        raise AssertionError("missing unified dog search index artifacts")
    unified_archive = json.loads(unified_archive_path.read_text(encoding="utf-8"))
    unified_public = json.loads(unified_public_path.read_text(encoding="utf-8"))
    if not isinstance(unified_archive, list) or not isinstance(unified_public, list):
        raise AssertionError("unified dog search indexes must be JSON lists")
    if unified_archive != unified_public:
        raise AssertionError("public unified dog search index differs from archive copy")
    if len(unified_archive) < 700:
        raise AssertionError(f"unified dog search index unexpectedly small: {len(unified_archive)}")
    unified_missions = {str(row.get("mission")) for row in unified_archive if isinstance(row, dict)}
    if not {"1", "2", "3"}.issubset(unified_missions):
        raise AssertionError(f"unified dog search mission coverage incomplete: {sorted(unified_missions)}")
    timeline_rows = read_json_list(ROOT / "generated" / "auction_timeline.json")
    winner_rows = read_json_list(ROOT / "generated" / "auction_winners.json")
    if rarity_state == "unavailable":
        rarity_surfaces = {
            "current auction": read_json_list(ROOT / "generated" / "current_auction.json"),
            "current latest bid": read_json_list(ROOT / "generated" / "current_latest_bid.json"),
            "auction feed": read_json_list(ROOT / "generated" / "auction_feed.json"),
            "recent auction winners": read_json_list(ROOT / "generated" / "recent_auction_winners.json"),
            "auction timeline": timeline_rows,
            "auction winners": winner_rows,
        }
        for surface, surface_rows in rarity_surfaces.items():
            for index, row in enumerate(surface_rows):
                assert_withheld_rarity(row, label=f"{surface} row {index}")
    mission3_source_rows = read_json_list(
        ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_dog_search_index.json"
    )
    assert_mission3_onchain_parity(
        unified_archive,
        generated_rows,
        timeline_rows,
        winner_rows,
        mission3_source_rows,
    )
    for row in unified_archive:
        if not isinstance(row, dict):
            continue
        token_id = int(row.get("dog_id", -1))
        if token_id not in ids:
            continue
        if rarity_state == "unavailable":
            assert_withheld_rarity(row, label=f"unified Dog #{token_id}")
        unified_rarity = rarity_display_from_archive(row)
        if unified_rarity != ids[token_id].get("rarity"):
            raise AssertionError(
                f"unified Dog #{token_id} rarity {unified_rarity!r} differs from historical rarity {ids[token_id].get('rarity')!r}"
            )
        raw_unified_rarity = row.get("rarity") if isinstance(row.get("rarity"), dict) else {}
        if unified_rarity.startswith("#") and raw_unified_rarity.get("scope") != "base_existing":
            raise AssertionError(f"unified Dog #{token_id} numeric rarity has no Base-existing scope")
    dog_archive = ROOT / "archive" / "dogs" / "by-id"
    if dog_archive.exists():
        for path in dog_archive.glob("*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = payload.get("record") if isinstance(payload, dict) else None
            if not isinstance(record, dict):
                raise AssertionError(f"{path.relative_to(ROOT)} missing record object")
            token_id = int(record.get("dog_id", path.stem))
            if token_id not in ids:
                continue
            if rarity_state == "unavailable":
                assert_withheld_rarity(record, label=f"by-id Dog #{token_id}")
            archive_rarity = rarity_display_from_archive(record)
            if archive_rarity != ids[token_id].get("rarity"):
                raise AssertionError(
                    f"by-id Dog #{token_id} rarity {archive_rarity!r} differs from historical rarity {ids[token_id].get('rarity')!r}"
                )
            raw_archive_rarity = record.get("rarity") if isinstance(record.get("rarity"), dict) else {}
            if archive_rarity.startswith("#") and raw_archive_rarity.get("scope") != "base_existing":
                raise AssertionError(f"by-id Dog #{token_id} numeric rarity has no Base-existing scope")
    unified_mission3 = indexed_token_rows(unified_archive, label="unified Mission 3 by-id parity", mission=3)
    for token_id, expected_record in unified_mission3.items():
        path = dog_archive / f"{token_id:03d}.json"
        if not path.exists():
            raise AssertionError(f"missing Mission 3 by-id artifact {path.relative_to(ROOT)}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = payload.get("record") if isinstance(payload, dict) else None
        if record != expected_record:
            raise AssertionError(f"Mission 3 by-id Dog #{token_id} differs from unified archive record")
    for row in unified_archive[:10]:
        search_text = str(row.get("search_text") or "").lower()
        if f"dog #{row.get('dog_id')}" not in search_text or f"mission {row.get('mission')}" not in search_text:
            raise AssertionError("unified search row missing dog/mission terms")

    auction_feed = json.loads((ROOT / "generated" / "auction_feed.json").read_text(encoding="utf-8"))
    current_feed = next((row for row in auction_feed if isinstance(row, dict) and str(row.get("status") or "").lower() in {"ongoing", "live"}), None)
    if current_feed:
        current_dog_id = dog_id_from_feed(current_feed)
        current_unified = next((row for row in unified_archive if isinstance(row, dict) and row.get("mission") == 3 and row.get("dog_id") == current_dog_id), None)
        if not current_unified:
            raise AssertionError(f"unified index missing current Mission 3 Dog #{current_dog_id}")
        assert isinstance(current_unified, dict)
        raw_who = current_unified.get("winner_or_high_bidder")
        raw_amount = current_unified.get("amount")
        who = raw_who if isinstance(raw_who, dict) else {}
        amount = raw_amount if isinstance(raw_amount, dict) else {}
        if str(who.get("wallet") or "").lower() != str(current_feed.get("bidder_winner_wallet") or "").lower():
            raise AssertionError("unified current row high-bidder wallet differs from auction_feed")
        if str(who.get("display") or "") != str(current_feed.get("bidder_winner") or ""):
            raise AssertionError("unified current row display differs from auction_feed")
        if expected_native(amount.get("native")) != expected_native(current_feed.get("amount_eth")):
            raise AssertionError("unified current row amount differs from auction_feed")
        if iso_utc(current_unified.get("activity_time_utc")) != iso_utc(current_feed.get("last_bid_utc") or current_feed.get("auction_time_utc")):
            raise AssertionError("unified current row last-bid time differs from auction_feed")

    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert_dashboard_archive_wiring(html)
    for retired_marker in ['data-table="historical_dog_search"', 'data-table="historical_dog_report"']:
        if retired_marker in html:
            raise AssertionError(f"index.html still renders separate archive table {retired_marker}")

    print(json.dumps({"historical_dog_search_rows": len(generated_rows), "report_rows": len(report_rows), "unified_search_rows": len(unified_archive)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
