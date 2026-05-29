#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
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
    "confidence",
    "search_text",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise AssertionError(f"missing {path.relative_to(ROOT)}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


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
    text = str(value or "").strip()
    return text.rstrip("0").rstrip(".") if "." in text else text


def expected_report_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    statuses = [(row.get("status") or "").lower() for row in rows]
    return {
        "dogs": len(rows),
        "settled": sum(1 for status in statuses if status == "settled" or (status.startswith("settled") and "unsettled" not in status)),
        "live_or_unsettled": sum(1 for status in statuses if "live" in status or "ongoing" in status or "unsettled" in status or "created" in status),
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
    if unified_archive != unified_public:
        raise AssertionError("public unified dog search index differs from archive copy")
    if len(unified_archive) < 700:
        raise AssertionError(f"unified dog search index unexpectedly small: {len(unified_archive)}")
    unified_missions = {str(row.get("mission")) for row in unified_archive if isinstance(row, dict)}
    if not {"1", "2", "3"}.issubset(unified_missions):
        raise AssertionError(f"unified dog search mission coverage incomplete: {sorted(unified_missions)}")
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
    for marker in [
        'data-table="auction_feed"',
        'generated/unified_dog_search_index.json',
        "'/generated/unified_dog_search_index.json'",
        "fetch(url,{cache:'no-store'})",
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
    ]:
        if marker not in html:
            raise AssertionError(f"index.html missing {marker}")
    for retired_marker in ['data-table="historical_dog_search"', 'data-table="historical_dog_report"']:
        if retired_marker in html:
            raise AssertionError(f"index.html still renders separate archive table {retired_marker}")

    print(json.dumps({"historical_dog_search_rows": len(generated_rows), "report_rows": len(report_rows), "unified_search_rows": len(unified_archive)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
