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

    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for marker in [
        'data-table="historical_dog_search"',
        'data-table="historical_dog_report"',
        'data-search=',
        'Search auctions, usernames, dogs, traits, wallets',
    ]:
        if marker not in html:
            raise AssertionError(f"index.html missing {marker}")

    print(json.dumps({"historical_dog_search_rows": len(generated_rows), "report_rows": len(report_rows)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
