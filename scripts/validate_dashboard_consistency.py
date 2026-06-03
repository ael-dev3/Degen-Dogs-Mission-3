#!/usr/bin/env python3
"""Validate that live/current auction artifacts agree across dashboard surfaces."""
from __future__ import annotations

import csv
import importlib.util
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x0000000000000000000000000000000000000000"
RECENT_BIDS = ROOT / "generated" / "recent_bids.json"
REFRESH_TELEMETRY_PATH = ROOT / "scripts" / "refresh_telemetry.py"


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


def reward_apr_display(apr: Decimal | None) -> str:
    if apr is None or apr <= 0:
        return "N/A"
    rounded = apr.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return f"≈{rounded:,.0f}% APR"


def decimals_equal(left: Any, right: Any) -> bool:
    return decimal_value(left) == decimal_value(right)


def iso_utc(value: Any) -> str:
    raw = text(value)
    if not raw:
        return ""
    raw = raw.replace(" ", "T")
    return raw if raw.endswith("Z") else f"{raw}Z"


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


def find_current_feed_row(feed_rows: list[dict[str, Any]], current_dog_id: int) -> dict[str, Any]:
    matches = [row for row in feed_rows if dog_id(row) == current_dog_id]
    if len(matches) != 1:
        raise AssertionError(f"auction_feed has {len(matches)} rows for current Dog #{current_dog_id}, expected exactly 1")
    return matches[0]


def find_unified_current(path: Path, current_dog_id: int) -> dict[str, Any]:
    rows = load_json(path)
    if not isinstance(rows, list):
        raise AssertionError(f"{path.relative_to(ROOT)} is not a JSON list")
    for row in rows:
        if isinstance(row, dict) and row.get("mission") == 3 and row.get("dog_id") == current_dog_id:
            return row
    raise AssertionError(f"{path.relative_to(ROOT)} missing Mission 3 Dog #{current_dog_id}")


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

    basis_display = "Observed 133-Dog stream"
    reward_surface = reward_strip_surface(index)
    if not reward_surface:
        raise AssertionError("index.html missing reward-strip APR/payback surface")
    if basis_display not in reward_surface:
        raise AssertionError(f"index.html missing observed reward basis display: {basis_display!r}")

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

    if int(metrics["current_auction_token_id"]) != current_dog_id:
        raise AssertionError("mission3_metrics current_auction_token_id differs from current_auction")
    if metrics["current_auction_status"].lower() != current_state:
        raise AssertionError("mission3_metrics current_auction_status differs from current_auction")
    if not decimals_equal(metrics["current_bid_eth"], current.get("current_bid_eth")):
        raise AssertionError("mission3_metrics current_bid_eth differs from current_auction")
    if text(metrics["current_bidder"]) != text(current.get("bidder")):
        raise AssertionError("mission3_metrics current_bidder differs from current_auction")
    if normalize_address(metrics["current_bidder_wallet"]) != normalize_address(current.get("bidder_wallet")):
        raise AssertionError("mission3_metrics current_bidder_wallet differs from current_auction")
    if text(metrics["latest_block"]) != text(current.get("latest_block")):
        raise AssertionError("mission3_metrics latest_block differs from current_auction")
    if iso_utc(metrics["latest_block_time_utc"]) != iso_utc(current.get("latest_block_time_utc")):
        raise AssertionError("mission3_metrics latest_block_time_utc differs from current_auction")

    expected_feed_status = {"live": "ongoing", "ended_unsettled": "ended pending settlement"}.get(current_state, current_state)
    if text(feed.get("status")).lower() != expected_feed_status:
        raise AssertionError("auction_feed current row status differs from current_auction")

    assert_metric_cell("latest_block", metrics["latest_block"], index)
    assert_metric_cell("latest_block_time_utc", metrics["latest_block_time_utc"], index)
    assert_metric_cell("current_auction_token_id", metrics["current_auction_token_id"], index)
    assert_metric_cell("current_auction_status", metrics["current_auction_status"], index)
    assert_metric_cell("current_bid_eth", metrics["current_bid_eth"], index)
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
        if text(feed.get("bid")) != text(current.get("current_bid")):
            raise AssertionError("auction_feed current row bid display differs from current_auction")
        if iso_utc(feed.get("last_bid_utc")) != iso_utc(latest.get("bid_time_utc")):
            raise AssertionError("auction_feed last_bid_utc differs from current_latest_bid bid_time_utc")

    historical_rows = load_json(ROOT / "generated" / "historical_dog_search.json")
    historical = next(
        (row for row in historical_rows if isinstance(row, dict) and row.get("mission") == 3 and int(row.get("token_id", -1)) == current_dog_id),
        None,
    )
    if historical is None:
        raise AssertionError(f"historical_dog_search missing Mission 3 Dog #{current_dog_id}")
    if current_state == "live":
        if normalize_address(historical.get("winner_wallet")) != normalize_address(feed.get("bidder_winner_wallet")):
            raise AssertionError("historical_dog_search current row wallet differs from auction_feed")
        if text(historical.get("winner")) != text(feed.get("bidder_winner")):
            raise AssertionError("historical_dog_search current row display differs from auction_feed")
        if text(historical.get("amount")) != text(feed.get("bid")):
            raise AssertionError("historical_dog_search current row amount differs from auction_feed")

    for table_name in ["mission3_metrics", "current_auction", "current_latest_bid", "auction_feed", "historical_dog_search", "recent_bids"]:
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
    expected_last_bid = iso_utc(feed.get("last_bid_utc") or feed.get("auction_time_utc"))
    recent_rows_raw = load_json(RECENT_BIDS)
    recent_rows = [row for row in recent_rows_raw if isinstance(row, dict) and dog_id(row) == current_dog_id] if isinstance(recent_rows_raw, list) else []
    recent_rows.sort(key=lambda row: (text(row.get("bid_time_utc")), int(row.get("block_number") or 0)), reverse=True)
    recent_wallets = {normalize_address(row.get("bidder_wallet") or row.get("bidder")) for row in recent_rows}
    recent_wallets.discard("")
    latest_recent_tx = text(recent_rows[0].get("tx_hash")) if recent_rows else ""
    for path in unified_paths:
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
        for required in [expected_wallet, expected_display.lower(), f"{expected_native.normalize()} eth"]:
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

    return {
        "current_dog": f"Dog #{current_dog_id}",
        "auction_state": current_state,
        "high_bidder": expected_display,
        "bid_eth": str(expected_native.normalize()),
        "feed_rows_for_current_dog": 1,
        "refresh_status_result": text(refresh_status.get("last_refresh_result")),
        "checked": [str(path.relative_to(ROOT)) for path in unified_paths]
        + ["generated/current_auction.json", "generated/current_latest_bid.json", "generated/auction_feed.json", "generated/historical_dog_search.json", "generated/refresh_status.json"],
    }


def main() -> int:
    print(json.dumps(validate_current_surface(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
