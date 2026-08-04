#!/usr/bin/env python3
"""Validate that live/current auction artifacts agree across dashboard surfaces."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import re
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x0000000000000000000000000000000000000000"
RECENT_BIDS = ROOT / "generated" / "recent_bids.json"
REFRESH_TELEMETRY_PATH = ROOT / "scripts" / "refresh_telemetry.py"
RARITY_TRAIT_TYPES = ("Background", "Body", "Neck", "Mouth", "Ears", "Head", "Eyes")
getcontext().prec = 80


def load_json(path: Path, default: Any | None = None) -> Any:
    if default is None:
        default = []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_refresh_telemetry() -> Any:
    spec = importlib.util.spec_from_file_location("refresh_telemetry", REFRESH_TELEMETRY_PATH)
    if not spec or not spec.loader:
        raise AssertionError("unable to load scripts/refresh_telemetry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_address(value: Any) -> str:
    raw = text(value).lower()
    return raw if raw.startswith("0x") and len(raw) == 42 else ""


def short_address(value: Any) -> str:
    address = normalize_address(value)
    return f"{address[:6]}…{address[-4:]}" if address else ""


def dog_id(row: dict[str, Any]) -> int:
    for key in ("token_id", "dog_id"):
        value = row.get(key)
        if value not in (None, ""):
            return int(value)
    label = text(row.get("dog") or row.get("dog_name"))
    digits = "".join(ch if ch.isdigit() else " " for ch in label).split()
    if not digits:
        raise AssertionError(f"unable to derive Dog id from row: {row}")
    return int(digits[-1])


def decimal_value(value: Any) -> Decimal:
    raw = text(value).replace(",", "")
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise AssertionError(f"invalid decimal value {value!r}") from exc


def optional_decimal_value(value: Any) -> Decimal | None:
    raw = text(value).replace(",", "")
    if not raw or raw.upper() == "N/A":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def quantized_decimal_str(value: Decimal, places: int) -> str:
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}".rstrip("0").rstrip(".") or "0"


def money_display(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"


def reward_apr_display(apr: Decimal | None) -> str:
    if apr is None or apr <= 0:
        return "N/A"
    rounded = apr.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"≈{rounded:,.0f}% APR"


def decimals_equal(left: Any, right: Any) -> bool:
    return decimal_value(left) == decimal_value(right)


def optional_decimals_equal(left: Any, right: Any) -> bool:
    return optional_decimal_value(left) == optional_decimal_value(right)


def first_optional_decimal(*values: Any) -> Decimal | None:
    for value in values:
        parsed = optional_decimal_value(value)
        if parsed is not None:
            return parsed
    return None


def iso_utc(value: Any) -> str:
    raw = text(value)
    if not raw:
        return ""
    raw = raw.replace(" ", "T")
    return raw if raw.endswith("Z") else f"{raw}Z"


def archive_current_rank(row: dict[str, Any]) -> int:
    status = text(row.get("status")).lower()
    return 1 if status == "live" or "ongoing" in status else 0


def unified_sort_key(row: dict[str, Any]) -> tuple[int, str, int]:
    return (archive_current_rank(row), iso_utc(row.get("activity_time_utc")), int(row.get("dog_id") or -1))


def historical_mission3_required_ids(rows: list[Any]) -> set[int]:
    required_statuses = {"settled", "live", "ongoing", "ended_unsettled", "live_or_unsettled", "ended pending settlement"}
    required: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or int(row.get("mission") or 0) != 3:
            continue
        status = text(row.get("status")).lower()
        if status in required_statuses:
            required.add(dog_id(row))
    return required


def parse_rarity_traits(value: Any, token_id: int) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in text(value).split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise AssertionError(f"Dog #{token_id} has a malformed rarity trait")
        trait_type, trait_value = (item.strip() for item in part.split(":", 1))
        if not trait_type or not trait_value or trait_type in attributes:
            raise AssertionError(f"Dog #{token_id} has an invalid or duplicate rarity trait")
        attributes[trait_type] = trait_value
    if set(attributes) != set(RARITY_TRAIT_TYPES):
        raise AssertionError(f"Dog #{token_id} rarity traits do not match the canonical schema")
    return {trait_type: attributes[trait_type] for trait_type in RARITY_TRAIT_TYPES}


def validate_base_rarity_universe(rows: Any, metrics: dict[str, str]) -> dict[int, Decimal]:
    if not isinstance(rows, list):
        raise AssertionError("historical_dog_search is not a list")
    # The Base claim contract reuses the collection-wide Dog IDs. Claimed
    # Mission 1 and Mission 2 Dogs therefore live in the same verified Base
    # rarity universe as newly auctioned Mission 3 Dogs, even though the
    # historical search index retains each Dog's original mission label.
    indexed_rows = [row for row in rows if isinstance(row, dict)]
    id_ceiling = int(metrics["dog_id_ceiling"])
    by_token: dict[int, dict[str, Any]] = {}
    for row in indexed_rows:
        token_id = dog_id(row)
        if token_id in by_token:
            raise AssertionError(f"historical_dog_search repeats Mission 3 Dog #{token_id}")
        by_token[token_id] = row
    if set(by_token) != set(range(id_ceiling)):
        raise AssertionError("historical_dog_search Dog IDs do not equal the verified Base ID ceiling")

    verified = {
        token_id: row
        for token_id, row in by_token.items()
        if text(row.get("metadata_verification_status")) == "onchain_token_uri_verified"
    }
    nonexistent = {
        token_id: row
        for token_id, row in by_token.items()
        if text(row.get("metadata_verification_status")) == "onchain_token_uri_unavailable"
    }
    unresolved = set(by_token).difference(verified, nonexistent)
    universe_size = int(metrics["dog_rarity_universe_count"])
    excluded_size = int(metrics["dog_rarity_excluded_nonexistent_count"])
    incomplete_size = int(metrics["dog_rarity_incomplete_metadata_count"])
    if len(verified) != universe_size or len(nonexistent) != excluded_size or len(unresolved) != incomplete_size:
        raise AssertionError("historical_dog_search rarity coverage contradicts mission3_metrics")
    verified_ids_sha256 = hashlib.sha256(
        ",".join(str(token_id) for token_id in sorted(verified)).encode("ascii")
    ).hexdigest()
    nonexistent_ids_sha256 = hashlib.sha256(
        ",".join(str(token_id) for token_id in sorted(nonexistent)).encode("ascii")
    ).hexdigest()
    if verified_ids_sha256 != metrics["dog_base_existing_token_ids_sha256"]:
        raise AssertionError("historical_dog_search verified IDs differ from Base existence attestation")
    if nonexistent_ids_sha256 != metrics["dog_base_unclaimed_token_ids_sha256"]:
        raise AssertionError("historical_dog_search nonexistent IDs differ from Base existence attestation")

    rarity_complete = metrics["dog_rarity_verification_status"] == "complete_verified_existing_token_universe"
    if not rarity_complete:
        for row in by_token.values():
            if text(row.get("rarity")) not in {"", "Unavailable"}:
                raise AssertionError("incomplete rarity universe published a rank")
            if text(row.get("rarity_score")):
                raise AssertionError("incomplete rarity universe published a numeric score")
        return {}

    attributes = {token_id: parse_rarity_traits(row.get("traits"), token_id) for token_id, row in verified.items()}
    counts: Counter[tuple[str, str]] = Counter()
    for values in attributes.values():
        counts.update(values.items())
    scores = {
        token_id: sum(
            (
                Decimal(universe_size) / Decimal(counts[(trait_type, trait_value)])
                for trait_type, trait_value in values.items()
            ),
            Decimal(0),
        )
        for token_id, values in attributes.items()
    }
    expected_ranks: dict[int, int] = {}
    previous_score: Decimal | None = None
    competition_rank = 0
    for position, token_id in enumerate(
        sorted(scores, key=lambda candidate: (-scores[candidate], candidate)),
        start=1,
    ):
        if previous_score is None or scores[token_id] != previous_score:
            competition_rank = position
            previous_score = scores[token_id]
        expected_ranks[token_id] = competition_rank

    for token_id, row in verified.items():
        expected_display = f"#{expected_ranks[token_id]}/{universe_size}"
        if text(row.get("rarity")) != expected_display:
            raise AssertionError(f"Dog #{token_id} rarity rank is inconsistent")
        if "rarity_score" in row:
            raw_score = text(row.get("rarity_score"))
            if not raw_score or abs(Decimal(raw_score) - scores[token_id]) > Decimal("0.000001"):
                raise AssertionError(f"Dog #{token_id} rarity score is inconsistent")
        expected_trait_rarity = "; ".join(
            f"{trait_type}: {trait_value} "
            f"({(Decimal(counts[(trait_type, trait_value)]) * Decimal(100) / Decimal(universe_size)):.1f}%)"
            for trait_type, trait_value in attributes[token_id].items()
        )
        if text(row.get("trait_rarity")) != expected_trait_rarity:
            raise AssertionError(f"Dog #{token_id} trait rarity percentages are inconsistent")

    for token_id in set(by_token).difference(verified):
        row = by_token[token_id]
        if text(row.get("rarity")) != "Unavailable":
            raise AssertionError(f"nonexistent/unresolved Dog #{token_id} received a rarity rank")
        if text(row.get("rarity_score")):
            raise AssertionError(f"nonexistent/unresolved Dog #{token_id} received a numeric rarity score")
        if text(row.get("trait_rarity")):
            raise AssertionError(f"nonexistent/unresolved Dog #{token_id} received trait frequencies")
    return scores


def first_row(path: Path) -> dict[str, Any]:
    data = load_json(path)
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise AssertionError(f"{path.relative_to(ROOT)} missing first object row")
    return data[0]


def load_metrics() -> dict[str, str]:
    path = ROOT / "generated" / "mission3_metrics.csv"
    json_path = ROOT / "generated" / "mission3_metrics.json"
    if not path.exists():
        raise AssertionError("generated/mission3_metrics.csv missing")
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    metrics = {text(row.get("metric")): text(row.get("value")) for row in rows}

    json_rows = load_json(json_path)
    if not isinstance(json_rows, list):
        raise AssertionError("generated/mission3_metrics.json is not a list")
    json_metrics = {text(row.get("metric")): text(row.get("value")) for row in json_rows if isinstance(row, dict)}
    if metrics != json_metrics:
        raise AssertionError("generated mission3_metrics CSV and JSON differ")
    public_json = ROOT / "public" / "generated" / "mission3_metrics.json"
    if public_json.exists() and public_json.read_bytes() != json_path.read_bytes():
        raise AssertionError("public/generated/mission3_metrics.json differs from generated/mission3_metrics.json")

    required = [
        "latest_block",
        "latest_block_time_utc",
        "onchain_verification_status",
        "onchain_verification_scope",
        "onchain_chain_id",
        "snapshot_block_hash",
        "snapshot_confirmations",
        "rpc_quorum_size",
        "rpc_quorum_agreement",
        "rpc_quorum_providers",
        "log_rpc_quorum_providers",
        "auction_house_code_sha256",
        "dog_nft_code_sha256",
        "dog_total_supply",
        "dog_id_ceiling",
        "dog_token_uri_verification_status",
        "dog_base_existence_verification_status",
        "dog_token_uri_present_count",
        "dog_token_uri_unavailable_count",
        "dog_base_existing_count",
        "dog_base_unclaimed_count",
        "dog_base_existing_token_ids_sha256",
        "dog_base_unclaimed_token_ids_sha256",
        "dog_metadata_verification_status",
        "dog_metadata_onchain_verified_count",
        "dog_metadata_unavailable_count",
        "dog_metadata_content_verification_status",
        "dog_metadata_content_observed_count",
        "dog_rarity_verification_status",
        "dog_rarity_universe_count",
        "dog_rarity_excluded_nonexistent_count",
        "dog_rarity_incomplete_metadata_count",
        "dog_rarity_scope",
        "dog_rarity_score_method",
        "dog_rarity_tie_policy",
        "dog_rarity_trait_schema",
        "dog_rarity_attested_block",
        "dog_rarity_attested_block_hash",
        "dog_rarity_continuity_through_block",
        "dog_rarity_continuity_through_block_hash",
        "dog_rarity_continuity_verification_status",
        "current_auction_token_id",
        "current_auction_status",
        "current_bid_eth",
        "current_bid_usd",
        "current_bidder",
        "current_bidder_wallet",
        "woof_usd_price",
        "sup_usd_price",
        "reward_basis_dogs",
        "reward_basis_source",
        "reward_snapshot_utc",
        "reward_observed_woof_flow_per_day",
        "reward_observed_sup_flow_per_day",
        "reward_observed_woof_per_dog_per_day",
        "reward_observed_sup_per_dog_per_day",
        "reward_woof_per_dog_per_day",
        "reward_sup_per_dog_per_day",
        "reward_total_per_dog_usd_per_day",
        "reward_current_bid_payback_days",
        "reward_current_bid_daily_roi_pct",
        "reward_current_bid_apr_pct",
        "reward_current_bid_apr_display",
    ]
    missing = [key for key in required if not metrics.get(key)]
    if missing:
        raise AssertionError("mission3_metrics missing required current-auction metrics: " + ", ".join(missing))
    return metrics


def readme_snapshot() -> dict[str, str]:
    path = ROOT / "README.md"
    if not path.exists():
        raise AssertionError("README.md missing")
    values: dict[str, str] = {}
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "## Current snapshot":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.startswith("|") and "---" not in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] != "Field":
                values[cells[0]] = cells[1]
    if not values:
        raise AssertionError("README current snapshot table missing or empty")
    return values


def assert_index_contains(label: str, expected: str, index: str) -> None:
    if expected and expected not in index:
        raise AssertionError(f"index.html missing {label}: {expected!r}")


def assert_metric_cell(metric: str, expected: str, index: str) -> None:
    cell = f"<td>{metric}</td><td>{expected}</td>"
    if cell not in index:
        raise AssertionError(f"index.html hidden mission3_metrics value mismatch for {metric}: expected {expected!r}")


def reward_strip_surface(index: str) -> str:
    match = re.search(r'<section\b[^>]*class="[^"]*\breward-strip\b[^"]*"[^>]*>.*?</section>', index, flags=re.DOTALL)
    return match.group(0) if match else ""


def season6_surface(index: str) -> str:
    reward_surface = reward_strip_surface(index)
    if "Season 6 SUP estimate" in reward_surface:
        return reward_surface
    match = re.search(r'<[^>]+class="[^"]*\bseason6-sup-estimate\b[^"]*"[^>]*>.*?</[^>]+>', index, flags=re.DOTALL)
    return match.group(0) if match else ""


def comma_decimal_display(value: Any, places: int = 0, prefix: str = "", suffix: str = "") -> str:
    decimal = optional_decimal_value(value)
    if decimal is None:
        return "N/A"
    quantized = decimal.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    rendered = f"{quantized:,.{places}f}" if places > 0 else f"{quantized:,.0f}"
    if places > 0:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{prefix}{rendered}{suffix}"


def season6_sup_display(value: Any, *, approximate: bool = True) -> str:
    rendered = comma_decimal_display(value, 0, suffix=" SUP")
    if rendered == "N/A":
        return rendered
    return f"≈{rendered}" if approximate else rendered


def season6_usd_display(value: Any) -> str:
    rendered = comma_decimal_display(value, 0, prefix="$")
    if rendered == "N/A":
        return rendered
    return f"≈{rendered}"


def season6_readme_estimate_summary(metrics: dict[str, str]) -> str:
    if text(metrics.get("season6_sup_enabled")).lower() not in {"true", "1", "yes"}:
        return ""
    status = text(metrics.get("season6_sup_estimate_status"))
    if status == "no_current_bid" or not normalize_address(metrics.get("season6_sup_current_bidder_wallet")):
        return "Bid to estimate S6 SUP"
    sup = season6_sup_display(metrics.get("season6_sup_current_bid_estimated_cap_aware_sup"))
    usd = season6_usd_display(metrics.get("season6_sup_current_bid_estimated_cap_aware_usd"))
    return f"{sup} / {usd}"


def identity_display(wallet: str) -> str:
    profiles = load_json(ROOT / "archive" / "data" / "identity" / "wallet_profiles.json", {})
    if isinstance(profiles, dict):
        profile = profiles.get(wallet.lower()) or profiles.get(wallet)
        if isinstance(profile, dict):
            return text(profile.get("display") or profile.get("farcaster_handle"))
    return short_address(wallet)


def archive_top_bid_for_dog(current_dog_id: int) -> dict[str, Any] | None:
    bids = load_json(ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_auction_bids.json")
    if not isinstance(bids, list):
        return None
    top: dict[str, Any] | None = None
    top_amount: Decimal | None = None
    top_time = ""
    for row in bids:
        if not isinstance(row, dict):
            continue
        try:
            row_dog_id = dog_id(row)
        except Exception:
            continue
        if row_dog_id != current_dog_id:
            continue
        amount = decimal_value(row.get("amount_eth") or row.get("native_amount") or row.get("bid_eth"))
        bid_time = text(row.get("block_time_utc"))
        if top is None or top_amount is None or amount > top_amount or (amount == top_amount and bid_time > top_time):
            top = row
            top_amount = amount
            top_time = bid_time
    return top


def _parity_int(value: Any, label: str, *, minimum: int = 0) -> int:
    raw = text(value)
    if isinstance(value, bool) or not re.fullmatch(r"0|[1-9][0-9]*", raw):
        raise AssertionError(f"{label} is not a canonical integer")
    parsed = int(raw)
    if parsed < minimum:
        raise AssertionError(f"{label} is below {minimum}")
    return parsed


def _parity_decimal(value: Any, label: str, *, empty_zero: bool = False) -> Decimal:
    raw = text(value)
    if not raw and empty_zero:
        return Decimal(0)
    if not raw:
        raise AssertionError(f"{label} is missing")
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", raw):
        raise AssertionError(f"{label} is not a canonical decimal")
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise AssertionError(f"{label} is not a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise AssertionError(f"{label} is not a finite non-negative decimal")
    return parsed


def _parity_timestamp(value: Any, label: str, *, allow_empty: bool = False) -> str:
    raw = text(value)
    if not raw:
        if allow_empty:
            return ""
        raise AssertionError(f"{label} is missing")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})?", raw):
        raise AssertionError(f"{label} is not a canonical timestamp")
    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AssertionError(f"{label} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parity_hash(value: Any, label: str, *, allow_empty: bool = False) -> str:
    normalized = text(value).lower()
    if not normalized and allow_empty:
        return ""
    if not re.fullmatch(r"0x[a-f0-9]{64}", normalized) or int(normalized[2:], 16) == 0:
        raise AssertionError(f"{label} is not a canonical transaction hash")
    return normalized


def _parity_address(value: Any, label: str) -> str:
    normalized = text(value).lower()
    if not re.fullmatch(r"0x[a-f0-9]{40}", normalized) or normalized == ZERO:
        raise AssertionError(f"{label} is not a canonical nonzero address")
    return normalized


def validate_mission3_archive_parity(*, root: Path = ROOT) -> dict[str, int | bool]:
    """Cross-check dashboard history against the independently quorum-built archive."""

    archive_root = root / "archive" / "mission3" / "data" / "generated"
    paths = {
        "dashboard_timeline": root / "generated" / "auction_timeline.json",
        "dashboard_winners": root / "generated" / "auction_winners.json",
        "archive_timeline": archive_root / "mission3_auction_timeline.json",
        "archive_winners": archive_root / "mission3_auction_winners.json",
        "archive_bids": archive_root / "mission3_auction_bids.json",
    }
    archive_expected = (root / "archive" / "mission3" / "config").is_dir() or archive_root.exists()
    if not archive_expected:
        return {"checked": False, "auctions": 0, "settlements": 0, "bids": 0}
    missing = [str(path.relative_to(root)) for path in paths.values() if not path.is_file()]
    if missing:
        raise AssertionError(f"Mission 3 archive parity inputs missing: {missing}")

    loaded = {name: load_json(path) for name, path in paths.items()}
    for name, rows in loaded.items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AssertionError(f"{paths[name].relative_to(root)} is not a list of objects")
    if not loaded["archive_timeline"]:
        raise AssertionError("Mission 3 archive timeline cannot be empty")

    def by_token(rows: list[dict[str, Any]], label: str) -> dict[int, dict[str, Any]]:
        indexed: dict[int, dict[str, Any]] = {}
        for row_number, row in enumerate(rows, 1):
            raw_token = row.get("token_id") if row.get("token_id") not in (None, "") else row.get("dog_id")
            if raw_token in (None, ""):
                raise AssertionError(f"{label} row {row_number} has no canonical Dog id")
            token = _parity_int(raw_token, f"{label} row {row_number} token id")
            if token in indexed:
                raise AssertionError(f"{label} contains duplicate Dog #{token}")
            indexed[token] = row
        return indexed

    dashboard_timeline = by_token(loaded["dashboard_timeline"], "dashboard auction_timeline")
    archive_timeline = by_token(loaded["archive_timeline"], "Mission 3 archive timeline")
    if set(dashboard_timeline) != set(archive_timeline):
        missing_dashboard = sorted(set(archive_timeline) - set(dashboard_timeline))
        missing_archive = sorted(set(dashboard_timeline) - set(archive_timeline))
        raise AssertionError(
            "Mission 3 timeline/archive Dog set differs: "
            f"missing_dashboard={missing_dashboard[:20]} missing_archive={missing_archive[:20]}"
        )

    archive_bid_totals: dict[int, Decimal] = {}
    archive_bid_counts: dict[int, int] = {}
    archive_bid_bidders: dict[int, set[str]] = {}
    archive_bid_highs: dict[int, Decimal] = {}
    archive_bid_time_ranges: dict[int, tuple[str, str]] = {}
    archive_latest_bids: dict[int, tuple[tuple[int, int], Decimal, str, str]] = {}
    archive_bid_identities: set[tuple[int, int]] = set()
    for row_number, row in enumerate(loaded["archive_bids"], 1):
        token = _parity_int(row.get("token_id"), f"archive bid row {row_number} token id")
        if token not in archive_timeline:
            raise AssertionError(f"Mission 3 archive bid references unknown Dog #{token}")
        amount = _parity_decimal(row.get("amount_eth"), f"Mission 3 Dog #{token} archive bid amount")
        if amount <= 0:
            raise AssertionError(f"Mission 3 Dog #{token} archive bid amount must be positive")
        bidder = _parity_address(row.get("bidder"), f"Mission 3 Dog #{token} archive bid bidder")
        block = _parity_int(row.get("block_number"), f"Mission 3 Dog #{token} archive bid block", minimum=1)
        log_index = _parity_int(row.get("log_index"), f"Mission 3 Dog #{token} archive bid log index")
        tx_hash = _parity_hash(row.get("transaction_hash"), f"Mission 3 Dog #{token} archive bid transaction")
        bid_time = _parity_timestamp(row.get("block_time_utc"), f"Mission 3 Dog #{token} archive bid time")
        identity = (block, log_index)
        if identity in archive_bid_identities:
            raise AssertionError(f"Mission 3 archive bids contain duplicate log {block}:{log_index}")
        archive_bid_identities.add(identity)
        archive_bid_totals[token] = archive_bid_totals.get(token, Decimal(0)) + amount
        archive_bid_counts[token] = archive_bid_counts.get(token, 0) + 1
        archive_bid_bidders.setdefault(token, set()).add(bidder)
        archive_bid_highs[token] = max(archive_bid_highs.get(token, Decimal(0)), amount)
        previous_range = archive_bid_time_ranges.get(token)
        archive_bid_time_ranges[token] = (
            min(previous_range[0], bid_time) if previous_range else bid_time,
            max(previous_range[1], bid_time) if previous_range else bid_time,
        )
        ordering = (block, log_index)
        previous_latest = archive_latest_bids.get(token)
        if previous_latest is None or ordering > previous_latest[0]:
            archive_latest_bids[token] = (ordering, amount, bid_time, bidder)

    for token, archive_row in archive_timeline.items():
        dashboard_row = dashboard_timeline[token]
        archive_state = text(archive_row.get("auction_state")).lower()
        dashboard_state = text(dashboard_row.get("auction_state")).lower()
        if archive_state not in {"settled", "unsettled_or_live"}:
            raise AssertionError(f"Mission 3 Dog #{token} archive auction_state is invalid")
        if dashboard_state not in {"settled", "live", "ended_unsettled"}:
            raise AssertionError(f"Mission 3 Dog #{token} dashboard auction_state is invalid")
        if (archive_state == "settled") != (dashboard_state == "settled"):
            raise AssertionError(f"Mission 3 Dog #{token} settlement state differs from quorum archive")

        created_dashboard = _parity_hash(
            dashboard_row.get("created_tx_hash"),
            f"Mission 3 Dog #{token} dashboard created transaction",
        )
        created_archive = _parity_hash(
            archive_row.get("created_tx"),
            f"Mission 3 Dog #{token} archive created transaction",
        )
        if created_dashboard != created_archive:
            raise AssertionError(f"Mission 3 Dog #{token} created_tx_hash differs from quorum archive")
        settled = archive_state == "settled"
        settled_dashboard = _parity_hash(
            dashboard_row.get("settled_tx_hash"),
            f"Mission 3 Dog #{token} dashboard settled transaction",
            allow_empty=not settled,
        )
        settled_archive = _parity_hash(
            archive_row.get("settled_tx"),
            f"Mission 3 Dog #{token} archive settled transaction",
            allow_empty=not settled,
        )
        if settled_dashboard != settled_archive:
            raise AssertionError(f"Mission 3 Dog #{token} settled_tx_hash differs from quorum archive")
        if not settled and (settled_dashboard or settled_archive):
            raise AssertionError(f"Mission 3 Dog #{token} unsettled auction has a settlement transaction")

        integer_fields = (
            ("bids", "bids"),
            ("unique_bidders", "unique_bidder_count"),
        )
        for dashboard_key, archive_key in integer_fields:
            dashboard_value = _parity_int(
                dashboard_row.get(dashboard_key),
                f"Mission 3 Dog #{token} dashboard {dashboard_key}",
            )
            archive_value = _parity_int(
                archive_row.get(archive_key),
                f"Mission 3 Dog #{token} archive {archive_key}",
            )
            if dashboard_value != archive_value:
                raise AssertionError(f"Mission 3 Dog #{token} {dashboard_key} differs from quorum archive")
        bid_count = _parity_int(dashboard_row.get("bids"), f"Mission 3 Dog #{token} dashboard bids")
        raw_bid_count = archive_bid_counts.get(token, 0)
        if bid_count != raw_bid_count:
            raise AssertionError(f"Mission 3 Dog #{token} bid count differs from archive raw logs")
        dashboard_unique_bidders = _parity_int(
            dashboard_row.get("unique_bidders"),
            f"Mission 3 Dog #{token} dashboard unique bidders",
        )
        raw_unique_bidders = len(archive_bid_bidders.get(token, set()))
        if dashboard_unique_bidders != raw_unique_bidders:
            raise AssertionError(f"Mission 3 Dog #{token} unique bidder count differs from archive raw logs")
        total_bid_eth = _parity_decimal(
            dashboard_row.get("total_bid_eth"),
            f"Mission 3 Dog #{token} dashboard total bid ETH",
            empty_zero=True,
        )
        if total_bid_eth != archive_bid_totals.get(token, Decimal(0)):
            raise AssertionError(f"Mission 3 Dog #{token} total_bid_eth differs from archive raw logs")
        decimal_fields = (
            ("high_bid_eth", "high_bid_eth"),
            ("latest_bid_eth", "latest_bid_eth"),
            ("settled_eth", "settled_amount_eth"),
        )
        for dashboard_key, archive_key in decimal_fields:
            dashboard_value = _parity_decimal(
                dashboard_row.get(dashboard_key),
                f"Mission 3 Dog #{token} dashboard {dashboard_key}",
                empty_zero=True,
            )
            archive_value = _parity_decimal(
                archive_row.get(archive_key),
                f"Mission 3 Dog #{token} archive {archive_key}",
                empty_zero=True,
            )
            if dashboard_value != archive_value:
                raise AssertionError(f"Mission 3 Dog #{token} {dashboard_key} differs from quorum archive")
        raw_high_bid = archive_bid_highs.get(token, Decimal(0))
        if _parity_decimal(
            dashboard_row.get("high_bid_eth"),
            f"Mission 3 Dog #{token} dashboard high bid ETH",
            empty_zero=True,
        ) != raw_high_bid:
            raise AssertionError(f"Mission 3 Dog #{token} high bid differs from archive raw logs")
        raw_latest = archive_latest_bids.get(token)
        raw_latest_amount = raw_latest[1] if raw_latest else Decimal(0)
        raw_latest_time = raw_latest[2] if raw_latest else ""
        if _parity_decimal(
            dashboard_row.get("latest_bid_eth"),
            f"Mission 3 Dog #{token} dashboard latest bid ETH",
            empty_zero=True,
        ) != raw_latest_amount:
            raise AssertionError(f"Mission 3 Dog #{token} latest bid differs from archive raw logs")
        dashboard_latest_time = _parity_timestamp(
            dashboard_row.get("latest_bid_utc"),
            f"Mission 3 Dog #{token} dashboard latest bid time",
            allow_empty=raw_latest is None,
        )
        if dashboard_latest_time != raw_latest_time:
            raise AssertionError(f"Mission 3 Dog #{token} latest bid time differs from archive raw logs")
        dashboard_latest_bidder = text(dashboard_row.get("latest_bidder"))
        if (raw_latest is None and dashboard_latest_bidder) or (raw_latest is not None and not dashboard_latest_bidder):
            raise AssertionError(f"Mission 3 Dog #{token} dashboard latest bidder presence differs from raw logs")
        archive_latest_bidder_raw = text(archive_row.get("latest_bidder"))
        if raw_latest is None:
            if archive_latest_bidder_raw:
                raise AssertionError(f"Mission 3 Dog #{token} archive latest bidder exists without a raw bid")
        elif _parity_address(
            archive_latest_bidder_raw,
            f"Mission 3 Dog #{token} archive latest bidder",
        ) != raw_latest[3]:
            raise AssertionError(f"Mission 3 Dog #{token} latest bidder differs from archive raw logs")
        timestamp_fields = (
            ("start_time_utc", "start_time_utc"),
            ("end_time_utc", "end_time_utc"),
            ("latest_bid_utc", "latest_bid_time_utc"),
            ("settled_time_utc", "settled_time_utc"),
        )
        for dashboard_key, archive_key in timestamp_fields:
            allow_empty = archive_key in {"latest_bid_time_utc", "settled_time_utc"} and not text(archive_row.get(archive_key))
            dashboard_value = _parity_timestamp(
                dashboard_row.get(dashboard_key),
                f"Mission 3 Dog #{token} dashboard {dashboard_key}",
                allow_empty=allow_empty,
            )
            archive_value = _parity_timestamp(
                archive_row.get(archive_key),
                f"Mission 3 Dog #{token} archive {archive_key}",
                allow_empty=allow_empty,
            )
            if dashboard_value != archive_value:
                raise AssertionError(f"Mission 3 Dog #{token} {dashboard_key} differs from quorum archive")
        start_time = _parity_timestamp(
            archive_row.get("start_time_utc"),
            f"Mission 3 Dog #{token} archive start time",
        )
        end_time = _parity_timestamp(
            archive_row.get("end_time_utc"),
            f"Mission 3 Dog #{token} archive end time",
        )
        if start_time >= end_time:
            raise AssertionError(f"Mission 3 Dog #{token} archive auction time range is invalid")
        bid_time_range = archive_bid_time_ranges.get(token)
        if bid_time_range and (bid_time_range[0] < start_time or bid_time_range[1] > end_time):
            raise AssertionError(f"Mission 3 Dog #{token} archive raw bid falls outside the auction window")
        if settled:
            settled_time = _parity_timestamp(
                archive_row.get("settled_time_utc"),
                f"Mission 3 Dog #{token} archive settled time",
            )
            if settled_time < end_time:
                raise AssertionError(f"Mission 3 Dog #{token} settled before its auction ended")
        else:
            unsettled_metadata = any(
                text(archive_row.get(key))
                for key in ("settled_block", "settled_time_utc", "winner")
            )
            settled_amount = _parity_decimal(
                archive_row.get("settled_amount_eth"),
                f"Mission 3 Dog #{token} archive settled amount",
                empty_zero=True,
            )
            if unsettled_metadata or settled_amount != 0:
                raise AssertionError(f"Mission 3 Dog #{token} unsettled auction has settlement data")

    dashboard_winners = by_token(loaded["dashboard_winners"], "dashboard auction_winners")
    archive_winners = by_token(loaded["archive_winners"], "Mission 3 archive winners")
    if set(dashboard_winners) != set(archive_winners):
        raise AssertionError("Mission 3 dashboard/archive settlement Dog sets differ")
    settled_timeline_tokens = {
        token
        for token, row in archive_timeline.items()
        if text(row.get("auction_state")).lower() == "settled"
    }
    if set(archive_winners) != settled_timeline_tokens:
        raise AssertionError("Mission 3 archive winner set differs from settled timeline Dogs")
    for token, archive_row in archive_winners.items():
        dashboard_row = dashboard_winners[token]
        dashboard_winner = _parity_address(
            dashboard_row.get("winner_wallet"),
            f"Mission 3 Dog #{token} dashboard winner",
        )
        archive_winner = _parity_address(
            archive_row.get("winner"),
            f"Mission 3 Dog #{token} archive winner",
        )
        dashboard_amount = _parity_decimal(
            dashboard_row.get("winning_bid_eth"),
            f"Mission 3 Dog #{token} dashboard winning bid",
        )
        archive_amount = _parity_decimal(
            archive_row.get("amount_eth"),
            f"Mission 3 Dog #{token} archive winning bid",
        )
        if dashboard_amount <= 0 or archive_amount <= 0:
            raise AssertionError(f"Mission 3 Dog #{token} winning bid must be positive")
        dashboard_block = _parity_int(
            dashboard_row.get("block_number"),
            f"Mission 3 Dog #{token} dashboard settled block",
            minimum=1,
        )
        archive_block = _parity_int(
            archive_row.get("settled_block"),
            f"Mission 3 Dog #{token} archive settled block",
            minimum=1,
        )
        dashboard_tx = _parity_hash(
            dashboard_row.get("tx_hash"),
            f"Mission 3 Dog #{token} dashboard settled transaction",
        )
        archive_tx = _parity_hash(
            archive_row.get("settled_tx"),
            f"Mission 3 Dog #{token} archive settled transaction",
        )
        dashboard_bid_count = _parity_int(
            dashboard_row.get("bid_count"),
            f"Mission 3 Dog #{token} dashboard winner bid count",
            minimum=1,
        )
        archive_bid_count = _parity_int(
            archive_row.get("bid_count"),
            f"Mission 3 Dog #{token} archive winner bid count",
            minimum=1,
        )
        dashboard_unique = _parity_int(
            dashboard_row.get("unique_bidders"),
            f"Mission 3 Dog #{token} dashboard winner unique bidder count",
            minimum=1,
        )
        archive_unique = _parity_int(
            archive_row.get("unique_bidder_count"),
            f"Mission 3 Dog #{token} archive winner unique bidder count",
            minimum=1,
        )
        dashboard_first = _parity_timestamp(
            dashboard_row.get("first_bid_utc"),
            f"Mission 3 Dog #{token} dashboard first bid time",
        )
        archive_first = _parity_timestamp(
            archive_row.get("first_bid_time_utc"),
            f"Mission 3 Dog #{token} archive first bid time",
        )
        dashboard_last = _parity_timestamp(
            dashboard_row.get("last_bid_utc"),
            f"Mission 3 Dog #{token} dashboard last bid time",
        )
        archive_last = _parity_timestamp(
            archive_row.get("last_bid_time_utc"),
            f"Mission 3 Dog #{token} archive last bid time",
        )
        dashboard_settled = _parity_timestamp(
            dashboard_row.get("settled_time_utc"),
            f"Mission 3 Dog #{token} dashboard settled time",
        )
        archive_settled = _parity_timestamp(
            archive_row.get("settled_time_utc"),
            f"Mission 3 Dog #{token} archive settled time",
        )
        comparisons = (
            (dashboard_winner, archive_winner, "winner"),
            (dashboard_amount, archive_amount, "winning bid"),
            (dashboard_block, archive_block, "settled block"),
            (dashboard_tx, archive_tx, "settled transaction"),
            (dashboard_bid_count, archive_bid_count, "bid count"),
            (dashboard_unique, archive_unique, "unique bidder count"),
            (dashboard_first, archive_first, "first bid time"),
            (dashboard_last, archive_last, "last bid time"),
            (dashboard_settled, archive_settled, "settled time"),
        )
        for dashboard_value, archive_value, label in comparisons:
            if dashboard_value != archive_value:
                raise AssertionError(f"Mission 3 Dog #{token} {label} differs from quorum archive")
        timeline_row = archive_timeline[token]
        timeline_winner = _parity_address(
            timeline_row.get("winner"),
            f"Mission 3 Dog #{token} archive timeline winner",
        )
        timeline_amount = _parity_decimal(
            timeline_row.get("settled_amount_eth"),
            f"Mission 3 Dog #{token} archive timeline settled amount",
        )
        timeline_block = _parity_int(
            timeline_row.get("settled_block"),
            f"Mission 3 Dog #{token} archive timeline settled block",
            minimum=1,
        )
        timeline_tx = _parity_hash(
            timeline_row.get("settled_tx"),
            f"Mission 3 Dog #{token} archive timeline settled transaction",
        )
        timeline_settled = _parity_timestamp(
            timeline_row.get("settled_time_utc"),
            f"Mission 3 Dog #{token} archive timeline settled time",
        )
        if (timeline_winner, timeline_amount, timeline_block, timeline_tx, timeline_settled) != (
            archive_winner,
            archive_amount,
            archive_block,
            archive_tx,
            archive_settled,
        ):
            raise AssertionError(f"Mission 3 Dog #{token} archive winner differs from its timeline settlement")
        if dashboard_bid_count != archive_bid_counts.get(token, 0):
            raise AssertionError(f"Mission 3 Dog #{token} winner bid count differs from archive raw logs")
        if dashboard_unique != len(archive_bid_bidders.get(token, set())):
            raise AssertionError(f"Mission 3 Dog #{token} winner unique bidder count differs from archive raw logs")
        raw_latest = archive_latest_bids.get(token)
        if raw_latest is None or raw_latest[1] != dashboard_amount or raw_latest[3] != dashboard_winner:
            raise AssertionError(f"Mission 3 Dog #{token} winner differs from latest archive raw bid")
        raw_time_range = archive_bid_time_ranges.get(token)
        if not raw_time_range or (dashboard_first, dashboard_last) != raw_time_range:
            raise AssertionError(f"Mission 3 Dog #{token} winner bid time range differs from archive raw logs")

    return {
        "checked": True,
        "auctions": len(archive_timeline),
        "settlements": len(archive_winners),
        "bids": len(loaded["archive_bids"]),
    }


def find_current_feed_row(feed_rows: list[dict[str, Any]], current_dog_id: int) -> dict[str, Any]:
    matches = [row for row in feed_rows if dog_id(row) == current_dog_id]
    if len(matches) != 1:
        raise AssertionError(f"auction_feed has {len(matches)} rows for current Dog #{current_dog_id}, expected exactly 1")
    return matches[0]


def find_unified_mission3(path: Path, mission3_dog_id: int) -> dict[str, Any]:
    rows = load_json(path)
    if not isinstance(rows, list):
        raise AssertionError(f"{path.relative_to(ROOT)} is not a JSON list")
    for row in rows:
        if isinstance(row, dict) and row.get("mission") == 3 and row.get("dog_id") == mission3_dog_id:
            return row
    raise AssertionError(f"{path.relative_to(ROOT)} missing Mission 3 Dog #{mission3_dog_id}")


def find_unified_current(path: Path, current_dog_id: int) -> dict[str, Any]:
    return find_unified_mission3(path, current_dog_id)


def validate_reward_metrics(metrics: dict[str, str], index: str, readme: dict[str, str]) -> None:
    basis_count = optional_decimal_value(metrics.get("reward_basis_dogs"))
    observed_count = optional_decimal_value(metrics.get("reward_observed_dogs_count"))
    if basis_count != Decimal("133") or observed_count not in (None, Decimal("133")):
        raise AssertionError("mission3_metrics reward_basis_dogs must use the observed 133-Dog reward basis")
    if "observed" not in text(metrics.get("reward_basis_source")).lower():
        raise AssertionError("mission3_metrics reward_basis_source must identify the observed reward stream snapshot")

    woof_flow = optional_decimal_value(metrics.get("reward_observed_woof_flow_per_day"))
    sup_flow = optional_decimal_value(metrics.get("reward_observed_sup_flow_per_day"))
    woof_usd = optional_decimal_value(metrics.get("woof_usd_price"))
    sup_usd = optional_decimal_value(metrics.get("sup_usd_price"))
    if basis_count is None or woof_flow is None or sup_flow is None or woof_usd is None or sup_usd is None:
        raise AssertionError("mission3_metrics missing observed reward basis inputs")
    expected_woof_per_dog = woof_flow / basis_count
    expected_sup_per_dog = sup_flow / basis_count
    expected_values = {
        "reward_observed_woof_per_dog_per_day": quantized_decimal_str(expected_woof_per_dog, 12),
        "reward_woof_per_dog_per_day": quantized_decimal_str(expected_woof_per_dog, 12),
        "reward_observed_sup_per_dog_per_day": quantized_decimal_str(expected_sup_per_dog, 16),
        "reward_sup_per_dog_per_day": quantized_decimal_str(expected_sup_per_dog, 16),
        "reward_total_per_dog_usd_per_day": quantized_decimal_str((expected_woof_per_dog * woof_usd) + (expected_sup_per_dog * sup_usd), 6),
    }
    for key, expected_value in expected_values.items():
        if text(metrics.get(key)) != expected_value:
            raise AssertionError(f"mission3_metrics {key} differs from observed 133-Dog reward basis: expected {expected_value!r}, got {metrics.get(key)!r}")
        assert_metric_cell(key, expected_value, index)

    reward_surface = reward_strip_surface(index)
    if not reward_surface:
        raise AssertionError("index.html missing reward-strip APR/payback surface")
    for removed_copy in ("Observed 133-Dog stream", "WOOF Vault Bonus excluded."):
        if removed_copy in reward_surface:
            raise AssertionError(f"index.html still renders removed reward basis copy: {removed_copy!r}")

    current_bid_usd = optional_decimal_value(metrics.get("current_bid_usd"))
    daily_flow = optional_decimal_value(metrics.get("reward_total_per_dog_usd_per_day"))
    if current_bid_usd is None or current_bid_usd <= 0 or daily_flow is None or daily_flow <= 0:
        expected = {
            "reward_current_bid_payback_days": "N/A",
            "reward_current_bid_daily_roi_pct": "N/A",
            "reward_current_bid_apr_pct": "N/A",
            "reward_current_bid_apr_display": "N/A",
        }
    else:
        payback = current_bid_usd / daily_flow
        daily_roi = daily_flow / current_bid_usd * Decimal(100)
        apr = daily_roi * Decimal(365)
        expected = {
            "reward_current_bid_payback_days": quantized_decimal_str(payback, 2),
            "reward_current_bid_daily_roi_pct": quantized_decimal_str(daily_roi, 4),
            "reward_current_bid_apr_pct": quantized_decimal_str(apr, 2),
            "reward_current_bid_apr_display": reward_apr_display(apr),
        }
    for key, expected_value in expected.items():
        if text(metrics.get(key)) != expected_value:
            raise AssertionError(f"mission3_metrics {key} differs from current bid reward math: expected {expected_value!r}, got {metrics.get(key)!r}")
        assert_metric_cell(key, expected_value, index)

    expected_display = expected["reward_current_bid_apr_display"]
    reward_surface = reward_strip_surface(index)
    if not reward_surface:
        raise AssertionError("index.html missing reward-strip APR/payback surface")
    if expected_display and expected_display not in reward_surface:
        raise AssertionError(f"index.html missing reward APR display: {expected_display!r}")
    payback_display = "N/A"
    payback_days = optional_decimal_value(expected["reward_current_bid_payback_days"])
    if payback_days is not None and payback_days > 0:
        places = 1 if payback_days < 10 else 0
        payback_display = f"≈{payback_days:,.{places}f} days"
    if payback_display and payback_display not in reward_surface:
        raise AssertionError(f"index.html missing reward payback display: {payback_display!r}")
    readme_summary = readme.get("Bid payback / APR")
    expected_summary = f"{payback_display} / {expected_display}"
    if readme_summary != expected_summary:
        raise AssertionError("README Bid payback / APR differs from mission3_metrics reward estimate")


def validate_season6_metrics(metrics: dict[str, str], index: str) -> None:
    if not text(metrics.get("season6_sup_status")):
        return
    enabled = text(metrics.get("season6_sup_enabled")).lower()
    if enabled in {"false", "0", "no", "disabled"}:
        return
    estimate_status = text(metrics.get("season6_sup_estimate_status"))
    current_bidder = normalize_address(metrics.get("season6_sup_current_bidder_wallet"))
    required = [
        "season6_sup_enabled",
        "season6_sup_token",
        "season6_sup_usd_price",
        "season6_sup_total_allocation",
        "season6_sup_wallet_cap",
        "season6_sup_xp_per_win",
        "season6_sup_start_utc",
        "season6_sup_end_utc",
        "season6_sup_reward_start_delay_days",
        "season6_sup_settled_win_count_to_date",
        "season6_sup_current_bidder_prior_s6_wins",
        "season6_sup_current_bidder_prior_s6_xp",
        "season6_sup_current_bid_estimated_win_time_utc",
        "season6_sup_current_bid_estimated_raw_incremental_sup",
        "season6_sup_current_bid_estimated_cap_aware_sup",
        "season6_sup_current_bid_estimated_cap_aware_usd",
        "season6_sup_current_bid_projected_total_without_win_sup",
        "season6_sup_current_bid_projected_total_with_win_sup",
        "season6_sup_current_bid_cap_remaining_before_win_sup",
        "season6_sup_projection_model",
        "season6_sup_future_dilution_enabled",
        "season6_sup_expected_future_settlement_interval_seconds",
        "season6_sup_estimate_status",
        # Legacy aliases kept for generated-data compatibility.
        "season6_sup_total_allocated",
        "season6_sup_cap_per_wallet",
        "season6_sup_xp_per_settled_win",
        "season6_current_bidder_projected_raw_sup_if_wins",
        "season6_current_bidder_projected_capped_sup_if_wins",
        "season6_current_bidder_projected_raw_usd_if_wins",
        "season6_current_bidder_projected_capped_usd_if_wins",
    ]
    if estimate_status != "no_current_bid":
        required.append("season6_sup_current_bidder_wallet")
    missing = [key for key in required if not text(metrics.get(key))]
    if missing:
        raise AssertionError("mission3_metrics missing Season 6 metrics: " + ", ".join(missing))
    if estimate_status != "no_current_bid" and not current_bidder:
        raise AssertionError("mission3_metrics season6_sup_current_bidder_wallet invalid")

    cap = optional_decimal_value(metrics.get("season6_sup_wallet_cap") or metrics.get("season6_sup_cap_per_wallet"))
    if cap is None or cap <= 0:
        raise AssertionError("mission3_metrics season6_sup_wallet_cap invalid")
    estimate = optional_decimal_value(metrics.get("season6_sup_current_bid_estimated_cap_aware_sup"))
    if estimate is not None and estimate > cap:
        raise AssertionError("Season 6 current-bid cap-aware estimate exceeds configured cap")
    raw_incremental = optional_decimal_value(metrics.get("season6_sup_current_bid_estimated_raw_incremental_sup"))
    if estimate is not None and raw_incremental is not None and estimate > raw_incremental:
        raise AssertionError("Season 6 cap-aware estimate exceeds raw incremental estimate")
    cap_remaining = optional_decimal_value(metrics.get("season6_sup_current_bid_cap_remaining_before_win_sup"))
    if estimate is not None and cap_remaining is not None and estimate > cap_remaining:
        raise AssertionError("Season 6 cap-aware estimate exceeds cap remaining before current win")

    for path in [ROOT / "generated" / "season6_sup_by_winner.json", ROOT / "public" / "generated" / "season6_sup_by_winner.json"]:
        rows = load_json(path)
        if not isinstance(rows, list):
            raise AssertionError(f"{path.relative_to(ROOT)} is not a JSON list")
        for row in rows:
            if not isinstance(row, dict):
                continue
            capped = optional_decimal_value(row.get("season6_capped_sup_projected_full"))
            if capped is not None and capped > cap:
                raise AssertionError("Season 6 capped SUP exceeds configured cap")
            row_cap = optional_decimal_value(row.get("season6_cap_sup"))
            if row_cap is not None and row_cap != cap:
                raise AssertionError("Season 6 winner cap differs from configured cap")

    for table_name in ["season6_sup_by_winner", "season6_sup_rewards_by_auction", "season6_sup_current_bidder_status"]:
        generated_path = ROOT / "generated" / f"{table_name}.json"
        public_path = ROOT / "public" / "generated" / f"{table_name}.json"
        if generated_path.exists() and public_path.exists() and generated_path.read_bytes() != public_path.read_bytes():
            raise AssertionError(f"public/generated/{table_name}.json differs from generated/{table_name}.json")

    status_rows = load_json(ROOT / "generated" / "season6_sup_current_bidder_status.json")
    if current_bidder:
        if not isinstance(status_rows, list) or not status_rows or not isinstance(status_rows[0], dict):
            raise AssertionError("generated/season6_sup_current_bidder_status.json missing current bidder row")
        status = status_rows[0]
        if int(status.get("current_auction_token_id") or -1) != int(metrics.get("current_auction_token_id") or -2):
            raise AssertionError("Season 6 current bidder status Dog differs from mission3_metrics")
        if normalize_address(status.get("current_bidder_wallet")) != current_bidder:
            raise AssertionError("Season 6 current bidder status wallet differs from mission3_metrics")
        if text(status.get("prior_s6_wins_confirmed")) != text(metrics.get("season6_sup_current_bidder_prior_s6_wins")):
            raise AssertionError("Season 6 current bidder prior wins differ from status row")
        if text(status.get("prior_s6_xp_confirmed")) != text(metrics.get("season6_sup_current_bidder_prior_s6_xp")):
            raise AssertionError("Season 6 current bidder prior XP differs from status row")
        if text(status.get("estimated_cap_aware_incremental_sup")) != text(metrics.get("season6_sup_current_bid_estimated_cap_aware_sup")):
            raise AssertionError("Season 6 status cap-aware estimate differs from mission3_metrics")
        if text(status.get("estimated_cap_aware_incremental_usd")) != text(metrics.get("season6_sup_current_bid_estimated_cap_aware_usd")):
            raise AssertionError("Season 6 status USD estimate differs from mission3_metrics")

    forbidden_fragments = [
        "Pool: 251,340 SUP",
        "Cap: 12,500 SUP",
        "100 XP per settled Dog win",
        "Projected if current bid wins",
        "Cap-limited estimate",
        "Season 6 SUP rewards live",
    ]
    for fragment in forbidden_fragments:
        if fragment in index:
            raise AssertionError(f"index.html still exposes verbose Season 6 UI fragment: {fragment!r}")

    surface = season6_surface(index)
    if not surface:
        raise AssertionError("index.html missing compact Season 6 SUP estimate surface")
    if "Season 6 SUP estimate" not in surface:
        raise AssertionError("index.html missing compact Season 6 SUP estimate title")
    season_pos = surface.find("Season 6 SUP estimate")
    payback_pos = surface.lower().find("bid payback")
    if payback_pos == -1:
        raise AssertionError("index.html reward surface missing Bid payback card")
    if season_pos == -1 or season_pos > payback_pos:
        raise AssertionError("Season 6 SUP estimate must appear before Bid payback")

    estimate_status = text(metrics.get("season6_sup_estimate_status"))
    if estimate_status == "no_current_bid" or not current_bidder:
        expected_fragments = ["Bid to estimate S6 SUP"]
    else:
        expected_fragments = [
            season6_sup_display(metrics.get("season6_sup_current_bid_estimated_cap_aware_sup")),
            f"{season6_usd_display(metrics.get('season6_sup_current_bid_estimated_cap_aware_usd'))} if current bid wins",
        ]
        if estimate_status == "wallet_near_cap":
            expected_fragments.append("Wallet estimate already near cap.")
        else:
            expected_fragments.append("Adjusted for prior S6 wins; estimate only.")
    for fragment in expected_fragments:
        if fragment and fragment not in surface:
            raise AssertionError(f"Season 6 compact card mismatch: missing {fragment!r}")

def wei_to_eth(value: Any) -> Decimal:
    raw = text(value)
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw) / Decimal(10**18)
    except (InvalidOperation, ValueError) as exc:
        raise AssertionError(f"invalid wei value {value!r}") from exc


def _first_row_at(root: Path, rel: str) -> dict[str, Any]:
    data = load_json(root / rel)
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise AssertionError(f"{rel} missing first object row")
    return data[0]


def _current_feed_row_at(root: Path, current_dog_id: int, rel: str = "generated/auction_feed.json") -> dict[str, Any]:
    data = load_json(root / rel)
    rows = [row for row in data if isinstance(row, dict) and dog_id(row) == current_dog_id] if isinstance(data, list) else []
    if len(rows) != 1:
        raise AssertionError(f"{rel} has {len(rows)} rows for observed onchain current auction Dog #{current_dog_id}, expected exactly 1")
    return rows[0]


def _latest_history_row_at(root: Path, current_dog_id: int, rel: str = "generated/current_auction_bid_history.json") -> dict[str, Any] | None:
    data = load_json(root / rel)
    rows = [row for row in data if isinstance(row, dict) and dog_id(row) == current_dog_id] if isinstance(data, list) else []
    rows.sort(key=lambda row: (text(row.get("bid_time_utc")), int(row.get("block_number") or 0), int(row.get("log_index") or 0)), reverse=True)
    return rows[0] if rows else None


def _assert_observed_current_row(rel: str, row: dict[str, Any], *, observed_token_id: int, observed_wallet: str, observed_bid_eth: Decimal) -> None:
    has_dog_identity = any(text(row.get(key)) for key in ("token_id", "dog_id", "dog", "dog_name"))
    if has_dog_identity and dog_id(row) != observed_token_id:
        raise AssertionError(f"{rel} observed onchain current auction token mismatch: expected Dog #{observed_token_id}, got Dog #{dog_id(row)}")
    wallet = normalize_address(row.get("bidder_wallet") or row.get("bidder_winner_wallet") or row.get("winner_wallet"))
    if observed_wallet and observed_wallet != ZERO and wallet != observed_wallet:
        raise AssertionError(f"{rel} observed onchain current auction high-bidder wallet mismatch: expected {observed_wallet}, got {wallet or 'missing'}")
    amount = decimal_value(row.get("current_bid_eth") or row.get("amount_eth") or row.get("latest_bid_eth") or row.get("bid_eth"))
    if amount != observed_bid_eth:
        raise AssertionError(f"{rel} observed onchain current auction bid amount mismatch: expected {observed_bid_eth.normalize()} ETH, got {amount.normalize()} ETH")


def validate_current_surface_against_observed_state(observed_state: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    """Compare generated/public/rendered current-auction surfaces to watcher-observed onchain state.

    The regular consistency validator proves generated artifacts agree with each other. This extra
    guard catches the failure mode where the watcher saw a newer same-token bid but a refresh/publish
    failure left every generated surface consistently stale.
    """
    observed_token_id = int(observed_state.get("last_observed_token_id") or observed_state.get("last_seen_token_id") or 0)
    observed_wallet = normalize_address(observed_state.get("last_observed_high_bidder") or observed_state.get("last_seen_high_bidder"))
    observed_bid_eth = wei_to_eth(observed_state.get("last_observed_amount_wei") or observed_state.get("last_seen_amount_wei"))
    if observed_token_id <= 0 or observed_bid_eth <= 0:
        return {"observed_check": "skipped", "reason": "missing observed current auction bid"}

    generated_current = _first_row_at(root, "generated/current_auction.json")
    _assert_observed_current_row(
        "generated/current_auction.json",
        generated_current,
        observed_token_id=observed_token_id,
        observed_wallet=observed_wallet,
        observed_bid_eth=observed_bid_eth,
    )
    public_current_path = root / "public" / "generated" / "current_auction.json"
    if public_current_path.exists():
        public_current = _first_row_at(root, "public/generated/current_auction.json")
        _assert_observed_current_row(
            "public/generated/current_auction.json",
            public_current,
            observed_token_id=observed_token_id,
            observed_wallet=observed_wallet,
            observed_bid_eth=observed_bid_eth,
        )

    latest = _first_row_at(root, "generated/current_latest_bid.json")
    _assert_observed_current_row(
        "generated/current_latest_bid.json",
        latest,
        observed_token_id=observed_token_id,
        observed_wallet=observed_wallet,
        observed_bid_eth=observed_bid_eth,
    )
    public_latest_path = root / "public" / "generated" / "current_latest_bid.json"
    if public_latest_path.exists():
        public_latest = _first_row_at(root, "public/generated/current_latest_bid.json")
        _assert_observed_current_row(
            "public/generated/current_latest_bid.json",
            public_latest,
            observed_token_id=observed_token_id,
            observed_wallet=observed_wallet,
            observed_bid_eth=observed_bid_eth,
        )
    feed = _current_feed_row_at(root, observed_token_id)
    _assert_observed_current_row(
        "generated/auction_feed.json",
        feed,
        observed_token_id=observed_token_id,
        observed_wallet=observed_wallet,
        observed_bid_eth=observed_bid_eth,
    )
    public_feed_path = root / "public" / "generated" / "auction_feed.json"
    if public_feed_path.exists():
        public_feed = _current_feed_row_at(root, observed_token_id, "public/generated/auction_feed.json")
        _assert_observed_current_row(
            "public/generated/auction_feed.json",
            public_feed,
            observed_token_id=observed_token_id,
            observed_wallet=observed_wallet,
            observed_bid_eth=observed_bid_eth,
        )
    history = _latest_history_row_at(root, observed_token_id)
    if history is not None:
        _assert_observed_current_row(
            "generated/current_auction_bid_history.json",
            history,
            observed_token_id=observed_token_id,
            observed_wallet=observed_wallet,
            observed_bid_eth=observed_bid_eth,
        )
    public_history_path = root / "public" / "generated" / "current_auction_bid_history.json"
    if public_history_path.exists():
        public_history = _latest_history_row_at(root, observed_token_id, "public/generated/current_auction_bid_history.json")
        if public_history is not None:
            _assert_observed_current_row(
                "public/generated/current_auction_bid_history.json",
                public_history,
                observed_token_id=observed_token_id,
                observed_wallet=observed_wallet,
                observed_bid_eth=observed_bid_eth,
            )

    index_path = root / "index.html"
    if index_path.exists():
        index = index_path.read_text(encoding="utf-8")
        if f"Dog #{observed_token_id}" not in index:
            raise AssertionError(f"index.html observed onchain current auction Dog #{observed_token_id} missing")
        rendered_eth_values = [decimal_value(match.group(1)) for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*ETH", index)]
        if observed_bid_eth not in rendered_eth_values:
            raise AssertionError(f"index.html observed onchain current auction bid amount missing: {observed_bid_eth.normalize()} ETH")
        generated_display = text(generated_current.get("bidder"))
        if generated_display and generated_display not in index:
            raise AssertionError(f"index.html observed onchain current auction high-bidder display missing: {generated_display!r}")

    readme_path = root / "README.md"
    if readme_path.exists():
        readme = readme_snapshot() if root == ROOT else _readme_snapshot_at(root)
        if readme.get("Current Dog") and readme.get("Current Dog") != f"Dog #{observed_token_id}":
            raise AssertionError("README observed onchain current auction Dog mismatch")
        readme_bid_match = re.search(r"[0-9]+(?:\.[0-9]+)?", readme.get("Current bid", ""))
        if readme_bid_match and decimal_value(readme_bid_match.group(0)) != observed_bid_eth:
            raise AssertionError("README observed onchain current auction bid amount mismatch")

    return {
        "observed_check": "ok",
        "observed_dog": f"Dog #{observed_token_id}",
        "observed_bid_eth": str(observed_bid_eth.normalize()),
        "observed_high_bidder": observed_wallet,
        "observed_bid_log_id": text(observed_state.get("last_observed_bid_log_id") or observed_state.get("last_seen_bid_log_id")),
    }


def _readme_snapshot_at(root: Path) -> dict[str, str]:
    path = root / "README.md"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    in_section = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() == "## Current snapshot":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and line.startswith("|") and "---" not in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) >= 2 and cells[0] != "Field":
                values[cells[0]] = cells[1]
    return values


def validate_current_surface() -> dict[str, Any]:
    current = first_row(ROOT / "generated" / "current_auction.json")
    latest = first_row(ROOT / "generated" / "current_latest_bid.json")
    feed_rows_raw = load_json(ROOT / "generated" / "auction_feed.json")
    if not isinstance(feed_rows_raw, list):
        raise AssertionError("generated/auction_feed.json is not a list")
    feed_rows = [row for row in feed_rows_raw if isinstance(row, dict)]

    current_dog_id = dog_id(current)
    feed = find_current_feed_row(feed_rows, current_dog_id)
    current_state = text(current.get("auction_state")).lower()
    metrics = load_metrics()
    refresh_status = load_refresh_telemetry().validate_refresh_status(root=ROOT)
    index = (ROOT / "index.html").read_text(encoding="utf-8") if (ROOT / "index.html").exists() else ""
    readme = readme_snapshot()
    archive_parity = validate_mission3_archive_parity(root=ROOT)

    if int(metrics["current_auction_token_id"]) != current_dog_id:
        raise AssertionError("mission3_metrics current_auction_token_id differs from current_auction")
    if metrics["current_auction_status"].lower() != current_state:
        raise AssertionError("mission3_metrics current_auction_status differs from current_auction")
    if not decimals_equal(metrics["current_bid_eth"], current.get("current_bid_eth")):
        raise AssertionError("mission3_metrics current_bid_eth differs from current_auction")
    if not optional_decimals_equal(metrics.get("current_bid_usd"), current.get("current_bid_usd")):
        raise AssertionError("mission3_metrics current_bid_usd differs from current_auction")
    if text(metrics["current_bidder"]) != text(current.get("bidder")):
        raise AssertionError("mission3_metrics current_bidder differs from current_auction")
    if normalize_address(metrics["current_bidder_wallet"]) != normalize_address(current.get("bidder_wallet")):
        raise AssertionError("mission3_metrics current_bidder_wallet differs from current_auction")
    if text(metrics["latest_block"]) != text(current.get("latest_block")):
        raise AssertionError("mission3_metrics latest_block differs from current_auction")
    if iso_utc(metrics["latest_block_time_utc"]) != iso_utc(current.get("latest_block_time_utc")):
        raise AssertionError("mission3_metrics latest_block_time_utc differs from current_auction")
    if metrics["onchain_verification_status"] != "current_snapshot_cross_provider_verified":
        raise AssertionError("mission3_metrics current snapshot is not cross-provider verified")
    required_scope = {
        "snapshot_hash",
        "contract_code",
        "current_auction",
        "dog_total_supply",
        "dog_token_uri_bindings",
        "recent_event_logs",
    }
    actual_scope = {value.strip() for value in metrics["onchain_verification_scope"].split(",") if value.strip()}
    if not required_scope.issubset(actual_scope):
        raise AssertionError("mission3_metrics current snapshot verification scope is incomplete")
    if metrics["onchain_chain_id"] != "8453":
        raise AssertionError("mission3_metrics onchain_chain_id is not Base mainnet 8453")
    if not re.fullmatch(r"0x[a-fA-F0-9]{64}", metrics["snapshot_block_hash"]):
        raise AssertionError("mission3_metrics snapshot_block_hash is invalid")
    if int(metrics["rpc_quorum_size"]) < 2:
        raise AssertionError("mission3_metrics rpc_quorum_size must be at least 2")
    agreement_match = re.fullmatch(r"(\d+)/(\d+)", metrics["rpc_quorum_agreement"])
    if not agreement_match or int(agreement_match.group(1)) < int(metrics["rpc_quorum_size"]):
        raise AssertionError("mission3_metrics rpc_quorum_agreement is below the required quorum")
    providers = {value.strip() for value in re.split(r"[,|]", metrics["rpc_quorum_providers"]) if value.strip()}
    if len(providers) < int(metrics["rpc_quorum_size"]):
        raise AssertionError("mission3_metrics does not name enough independent RPC providers")
    log_providers = {value.strip() for value in re.split(r"[,|]", metrics["log_rpc_quorum_providers"]) if value.strip()}
    if len(log_providers) < int(metrics["rpc_quorum_size"]):
        raise AssertionError("mission3_metrics does not name enough independent log RPC providers")
    for code_metric in ("auction_house_code_sha256", "dog_nft_code_sha256"):
        if not re.fullmatch(r"[a-fA-F0-9]{64}", metrics[code_metric]):
            raise AssertionError(f"mission3_metrics {code_metric} is invalid")
    full_token_uri_status = "hash_pinned_cross_provider_exact_outcome_quorum"
    continuity_token_uri_status = "baseline_hash_pinned_quorum_plus_cross_provider_rarity_event_continuity"
    full_existence_status = "hash_pinned_cross_provider_exists_token_uri_parity_quorum"
    continuity_existence_status = (
        "baseline_exists_token_uri_quorum_plus_cross_provider_rarity_event_continuity"
    )
    full_continuity_status = "full_snapshot_exists_token_uri_content_schema_attested"
    incremental_continuity_status = (
        "hash_pinned_cross_provider_no_existence_or_token_uri_mutation_events_since_attestation"
    )
    if metrics["dog_token_uri_verification_status"] not in {
        full_token_uri_status,
        continuity_token_uri_status,
    }:
        raise AssertionError("mission3_metrics tokenURI outcomes are not hash-pinned and cross-provider verified")
    if metrics["dog_base_existence_verification_status"] not in {
        full_existence_status,
        continuity_existence_status,
    }:
        raise AssertionError("mission3_metrics Base exists/tokenURI parity is not cross-provider verified")
    rarity_attested_block = int(metrics["dog_rarity_attested_block"])
    rarity_continuity_block = int(metrics["dog_rarity_continuity_through_block"])
    latest_metric_block = int(metrics["latest_block"])
    if (
        rarity_attested_block <= 0
        or rarity_attested_block > rarity_continuity_block
        or rarity_continuity_block != latest_metric_block
    ):
        raise AssertionError("mission3_metrics rarity attestation block range is invalid")
    for key in ("dog_rarity_attested_block_hash", "dog_rarity_continuity_through_block_hash"):
        if not re.fullmatch(r"0x[0-9a-f]{64}", metrics[key]):
            raise AssertionError(f"mission3_metrics {key} is invalid")
    if metrics["dog_rarity_continuity_through_block_hash"] != metrics["snapshot_block_hash"]:
        raise AssertionError("mission3_metrics rarity continuity hash differs from the snapshot")
    continuity_status = metrics["dog_rarity_continuity_verification_status"]
    if continuity_status == full_continuity_status:
        if (
            metrics["dog_token_uri_verification_status"] != full_token_uri_status
            or metrics["dog_base_existence_verification_status"] != full_existence_status
            or rarity_attested_block != latest_metric_block
            or metrics["dog_rarity_attested_block_hash"] != metrics["snapshot_block_hash"]
        ):
            raise AssertionError("mission3_metrics full rarity attestation is internally inconsistent")
    elif continuity_status == incremental_continuity_status:
        if (
            metrics["dog_token_uri_verification_status"] != continuity_token_uri_status
            or metrics["dog_base_existence_verification_status"] != continuity_existence_status
        ):
            raise AssertionError("mission3_metrics incremental rarity continuity is internally inconsistent")
    else:
        raise AssertionError("mission3_metrics rarity continuity verification status is unsupported")
    dog_total_supply = int(metrics["dog_total_supply"])
    token_uri_present = int(metrics["dog_token_uri_present_count"])
    token_uri_unavailable = int(metrics["dog_token_uri_unavailable_count"])
    metadata_verified = int(metrics["dog_metadata_onchain_verified_count"])
    metadata_unavailable = int(metrics["dog_metadata_unavailable_count"])
    metadata_content_observed = int(metrics["dog_metadata_content_observed_count"])
    dog_id_ceiling = int(metrics["dog_id_ceiling"])
    base_existing = int(metrics["dog_base_existing_count"])
    base_unclaimed = int(metrics["dog_base_unclaimed_count"])
    rarity_universe = int(metrics["dog_rarity_universe_count"])
    rarity_excluded = int(metrics["dog_rarity_excluded_nonexistent_count"])
    rarity_incomplete = int(metrics["dog_rarity_incomplete_metadata_count"])
    if min(
        dog_total_supply,
        dog_id_ceiling,
        token_uri_present,
        token_uri_unavailable,
        metadata_verified,
        metadata_unavailable,
        metadata_content_observed,
        base_existing,
        base_unclaimed,
        rarity_universe,
        rarity_excluded,
        rarity_incomplete,
    ) < 0:
        raise AssertionError("mission3_metrics tokenURI/metadata aggregate counts cannot be negative")
    if dog_id_ceiling != dog_total_supply:
        raise AssertionError("mission3_metrics Dog ID ceiling contradicts legacy dog_total_supply")
    if token_uri_present + token_uri_unavailable != dog_total_supply:
        raise AssertionError("mission3_metrics tokenURI aggregate counts do not equal Dog total supply")
    if base_existing != token_uri_present or base_unclaimed != token_uri_unavailable:
        raise AssertionError("mission3_metrics Base existence counts contradict tokenURI outcomes")
    for key in ("dog_base_existing_token_ids_sha256", "dog_base_unclaimed_token_ids_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", metrics[key]):
            raise AssertionError(f"mission3_metrics {key} is invalid")
    if metadata_verified + metadata_unavailable != dog_total_supply:
        raise AssertionError("mission3_metrics metadata aggregate counts do not equal Dog total supply")
    if metadata_content_observed != metadata_verified:
        raise AssertionError("mission3_metrics observed metadata content count contradicts verified tokenURIs")
    if (
        metrics["dog_metadata_content_verification_status"]
        != "verified_token_uri_offchain_content_hash_observed"
    ):
        raise AssertionError("mission3_metrics metadata content verification class is unsupported")
    if metadata_unavailable < token_uri_unavailable:
        raise AssertionError("mission3_metrics hides tokenURI-unavailable Dogs from metadata unavailability")
    expected_metadata_status = (
        "complete_onchain_token_uri_verified"
        if metadata_unavailable == 0
        else "partial_onchain_token_uri_unavailable"
        if metadata_unavailable == token_uri_unavailable
        else "incomplete_metadata_unavailable"
    )
    if metrics["dog_metadata_verification_status"] != expected_metadata_status:
        raise AssertionError("mission3_metrics dog metadata aggregate status contradicts its counts")
    if rarity_universe != metadata_verified:
        raise AssertionError("mission3_metrics rarity universe differs from verified Base metadata count")
    if rarity_excluded != token_uri_unavailable:
        raise AssertionError("mission3_metrics rarity exclusions differ from nonexistent Base token count")
    if rarity_incomplete != metadata_unavailable - token_uri_unavailable:
        raise AssertionError("mission3_metrics rarity incomplete count contradicts metadata outcomes")
    expected_rarity_status = (
        "complete_verified_existing_token_universe"
        if rarity_universe > 0 and rarity_incomplete == 0
        else "unavailable_no_verified_existing_tokens"
        if rarity_universe == 0
        else "incomplete_existing_token_metadata"
    )
    if metrics["dog_rarity_verification_status"] != expected_rarity_status:
        raise AssertionError("mission3_metrics rarity status contradicts its counts")
    if metrics["dog_rarity_scope"] != "base_existing":
        raise AssertionError("mission3_metrics rarity scope is not Base-existing")
    if metrics["dog_rarity_score_method"] != "sum_existing_token_count_divided_by_trait_frequency_v1":
        raise AssertionError("mission3_metrics rarity score method is unsupported")
    if metrics["dog_rarity_tie_policy"] != "competition_rank_equal_scores_share_rank":
        raise AssertionError("mission3_metrics rarity tie policy is unsupported")
    if metrics["dog_rarity_trait_schema"] != "Background|Body|Neck|Mouth|Ears|Head|Eyes":
        raise AssertionError("mission3_metrics rarity trait schema is unsupported")
    if int(metrics["snapshot_confirmations"]) < 1:
        raise AssertionError("mission3_metrics snapshot_confirmations must be at least one")

    expected_feed_status = {"live": "ongoing", "ended_unsettled": "ended pending settlement"}.get(current_state, current_state)
    if text(feed.get("status")).lower() != expected_feed_status:
        raise AssertionError("auction_feed current row status differs from current_auction")

    assert_metric_cell("latest_block", metrics["latest_block"], index)
    assert_metric_cell("latest_block_time_utc", metrics["latest_block_time_utc"], index)
    assert_metric_cell("current_auction_token_id", metrics["current_auction_token_id"], index)
    assert_metric_cell("current_auction_status", metrics["current_auction_status"], index)
    assert_metric_cell("current_bid_eth", metrics["current_bid_eth"], index)
    assert_metric_cell("current_bid_usd", metrics["current_bid_usd"], index)
    assert_metric_cell("current_bidder", metrics["current_bidder"], index)
    assert_metric_cell("current_bidder_wallet", metrics["current_bidder_wallet"], index)
    assert_index_contains("current Dog heading", f"Dog #{current_dog_id}", index)
    assert_index_contains("current bid display", text(current.get("current_bid")), index)
    assert_index_contains("current high-bidder display", text(current.get("bidder")), index)
    assert_index_contains("current auction status", text(feed.get("status")), index)
    validate_reward_metrics(metrics, index, readme)
    validate_season6_metrics(metrics, index)
    expected_season6_readme = season6_readme_estimate_summary(metrics)
    if expected_season6_readme and readme.get("Season 6 SUP estimate if current bid wins") != expected_season6_readme:
        raise AssertionError("README Season 6 SUP estimate differs from mission3_metrics")

    if readme.get("Snapshot block") != metrics["latest_block"]:
        raise AssertionError("README Snapshot block differs from mission3_metrics latest_block")
    if iso_utc(readme.get("Snapshot time UTC")) != iso_utc(metrics["latest_block_time_utc"]):
        raise AssertionError("README Snapshot time UTC differs from mission3_metrics latest_block_time_utc")
    if readme.get("Current Dog") != f"Dog #{current_dog_id}":
        raise AssertionError("README Current Dog differs from current_auction")
    if readme.get("Current status", "").lower() != current_state:
        raise AssertionError("README Current status differs from current_auction")
    readme_bid_match = re.search(r"[0-9]+(?:\.[0-9]+)?", readme.get("Current bid", ""))
    if not readme_bid_match or decimal_value(readme_bid_match.group(0)) != decimal_value(metrics["current_bid_eth"]):
        raise AssertionError("README Current bid differs from mission3_metrics current_bid_eth")
    if readme.get("Current high bidder") != metrics["current_bidder"]:
        raise AssertionError("README Current high bidder differs from mission3_metrics current_bidder")

    if current_state == "live" and text(feed.get("status")).lower() != "ongoing":
        raise AssertionError("live current_auction row is not marked ongoing in auction_feed")
    if current_state in {"live", "ended_unsettled"}:
        expected_wallet = normalize_address(current.get("bidder_wallet"))
        if expected_wallet and expected_wallet != ZERO:
            if normalize_address(feed.get("bidder_winner_wallet")) != expected_wallet:
                raise AssertionError("auction_feed current row high-bidder wallet differs from current_auction")
            if normalize_address(latest.get("bidder_wallet")) != expected_wallet:
                raise AssertionError("current_latest_bid high-bidder wallet differs from current_auction")
        if text(feed.get("bidder_winner")) != text(current.get("bidder")):
            raise AssertionError("auction_feed current row high-bidder display differs from current_auction")
        if text(latest.get("bidder")) != text(current.get("bidder")):
            raise AssertionError("current_latest_bid high-bidder display differs from current_auction")
        if not decimals_equal(feed.get("amount_eth"), current.get("current_bid_eth")):
            raise AssertionError("auction_feed current row amount_eth differs from current_auction")
        if not decimals_equal(latest.get("latest_bid_eth"), current.get("current_bid_eth")):
            raise AssertionError("current_latest_bid amount differs from current_auction")
        expected_live_usd = first_optional_decimal(current.get("current_bid_usd"), metrics.get("current_bid_usd"), latest.get("latest_bid_usd"), feed.get("amount_usd"))
        if expected_live_usd is not None:
            if optional_decimal_value(feed.get("amount_usd")) != expected_live_usd:
                raise AssertionError("auction_feed current row amount_usd differs from current_auction")
            if optional_decimal_value(latest.get("latest_bid_usd")) != expected_live_usd:
                raise AssertionError("current_latest_bid USD amount differs from current_auction")
            if optional_decimal_value(current.get("current_bid_usd")) != expected_live_usd:
                raise AssertionError("current_auction current_bid_usd differs from auction_feed")
        if text(feed.get("bid")) != text(current.get("current_bid")):
            raise AssertionError("auction_feed current row bid display differs from current_auction")
        if iso_utc(feed.get("last_bid_utc")) != iso_utc(latest.get("bid_time_utc")):
            raise AssertionError("auction_feed last_bid_utc differs from current_latest_bid bid_time_utc")

    historical_rows = load_json(ROOT / "generated" / "historical_dog_search.json")
    expected_rarity_scores = validate_base_rarity_universe(historical_rows, metrics)
    historical = next(
        (row for row in historical_rows if isinstance(row, dict) and row.get("mission") == 3 and int(row.get("token_id", -1)) == current_dog_id),
        None,
    )
    if historical is None:
        raise AssertionError(f"historical_dog_search missing Mission 3 Dog #{current_dog_id}")
    if current_dog_id in expected_rarity_scores:
        if text(current.get("rarity")) != text(historical.get("rarity")):
            raise AssertionError("current_auction rarity differs from independently validated history")
        current_rarity_score = text(current.get("rarity_score"))
        if (
            not current_rarity_score
            or abs(Decimal(current_rarity_score) - expected_rarity_scores[current_dog_id])
            > Decimal("0.000001")
        ):
            raise AssertionError("current_auction rarity score is inconsistent")
    elif text(current.get("rarity_score")):
        raise AssertionError("current_auction publishes a rarity score without a verified rank")
    if current_state == "live":
        if normalize_address(historical.get("winner_wallet")) != normalize_address(feed.get("bidder_winner_wallet")):
            raise AssertionError("historical_dog_search current row wallet differs from auction_feed")
        if text(historical.get("winner")) != text(feed.get("bidder_winner")):
            raise AssertionError("historical_dog_search current row display differs from auction_feed")
        if text(historical.get("amount")) != text(feed.get("bid")):
            raise AssertionError("historical_dog_search current row amount differs from auction_feed")

    for table_name in [
        "mission3_metrics",
        "current_auction",
        "current_latest_bid",
        "current_auction_bid_history",
        "auction_feed",
        "historical_dog_search",
        "recent_bids",
        "auction_timeline",
        "auction_daily_activity",
        "auction_bidder_leaderboard",
        "season6_sup_current_bidder_status",
    ]:
        generated_path = ROOT / "generated" / f"{table_name}.json"
        public_path = ROOT / "public" / "generated" / f"{table_name}.json"
        if generated_path.exists() and public_path.exists() and generated_path.read_bytes() != public_path.read_bytes():
            raise AssertionError(f"public/generated/{table_name}.json differs from generated/{table_name}.json")

    unified_paths = [
        ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json",
        ROOT / "public" / "generated" / "unified_dog_search_index.json",
    ]
    expected_wallet = normalize_address(feed.get("bidder_winner_wallet"))
    expected_display = text(feed.get("bidder_winner"))
    expected_native = decimal_value(feed.get("amount_eth"))
    expected_usd = first_optional_decimal(feed.get("amount_usd"), latest.get("latest_bid_usd"), current.get("current_bid_usd"), metrics.get("current_bid_usd"))
    expected_usd_display = money_display(expected_usd) if expected_usd is not None else ""
    expected_last_bid = iso_utc(feed.get("last_bid_utc") or feed.get("auction_time_utc"))
    recent_rows_raw = load_json(RECENT_BIDS)
    recent_rows = [row for row in recent_rows_raw if isinstance(row, dict) and dog_id(row) == current_dog_id] if isinstance(recent_rows_raw, list) else []
    recent_rows.sort(key=lambda row: (text(row.get("bid_time_utc")), int(row.get("block_number") or 0)), reverse=True)
    recent_wallets = {normalize_address(row.get("bidder_wallet") or row.get("bidder")) for row in recent_rows}
    recent_wallets.discard("")
    latest_recent_tx = text(recent_rows[0].get("tx_hash")) if recent_rows else ""

    current_history_raw = load_json(ROOT / "generated" / "current_auction_bid_history.json")
    current_history = [row for row in current_history_raw if isinstance(row, dict) and dog_id(row) == current_dog_id] if isinstance(current_history_raw, list) else []
    current_history.sort(key=lambda row: (text(row.get("bid_time_utc")), int(row.get("block_number") or 0), int(row.get("log_index") or 0)), reverse=True)
    current_live_price = first_optional_decimal(current.get("eth_usd_price_live"))
    if current_state in {"live", "ended_unsettled"} and expected_wallet and expected_wallet != ZERO and expected_native > 0:
        if not current_history:
            raise AssertionError("current_auction_bid_history missing rows for current auction")
        latest_history = current_history[0]
        current_live_price = first_optional_decimal(current.get("eth_usd_price_live"), latest_history.get("eth_usd_price_live"))
        if normalize_address(latest_history.get("bidder_wallet")) != expected_wallet:
            raise AssertionError("current_auction_bid_history high-bidder wallet differs from current_auction")
        if decimal_value(latest_history.get("bid_eth")) != expected_native:
            raise AssertionError("current_auction_bid_history high bid ETH differs from current_auction")
        if expected_usd is not None and optional_decimal_value(latest_history.get("bid_usd")) != expected_usd:
            raise AssertionError("current_auction_bid_history high bid USD differs from current_auction")
        if text(latest_history.get("bid")) != text(current.get("current_bid")):
            raise AssertionError("current_auction_bid_history high bid display differs from current_auction")
        if text(latest_history.get("usd_estimate_source")) != "current_eth_usd_price":
            raise AssertionError("current_auction_bid_history should use current ETH/USD source for live auction rows")
        if text(latest_history.get("usd_estimate_confidence")).lower() in {"", "missing"}:
            raise AssertionError("current_auction_bid_history high bid USD confidence missing")
        for row in current_history:
            bid_usd = optional_decimal_value(row.get("bid_usd"))
            bid_eth = optional_decimal_value(row.get("bid_eth"))
            eth_usd = optional_decimal_value(row.get("eth_usd_price_live"))
            if bid_usd is None or bid_eth is None or eth_usd is None:
                continue
            calculated = (bid_eth * eth_usd).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if bid_usd != calculated:
                raise AssertionError("current_auction_bid_history bid_usd does not equal bid_eth * live ETH/USD")

        latest_history = current_history[0]
        if not recent_rows:
            raise AssertionError("recent_bids missing current auction bid rows")
        latest_recent = recent_rows[0]
        if text(latest_recent.get("tx_hash")) != text(latest_history.get("tx_hash")):
            raise AssertionError("recent_bids latest transaction differs from current_auction_bid_history")
        if normalize_address(latest_recent.get("bidder_wallet")) != expected_wallet:
            raise AssertionError("recent_bids latest bidder differs from current_auction")
        if decimal_value(latest_recent.get("bid_eth")) != expected_native:
            raise AssertionError("recent_bids latest amount differs from current_auction")

        timeline_rows = load_json(ROOT / "generated" / "auction_timeline.json")
        timeline = next(
            (row for row in timeline_rows if isinstance(row, dict) and dog_id(row) == current_dog_id),
            None,
        ) if isinstance(timeline_rows, list) else None
        if timeline is None:
            raise AssertionError("auction_timeline missing current auction row")
        if int(timeline.get("bids") or 0) != len(current_history):
            raise AssertionError("auction_timeline current bid count differs from current bid history")
        history_wallets = {normalize_address(row.get("bidder_wallet")) for row in current_history}
        history_wallets.discard("")
        if int(timeline.get("unique_bidders") or 0) != len(history_wallets):
            raise AssertionError("auction_timeline current unique bidder count differs from current bid history")
        history_total = sum((decimal_value(row.get("bid_eth")) for row in current_history), Decimal(0))
        if decimal_value(timeline.get("total_bid_eth")) != history_total:
            raise AssertionError("auction_timeline current total_bid_eth differs from current bid history")
        if decimal_value(timeline.get("high_bid_eth")) != max(decimal_value(row.get("bid_eth")) for row in current_history):
            raise AssertionError("auction_timeline current high_bid_eth differs from current bid history")

        bidder_rows = load_json(ROOT / "generated" / "auction_bidder_leaderboard.json")
        bidder_row = next(
            (row for row in bidder_rows if isinstance(row, dict) and normalize_address(row.get("bidder_wallet")) == expected_wallet),
            None,
        ) if isinstance(bidder_rows, list) else None
        if bidder_row is None:
            raise AssertionError("auction_bidder_leaderboard missing current high bidder")
        if int(bidder_row.get("latest_bid_token_id") or -1) != current_dog_id:
            raise AssertionError("auction_bidder_leaderboard current bidder latest Dog is stale")
        if iso_utc(bidder_row.get("latest_bid_utc")) != iso_utc(latest_history.get("bid_time_utc")):
            raise AssertionError("auction_bidder_leaderboard current bidder latest bid time is stale")

        current_day = text(latest_history.get("bid_time_utc"))[:10]
        day_history = [row for row in current_history if text(row.get("bid_time_utc"))[:10] == current_day]
        daily_rows = load_json(ROOT / "generated" / "auction_daily_activity.json")
        daily = next(
            (row for row in daily_rows if isinstance(row, dict) and text(row.get("activity_day")) == current_day),
            None,
        ) if isinstance(daily_rows, list) else None
        if daily is None:
            raise AssertionError("auction_daily_activity missing the current bid day")
        if int(daily.get("bids") or 0) < len(day_history):
            raise AssertionError("auction_daily_activity current-day bid count lags current bid history")
        if decimal_value(daily.get("bid_eth")) < sum((decimal_value(row.get("bid_eth")) for row in day_history), Decimal(0)):
            raise AssertionError("auction_daily_activity current-day bid volume lags current bid history")

    for path in unified_paths:
        unified_rows = load_json(path)
        if not isinstance(unified_rows, list):
            raise AssertionError(f"{path.relative_to(ROOT)} is not a list")
        sorted_rows = sorted([row for row in unified_rows if isinstance(row, dict)], key=unified_sort_key, reverse=True)
        if unified_rows != sorted_rows:
            raise AssertionError(f"{path.relative_to(ROOT)} is not sorted with only the actual current auction prioritized")
        for row in sorted_rows:
            rarity_payload = row.get("rarity") if isinstance(row.get("rarity"), dict) else {}
            rarity_display = text(rarity_payload.get("display"))
            if re.fullmatch(r"#\d+/\d+", rarity_display):
                if rarity_payload.get("scope") != "base_existing":
                    raise AssertionError(
                        f"{path.relative_to(ROOT)} Dog #{dog_id(row)} numeric rarity has no Base-existing scope"
                    )
                if int(rarity_payload.get("total") or 0) != rarity_universe:
                    raise AssertionError(
                        f"{path.relative_to(ROOT)} Dog #{dog_id(row)} rarity denominator contradicts metrics"
                    )
        ongoing_rows = [row for row in sorted_rows if row.get("mission") == 3 and archive_current_rank(row) == 1]
        if len(ongoing_rows) != 1 or dog_id(ongoing_rows[0]) != current_dog_id:
            raise AssertionError(f"{path.relative_to(ROOT)} must contain exactly one Mission 3 ongoing/current row for Dog #{current_dog_id}")
        required_mission3 = historical_mission3_required_ids(historical_rows if isinstance(historical_rows, list) else [])
        unified_mission3 = {dog_id(row) for row in sorted_rows if row.get("mission") == 3}
        missing_mission3 = sorted(required_mission3 - unified_mission3)
        if missing_mission3:
            raise AssertionError(f"{path.relative_to(ROOT)} missing Mission 3 historical archive rows: {missing_mission3[:20]}")

        for settled_row in sorted_rows:
            if settled_row.get("mission") != 3 or str(settled_row.get("status", "")).lower() != "settled":
                continue
            settlement = settled_row.get("settlement") if isinstance(settled_row.get("settlement"), dict) else {}
            settlement_time = settlement.get("block_time_utc")
            if settlement_time and iso_utc(settled_row.get("activity_time_utc")) != iso_utc(settlement_time):
                raise AssertionError(f"{path.relative_to(ROOT)} settled Dog #{dog_id(settled_row)} activity time is not the settlement block time")
            if settlement_time and text(settled_row.get("activity_time_basis")) != "settlement_block_time":
                raise AssertionError(f"{path.relative_to(ROOT)} settled Dog #{dog_id(settled_row)} activity basis is not settlement_block_time")

        unified = find_unified_current(path, current_dog_id)
        raw_who = unified.get("winner_or_high_bidder")
        who: dict[str, Any] = raw_who if isinstance(raw_who, dict) else {}
        raw_amount = unified.get("amount")
        amount: dict[str, Any] = raw_amount if isinstance(raw_amount, dict) else {}
        if normalize_address(who.get("wallet")) != expected_wallet:
            raise AssertionError(f"{path.relative_to(ROOT)} current row wallet differs from auction_feed")
        if text(who.get("display")) != expected_display:
            raise AssertionError(f"{path.relative_to(ROOT)} current row display differs from auction_feed")
        if decimal_value(amount.get("native")) != expected_native:
            raise AssertionError(f"{path.relative_to(ROOT)} current row native amount differs from auction_feed")
        if expected_usd is not None:
            actual_usd = optional_decimal_value(amount.get("usd_estimate"))
            quoted_price = optional_decimal_value(amount.get("usd_estimate_price_usd"))
            if actual_usd is None or quoted_price is None:
                raise AssertionError(f"{path.relative_to(ROOT)} current row exact USD quote provenance missing")
            if current_live_price is None or quoted_price != current_live_price:
                raise AssertionError(f"{path.relative_to(ROOT)} current row ETH/USD quote differs from current surfaces")
            exact_usd = (expected_native * quoted_price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
            if actual_usd != exact_usd:
                raise AssertionError(f"{path.relative_to(ROOT)} current row USD estimate is not native amount times exact quote")
            if actual_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != expected_usd.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):
                raise AssertionError(f"{path.relative_to(ROOT)} current row displayed USD differs from auction_feed")
            if text(amount.get("usd_estimate_display")) != expected_usd_display:
                raise AssertionError(f"{path.relative_to(ROOT)} current row USD estimate display differs from auction_feed")
            if text(amount.get("usd_estimate_source")) == "" or text(amount.get("usd_estimate_confidence")).lower() == "missing":
                raise AssertionError(f"{path.relative_to(ROOT)} current row USD estimate provenance missing")
        if iso_utc(unified.get("activity_time_utc")) != expected_last_bid:
            raise AssertionError(f"{path.relative_to(ROOT)} current row activity time differs from auction_feed")
        raw_bid_stats = unified.get("bid_stats")
        bid_stats: dict[str, Any] = raw_bid_stats if isinstance(raw_bid_stats, dict) else {}
        if recent_rows:
            if int(bid_stats.get("bid_count") or 0) < len(recent_rows):
                raise AssertionError(f"{path.relative_to(ROOT)} current row bid_count lags recent_bids")
            if int(bid_stats.get("unique_bidder_count") or 0) < len(recent_wallets):
                raise AssertionError(f"{path.relative_to(ROOT)} current row unique_bidder_count lags recent_bids")
            if latest_recent_tx and latest_recent_tx not in (unified.get("bid_tx_hashes") or []):
                raise AssertionError(f"{path.relative_to(ROOT)} current row bid_tx_hashes missing latest recent bid tx")
        search_text = text(unified.get("search_text")).lower()
        required_terms = [expected_wallet, expected_display.lower(), f"{expected_native.normalize()} eth"]
        if expected_usd is not None:
            required_terms.extend([str(expected_usd.normalize()), expected_usd_display.lower()])
        for required in required_terms:
            if required and required not in search_text:
                raise AssertionError(f"{path.relative_to(ROOT)} current row search_text missing {required!r}")
        if latest_recent_tx and latest_recent_tx not in search_text:
            raise AssertionError(f"{path.relative_to(ROOT)} current row search_text missing latest recent bid tx")

        stale_top = archive_top_bid_for_dog(current_dog_id)
        if stale_top:
            stale_wallet = normalize_address(stale_top.get("bidder"))
            stale_display = identity_display(stale_wallet).lower()
            stale_amount = decimal_value(stale_top.get("amount_eth") or stale_top.get("native_amount") or stale_top.get("bid_eth"))
            if stale_wallet and stale_wallet != expected_wallet and stale_wallet in search_text:
                raise AssertionError(f"{path.relative_to(ROOT)} search_text still contains stale archive bidder wallet {stale_wallet}")
            if stale_display and stale_display != expected_display.lower() and stale_display in search_text:
                raise AssertionError(f"{path.relative_to(ROOT)} search_text still contains stale archive bidder display {stale_display}")
            stale_amount_term = f"{stale_amount.normalize()} eth"
            if stale_amount != expected_native and stale_amount_term in search_text:
                raise AssertionError(f"{path.relative_to(ROOT)} search_text still contains stale archive bid amount {stale_amount_term}")

        for feed_row in feed_rows:
            feed_status = text(feed_row.get("status")).lower()
            if "settled" not in feed_status and "ended" not in feed_status:
                continue
            row_usd = first_optional_decimal(feed_row.get("amount_usd_at_event"), feed_row.get("amount_usd"))
            row_native = optional_decimal_value(feed_row.get("amount_eth"))
            if row_usd is None or row_native is None:
                continue
            row_dog_id = dog_id(feed_row)
            unified_row = find_unified_mission3(path, row_dog_id)
            unified_amount_raw = unified_row.get("amount")
            unified_amount: dict[str, Any] = unified_amount_raw if isinstance(unified_amount_raw, dict) else {}
            actual_usd = optional_decimal_value(unified_amount.get("usd_estimate"))
            if actual_usd is None or money_display(actual_usd) != money_display(row_usd):
                raise AssertionError(f"{path.relative_to(ROOT)} recent archive USD estimate differs from auction_feed for Dog #{row_dog_id}")
            if text(unified_amount.get("usd_estimate_display")) != money_display(row_usd):
                raise AssertionError(f"{path.relative_to(ROOT)} recent archive USD estimate display differs from auction_feed for Dog #{row_dog_id}")
            if optional_decimal_value(unified_amount.get("native")) != row_native:
                raise AssertionError(f"{path.relative_to(ROOT)} recent archive native amount differs from auction_feed for Dog #{row_dog_id}")
            if text(unified_amount.get("usd_estimate_source")) == "" or text(unified_amount.get("usd_estimate_confidence")).lower() == "missing":
                raise AssertionError(f"{path.relative_to(ROOT)} recent archive USD provenance missing for Dog #{row_dog_id}")
            if feed_status == "settled":
                for field in ["amount_usd_at_event", "eth_usd_price_at_event", "eth_usd_price_date_utc"]:
                    if text(feed_row.get(field)) and text(unified_amount.get(field)) == "":
                        raise AssertionError(f"{path.relative_to(ROOT)} recent archive USD event field {field} missing for Dog #{row_dog_id}")

    observed_state_check: dict[str, Any] = {}
    observed_state_path = ROOT / ".local" / "mission3_onchain_tracker_state.json"
    if observed_state_path.exists():
        observed_state = load_json(observed_state_path, {})
        if isinstance(observed_state, dict) and observed_state.get("last_observed_token_id"):
            observed_state_check = validate_current_surface_against_observed_state(observed_state, root=ROOT)

    return {
        "current_dog": f"Dog #{current_dog_id}",
        "auction_state": current_state,
        "high_bidder": expected_display,
        "bid_eth": str(expected_native.normalize()),
        "feed_rows_for_current_dog": 1,
        "refresh_status_result": text(refresh_status.get("last_refresh_result")),
        "observed_state_check": observed_state_check,
        "mission3_archive_parity": archive_parity,
        "checked": [str(path.relative_to(ROOT)) for path in unified_paths]
        + ["generated/current_auction.json", "generated/current_latest_bid.json", "generated/auction_feed.json", "generated/historical_dog_search.json", "generated/refresh_status.json"],
    }


def main() -> int:
    print(json.dumps(validate_current_surface(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
