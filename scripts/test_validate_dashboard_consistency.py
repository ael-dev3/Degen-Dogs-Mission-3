#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "validate_dashboard_consistency.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("validate_dashboard_consistency", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def hidden_metrics_table(metrics: dict[str, str]) -> str:
    rows = "".join(f"<tr><td>{key}</td><td>{value}</td></tr>" for key, value in metrics.items())
    return f'<table data-table="mission3_metrics" hidden><tbody>{rows}</tbody></table>'


def write_fixture(
    root: Path,
    *,
    apr_pct: str = "195.58",
    daily_roi_pct: str = "0.5358",
    payback_days: str = "186.63",
    index_dog: str = "Dog #729",
    index_apr: str = "≈196% APR",
    apr_display: str | None = None,
    reward_basis_dogs: str = "133",
    feed_bidder: str = "@0xael.eth",
    season6_estimate_display: str = "≈1,000 SUP",
    season6_by_winner_capped: str = "1000",
) -> None:
    wallet = "0x76d0e7a13248945ee9f808b4a472262b28778942"
    apr_display_value = apr_display or index_apr
    metrics = {
        "latest_block": "46732183",
        "latest_block_time_utc": "2026-05-31 18:55:13",
        "current_auction_token_id": "729",
        "current_auction_status": "live",
        "current_bid_eth": "0.01",
        "current_bid_usd": "19.98",
        "current_bidder": "@0xael.eth",
        "current_bidder_wallet": wallet,
        "current_auction_end_utc": "2026-05-31 20:40:09",
        "woof_usd_price": "0.0000005",
        "sup_usd_price": "0.02",
        "reward_basis_dogs": reward_basis_dogs,
        "reward_basis_source": "observed_stream_snapshot_133_dogs",
        "reward_snapshot_utc": "2026-06-02T20:55:15Z",
        "reward_excludes": "woof_vault_bonus",
        "reward_observed_dogs_count": reward_basis_dogs,
        "reward_observed_woof_flow_per_day": "20494201.3",
        "reward_observed_sup_flow_per_day": "199.58",
        "reward_observed_woof_per_dog_per_day": "154091.739097744361",
        "reward_observed_sup_per_dog_per_day": "1.5006015037593985",
        "reward_woof_per_dog_per_day": "154091.739097744361",
        "reward_sup_per_dog_per_day": "1.5006015037593985",
        "reward_total_per_dog_usd_per_day": "0.107058",
        "reward_current_bid_payback_days": payback_days,
        "reward_current_bid_daily_roi_pct": daily_roi_pct,
        "reward_current_bid_apr_pct": apr_pct,
        "reward_current_bid_apr_display": apr_display_value,
        "season6_sup_status": "live_estimate",
        "season6_sup_enabled": "true",
        "season6_sup_token": "0xa69f80524381275a7ffdb3ae01c54150644c8792",
        "season6_sup_usd_price": "2",
        "season6_sup_total_allocation": "251340",
        "season6_sup_wallet_cap": "12500",
        "season6_sup_xp_per_win": "100",
        "season6_sup_start_utc": "2026-06-02T00:00:00Z",
        "season6_sup_end_utc": "2026-09-01T00:00:00Z",
        "season6_sup_reward_start_delay_days": "0",
        "season6_sup_projection_model": "time_weighted_xp_with_expected_future_daily_auctions",
        "season6_sup_future_dilution_enabled": "true",
        "season6_sup_expected_future_settlement_interval_seconds": "86400",
        "season6_sup_settled_win_count_to_date": "1",
        "season6_sup_current_bidder_wallet": wallet,
        "season6_sup_current_bidder_prior_s6_wins": "1",
        "season6_sup_current_bidder_prior_s6_xp": "100",
        "season6_sup_current_bid_estimated_win_time_utc": "2026-05-31T20:40:09Z",
        "season6_sup_current_bid_estimated_raw_incremental_sup": "1000",
        "season6_sup_current_bid_estimated_cap_aware_sup": "1000",
        "season6_sup_current_bid_estimated_cap_aware_usd": "2000",
        "season6_sup_current_bid_projected_total_without_win_sup": "1000",
        "season6_sup_current_bid_projected_total_with_win_sup": "2000",
        "season6_sup_current_bid_cap_remaining_before_win_sup": "11500",
        "season6_sup_estimate_status": "estimated",
        "season6_sup_current_bid_estimate_status": "estimated",
        "season6_sup_total_allocated": "251340",
        "season6_sup_cap_per_wallet": "12500",
        "season6_sup_cap_percent_label": "5% cap",
        "season6_sup_xp_per_settled_win": "100",
        "season6_sup_xp_start_utc": "2026-06-02T00:00:00Z",
        "season6_sup_reward_start_utc": "2026-06-02T00:00:00Z",
        "season6_sup_campaign_end_utc": "2026-09-01T00:00:00Z",
        "season6_sup_days_remaining": "92",
        "season6_sup_confirmed_wins": "1",
        "season6_sup_confirmed_wallets": "1",
        "season6_sup_total_xp_confirmed": "100",
        "season6_sup_raw_allocated_to_date": "100",
        "season6_sup_raw_projected_full_allocated": "1000",
        "season6_sup_capped_projected_full_allocated": "1000",
        "season6_sup_unallocated_due_to_zero_xp": "0",
        "season6_sup_cap_overflow_policy": "no_redistribution_assumed",
        "season6_sup_usd_price_used": "2",
        "season6_sup_usd_source_used": "unit-test",
        "season6_current_bidder_prior_wins": "1",
        "season6_current_bidder_cap_remaining_sup": "11500",
        "season6_current_bidder_projected_raw_sup_if_wins": "1000",
        "season6_current_bidder_projected_capped_sup_if_wins": "1000",
        "season6_current_bidder_projected_raw_usd_if_wins": "2000",
        "season6_current_bidder_projected_capped_usd_if_wins": "2000",
    }
    metric_rows = [{"metric": key, "value": value} for key, value in metrics.items()]
    write_text(root / "generated" / "mission3_metrics.csv", "metric,value\n" + "".join(f"{key},{value}\n" for key, value in metrics.items()))
    write_json(root / "generated" / "mission3_metrics.json", metric_rows)
    write_json(root / "public" / "generated" / "mission3_metrics.json", metric_rows)

    current = {
        "token_id": 729,
        "current_bid": "0.01000 ETH ($20)",
        "current_bid_eth": 0.01,
        "current_bid_usd": 19.98,
        "bidder": "@0xael.eth",
        "bidder_wallet": wallet,
        "auction_state": "live",
        "end_time_utc": "2026-05-31 20:40:09",
        "latest_block": 46732183,
        "latest_block_time_utc": "2026-05-31 18:55:13",
    }
    write_json(root / "generated" / "current_auction.json", [current])
    refresh_status = {
        "schema_version": 1,
        "kind": "refresh_status",
        "site_url": "https://ael-dev3.github.io/Degen-Dogs-Mission-3/",
        "last_successful_refresh_time_utc": "2026-05-31T18:55:13Z",
        "latest_generated_block": 46732183,
        "latest_generated_block_time_utc": "2026-05-31T18:55:13Z",
        "trigger": "unit_test",
        "refresh_reason": "fixture",
        "current_dog_token_id": 729,
        "current_bid_eth": "0.01",
        "current_high_bidder": "@0xael.eth",
        "current_high_bidder_wallet": wallet,
        "current_auction_status": "live",
        "current_auction_end_time_utc": "2026-05-31T20:40:09Z",
        "last_refresh_result": "success_generated",
    }
    write_json(root / "generated" / "refresh_status.json", refresh_status)
    write_json(root / "public" / "generated" / "refresh_status.json", refresh_status)
    write_json(root / "generated" / "current_latest_bid.json", [{"latest_bid_eth": 0.01, "latest_bid_usd": 19.98, "bidder": "@0xael.eth", "bidder_wallet": wallet, "bid_time_utc": "2026-05-30 18:40:23"}])
    write_json(root / "generated" / "auction_feed.json", [{
        "status": "ongoing",
        "dog": "Dog #729",
        "bidder_winner": feed_bidder,
        "bidder_winner_wallet": wallet,
        "bid": "0.01000 ETH ($20)",
        "amount_eth": 0.01,
        "amount_usd": 19.98,
        "auction_time_utc": "2026-05-30 18:40:23",
        "auction_end_utc": "2026-05-31 20:40:09",
        "last_bid_utc": "2026-05-30 18:40:23",
    }])
    write_json(root / "generated" / "historical_dog_search.json", [{"mission": 3, "token_id": 729, "winner": "@0xael.eth", "winner_wallet": wallet, "amount": "0.01000 ETH ($20)"}])
    write_json(root / "generated" / "recent_bids.json", [])
    season6_winner = {
        "winner_wallet": wallet,
        "winner_display": "@0xael.eth",
        "farcaster_username": "0xael.eth",
        "season6_wins_confirmed": 1,
        "season6_xp_confirmed": 100,
        "season6_raw_sup_earned_to_date": "100",
        "season6_raw_sup_projected_full": "1000",
        "season6_capped_sup_projected_full": season6_by_winner_capped,
        "season6_cap_sup": "12500",
        "season6_cap_remaining_sup": "11500",
        "season6_cap_limited": "false",
        "season6_raw_usd_earned_to_date": "200",
        "season6_raw_usd_projected_full": "2000",
        "season6_capped_usd_projected_full": "2000",
        "first_s6_win_time_utc": "2026-06-02T00:00:00Z",
        "latest_s6_win_time_utc": "2026-06-02T00:00:00Z",
        "season6_wallet_note": "wallet-level estimate",
    }
    season6_auction = {
        "auction_id": 729,
        "token_id": 729,
        "dog": "Dog #729",
        "winner_wallet": wallet,
        "winner_display": "@0xael.eth",
        "settled_time_utc": "2026-06-02T00:00:00Z",
        "winning_bid_eth": "0.01",
        "winning_bid_usd": "19.98",
        "season6_xp": 100,
        "season6_raw_sup_earned_to_date": "100",
        "season6_raw_sup_projected_full": "1000",
        "season6_capped_sup_projected_full": "1000",
        "season6_raw_usd_earned_to_date": "200",
        "season6_raw_usd_projected_full": "2000",
        "season6_capped_usd_projected_full": "2000",
        "cap_limited_by_wallet": "false",
    }
    season6_status = {
        "current_auction_token_id": 729,
        "current_bidder_wallet": wallet,
        "current_bidder_display": "@0xael.eth",
        "current_bid_eth": "0.01",
        "current_bid_usd": "19.98",
        "current_auction_end_utc": "2026-05-31 20:40:09",
        "prior_s6_wins_confirmed": 1,
        "prior_s6_xp_confirmed": 100,
        "prior_s6_raw_sup_projected_full": "1000",
        "prior_s6_capped_sup_projected_full": "1000",
        "prior_s6_cap_remaining_sup": "11500",
        "projected_s6_wins_if_current_bid_wins": 2,
        "projected_s6_xp_if_current_bid_wins": 200,
        "projected_raw_sup_if_current_bid_wins": "2000",
        "projected_capped_sup_if_current_bid_wins": "2000",
        "projected_cap_remaining_sup_if_current_bid_wins": "10500",
        "projected_raw_usd_if_current_bid_wins": "4000",
        "projected_capped_usd_if_current_bid_wins": "4000",
        "projected_total_without_current_win_sup": "1000",
        "projected_total_with_current_win_sup": "2000",
        "estimated_raw_incremental_sup": "1000",
        "estimated_cap_aware_incremental_sup": "1000",
        "estimated_cap_aware_incremental_usd": "2000",
        "cap_remaining_before_current_win_sup": "11500",
        "future_dilution_enabled": "true",
        "expected_future_settlement_interval_seconds": "86400",
        "current_bidder_cap_status": "estimated",
        "estimate_status": "estimated",
        "projection_note": "cap-aware incremental estimate; future daily dilution projected",
    }
    for folder in (root / "generated", root / "public" / "generated"):
        write_json(folder / "season6_sup_by_winner.json", [season6_winner])
        write_json(folder / "season6_sup_rewards_by_auction.json", [season6_auction])
        write_json(folder / "season6_sup_current_bidder_status.json", [season6_status])
    unified_row = {
        "mission": 3,
        "dog_id": 729,
        "winner_or_high_bidder": {"wallet": wallet, "display": "@0xael.eth"},
        "amount": {
            "native": "0.01",
            "native_symbol": "ETH",
            "usd_estimate": "19.98",
            "usd_estimate_display": "$19.98",
            "usd_estimate_source": "generated_auction_feed",
            "usd_estimate_confidence": "medium",
        },
        "activity_time_utc": "2026-05-30T18:40:23Z",
        "bid_stats": {"bid_count": 0, "unique_bidder_count": 0},
        "bid_tx_hashes": [],
        "search_text": f"dog 729 {wallet} @0xael.eth 0.01 eth 19.98 $19.98",
    }
    write_json(root / "archive" / "data" / "generated" / "unified_dog_search_index.json", [unified_row])
    write_json(root / "public" / "generated" / "unified_dog_search_index.json", [unified_row])
    write_json(root / "archive" / "data" / "identity" / "wallet_profiles.json", {})
    write_json(root / "archive" / "mission3" / "data" / "generated" / "mission3_auction_bids.json", [])

    index = (
        f"<html><body><h1>{index_dog}</h1><span>0.01000 ETH ($20)</span>"
        f"<span>@0xael.eth</span><span>ongoing</span><span>2026-05-31 20:40:09</span>"
        f"<section class=\"reward-strip\">"
        f"<span class=\"reward-tile season6-sup-estimate\"><b>Season 6 SUP estimate</b>"
        f"<strong>{season6_estimate_display}<span>≈$2,000 if current bid wins</span></strong>"
        f"<em>Adjusted for prior S6 wins; estimate only.</em></span>"
        f"<span><b>Bid payback</b><strong><span>≈187 days</span>"
        f"<span>{index_apr}</span></strong><em>Simple APR estimate. Annualized from observed per-Dog daily WOOF + SUP flow; excludes WOOF Vault Bonus; does not compound; not guaranteed future return.</em></span></section>"
        f"{hidden_metrics_table(metrics)}</body></html>"
    )
    write_text(root / "index.html", index)
    write_text(root / "README.md", """# Fixture\n\n## Current snapshot\n\n| Field | Value |\n| --- | --- |\n| Snapshot block | 46732183 |\n| Snapshot time UTC | 2026-05-31 18:55:13 |\n| Current Dog | Dog #729 |\n| Current status | live |\n| Current bid | 0.01 ETH ($19.98) |\n| Current high bidder | @0xael.eth |\n| Bid payback / APR | ≈187 days / ≈196% APR |\n| Season 6 SUP estimate if current bid wins | ≈1,000 SUP / ≈$2,000 |\n\n## Next\n""")


def run_validation(root: Path) -> dict[str, Any]:
    validator = load_module()
    validator.ROOT = root
    validator.RECENT_BIDS = root / "generated" / "recent_bids.json"
    return validator.validate_current_surface()


def assert_raises_contains(fn, text: str) -> None:
    try:
        fn()
    except AssertionError as exc:
        assert text in str(exc)
    else:
        raise AssertionError(f"expected AssertionError containing {text!r}")


def test_validate_current_surface_accepts_consistent_apr_fixture() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        result = run_validation(root)
        assert result["current_dog"] == "Dog #729"


def test_validator_catches_apr_math_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, apr_pct="999.00", index_apr="≈999% APR")
        assert_raises_contains(lambda: run_validation(root), "reward_current_bid_apr_pct")


def test_validator_catches_apr_mismatch_between_metrics_and_rendered_html() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, index_apr="≈999% APR", apr_display="≈196% APR")
        assert_raises_contains(lambda: run_validation(root), "reward APR display")


def test_validator_catches_stale_current_dog_render() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, index_dog="Dog #728")
        assert_raises_contains(lambda: run_validation(root), "current Dog heading")


def test_validator_catches_stale_current_bidder_generated_surface() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, feed_bidder="@stale")
        assert_raises_contains(lambda: run_validation(root), "high-bidder display")


def test_validator_catches_unified_current_row_missing_live_usd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        for rel in [
            "archive/data/generated/unified_dog_search_index.json",
            "public/generated/unified_dog_search_index.json",
        ]:
            rows = json.loads((root / rel).read_text(encoding="utf-8"))
            rows[0]["amount"]["usd_estimate"] = None
            rows[0]["amount"]["usd_estimate_display"] = None
            rows[0]["amount"]["usd_estimate_source"] = None
            rows[0]["amount"]["usd_estimate_confidence"] = "missing"
            write_json(root / rel, rows)
        assert_raises_contains(lambda: run_validation(root), "current row USD estimate")


def test_validator_catches_unified_current_row_bad_live_usd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        for rel in [
            "archive/data/generated/unified_dog_search_index.json",
            "public/generated/unified_dog_search_index.json",
        ]:
            rows = json.loads((root / rel).read_text(encoding="utf-8"))
            rows[0]["amount"]["usd_estimate"] = "1.00"
            rows[0]["amount"]["usd_estimate_display"] = "$1.00"
            write_json(root / rel, rows)
        assert_raises_contains(lambda: run_validation(root), "current row USD estimate")


def test_validator_catches_current_latest_bid_missing_live_usd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        latest = json.loads((root / "generated" / "current_latest_bid.json").read_text(encoding="utf-8"))
        latest[0]["latest_bid_usd"] = None
        write_json(root / "generated" / "current_latest_bid.json", latest)
        assert_raises_contains(lambda: run_validation(root), "current_latest_bid USD amount")


def test_validator_catches_auction_feed_bad_live_usd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        feed = json.loads((root / "generated" / "auction_feed.json").read_text(encoding="utf-8"))
        feed[0]["amount_usd"] = "1.00"
        write_json(root / "generated" / "auction_feed.json", feed)
        assert_raises_contains(lambda: run_validation(root), "auction_feed current row amount_usd differs from current_auction")


def test_validator_catches_stale_season6_rendered_projection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, season6_estimate_display="≈999 SUP")
        assert_raises_contains(lambda: run_validation(root), "Season 6 compact card mismatch")


def test_validator_catches_stale_season6_readme_projection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        stale_readme = (root / "README.md").read_text(encoding="utf-8").replace(
            "| Season 6 SUP estimate if current bid wins | ≈1,000 SUP / ≈$2,000 |",
            "| Season 6 SUP estimate if current bid wins | ≈999 SUP / ≈$1,998 |",
        )
        write_text(root / "README.md", stale_readme)
        assert_raises_contains(lambda: run_validation(root), "README Season 6 SUP estimate")


def test_validator_catches_season6_capped_value_over_configured_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, season6_by_winner_capped="13000")
        assert_raises_contains(lambda: run_validation(root), "Season 6 capped SUP exceeds configured cap")


def test_validator_catches_stale_observed_reward_basis_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, reward_basis_dogs="141")
        assert_raises_contains(lambda: run_validation(root), "observed 133-Dog reward basis")


def test_validator_catches_stale_observed_per_dog_reward_math() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        metric_rows = json.loads((root / "generated" / "mission3_metrics.json").read_text(encoding="utf-8"))
        for row in metric_rows:
            if row.get("metric") == "reward_woof_per_dog_per_day":
                row["value"] = "158351.896454"
        write_json(root / "generated" / "mission3_metrics.json", metric_rows)
        write_json(root / "public" / "generated" / "mission3_metrics.json", metric_rows)
        csv_lines = ["metric,value"] + [f"{row['metric']},{row['value']}" for row in metric_rows]
        write_text(root / "generated" / "mission3_metrics.csv", "\n".join(csv_lines) + "\n")
        stale_index = (root / "index.html").read_text(encoding="utf-8").replace(
            "<td>reward_woof_per_dog_per_day</td><td>154091.739097744361</td>",
            "<td>reward_woof_per_dog_per_day</td><td>158351.896454</td>",
        )
        write_text(root / "index.html", stale_index)
        assert_raises_contains(lambda: run_validation(root), "reward_woof_per_dog_per_day")


def test_validator_accepts_season6_no_current_bid_neutral_card() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for folder in (root / "generated", root / "public" / "generated"):
            write_json(folder / "season6_sup_by_winner.json", [])
            write_json(folder / "season6_sup_rewards_by_auction.json", [])
            write_json(folder / "season6_sup_current_bidder_status.json", [])
        metrics = {
            "season6_sup_status": "live_estimate",
            "season6_sup_enabled": "true",
            "season6_sup_token": "0xa69f80524381275a7ffdb3ae01c54150644c8792",
            "season6_sup_usd_price": "2",
            "season6_sup_total_allocation": "251340",
            "season6_sup_wallet_cap": "12500",
            "season6_sup_xp_per_win": "100",
            "season6_sup_start_utc": "2026-06-02T00:00:00Z",
            "season6_sup_end_utc": "2026-09-01T00:00:00Z",
            "season6_sup_reward_start_delay_days": "0",
            "season6_sup_settled_win_count_to_date": "1",
            "season6_sup_current_bidder_wallet": "",
            "season6_sup_current_bidder_prior_s6_wins": "0",
            "season6_sup_current_bidder_prior_s6_xp": "0",
            "season6_sup_current_bid_estimated_win_time_utc": "N/A",
            "season6_sup_current_bid_estimated_raw_incremental_sup": "0",
            "season6_sup_current_bid_estimated_cap_aware_sup": "0",
            "season6_sup_current_bid_estimated_cap_aware_usd": "N/A",
            "season6_sup_current_bid_projected_total_without_win_sup": "0",
            "season6_sup_current_bid_projected_total_with_win_sup": "0",
            "season6_sup_current_bid_cap_remaining_before_win_sup": "12500",
            "season6_sup_projection_model": "time_weighted_xp_with_expected_future_daily_auctions",
            "season6_sup_future_dilution_enabled": "true",
            "season6_sup_expected_future_settlement_interval_seconds": "86400",
            "season6_sup_estimate_status": "no_current_bid",
            "season6_sup_total_allocated": "251340",
            "season6_sup_cap_per_wallet": "12500",
            "season6_sup_xp_per_settled_win": "100",
            "season6_current_bidder_projected_raw_sup_if_wins": "0",
            "season6_current_bidder_projected_capped_sup_if_wins": "0",
            "season6_current_bidder_projected_raw_usd_if_wins": "N/A",
            "season6_current_bidder_projected_capped_usd_if_wins": "N/A",
        }
        index = (
            '<section class="reward-strip">'
            '<span class="reward-tile season6-sup-estimate"><b>Season 6 SUP estimate</b>'
            '<strong>Bid to estimate S6 SUP</strong><em>Current high bidder needed</em></span>'
            '<span><b>Bid payback</b></span></section>'
        )
        validator = load_module()
        validator.ROOT = root
        validator.validate_season6_metrics(metrics, index)


def test_validator_catches_refresh_status_block_mismatch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        status = json.loads((root / "generated" / "refresh_status.json").read_text(encoding="utf-8"))
        status["latest_generated_block"] = 1
        write_json(root / "generated" / "refresh_status.json", status)
        write_json(root / "public" / "generated" / "refresh_status.json", status)
        assert_raises_contains(lambda: run_validation(root), "refresh_status latest_generated_block")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"validator_tests=pass count={len(tests)}")
