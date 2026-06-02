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
    apr_pct: str = "207.36",
    daily_roi_pct: str = "0.5681",
    payback_days: str = "176.02",
    index_dog: str = "Dog #729",
    index_apr: str = "≈207% APR",
    apr_display: str | None = None,
    feed_bidder: str = "@0xael.eth",
    season6_projected_raw_display: str = "Projected if current bid wins: ≈1,000 SUP / ≈$2,000 raw estimate",
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
        "reward_total_per_dog_usd_per_day": "0.113508",
        "reward_current_bid_payback_days": payback_days,
        "reward_current_bid_daily_roi_pct": daily_roi_pct,
        "reward_current_bid_apr_pct": apr_pct,
        "reward_current_bid_apr_display": apr_display_value,
        "season6_sup_status": "live_estimate",
        "season6_sup_total_allocated": "251340",
        "season6_sup_cap_per_wallet": "12500",
        "season6_sup_cap_percent_label": "5% cap",
        "season6_sup_xp_per_settled_win": "100",
        "season6_sup_xp_start_utc": "2026-06-02T00:00:00Z",
        "season6_sup_reward_start_utc": "2026-06-02T00:00:00Z",
        "season6_sup_campaign_end_utc": "2026-08-31T23:59:59Z",
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
    write_json(root / "generated" / "current_latest_bid.json", [{"latest_bid_eth": 0.01, "bidder": "@0xael.eth", "bidder_wallet": wallet, "bid_time_utc": "2026-05-30 18:40:23"}])
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
        "projected_raw_sup_if_current_bid_wins": "1000",
        "projected_capped_sup_if_current_bid_wins": "1000",
        "projected_cap_remaining_sup_if_current_bid_wins": "11500",
        "projected_raw_usd_if_current_bid_wins": "2000",
        "projected_capped_usd_if_current_bid_wins": "2000",
        "current_bidder_cap_status": "ok",
        "projection_note": "estimated wallet-level projection",
    }
    for folder in (root / "generated", root / "public" / "generated"):
        write_json(folder / "season6_sup_by_winner.json", [season6_winner])
        write_json(folder / "season6_sup_rewards_by_auction.json", [season6_auction])
        write_json(folder / "season6_sup_current_bidder_status.json", [season6_status])
    unified_row = {
        "mission": 3,
        "dog_id": 729,
        "winner_or_high_bidder": {"wallet": wallet, "display": "@0xael.eth"},
        "amount": {"native": "0.01"},
        "activity_time_utc": "2026-05-30T18:40:23Z",
        "bid_stats": {"bid_count": 0, "unique_bidder_count": 0},
        "bid_tx_hashes": [],
        "search_text": f"dog 729 {wallet} @0xael.eth 0.01 eth",
    }
    write_json(root / "archive" / "data" / "generated" / "unified_dog_search_index.json", [unified_row])
    write_json(root / "public" / "generated" / "unified_dog_search_index.json", [unified_row])
    write_json(root / "archive" / "data" / "identity" / "wallet_profiles.json", {})
    write_json(root / "archive" / "mission3" / "data" / "generated" / "mission3_auction_bids.json", [])

    index = (
        f"<html><body><h1>{index_dog}</h1><span>0.01000 ETH ($20)</span>"
        f"<span>@0xael.eth</span><span>ongoing</span><span>2026-05-31 20:40:09</span>"
        f"<section class=\"reward-strip\"><span><b>Bid payback</b><strong>≈176 days</strong>"
        f"<span>{index_apr}</span><em>Simple APR estimate. Annualized from current per-Dog daily WOOF + SUP flow; excludes WOOF Vault Bonus; does not compound; not guaranteed future return.</em></span></section>"
        f"<section class=\"season6-sup\"><h2>Season 6 SUP rewards live</h2>"
        f"<span>Pool: 251,340 SUP</span><span>Cap: 12,500 SUP per wallet-level estimate</span>"
        f"<span>100 XP per settled Dog win</span><span>{season6_projected_raw_display}</span>"
        f"<span>Cap-limited estimate: ≈1,000 SUP / ≈$2,000</span></section>"
        f"{hidden_metrics_table(metrics)}</body></html>"
    )
    write_text(root / "index.html", index)
    write_text(root / "README.md", """# Fixture\n\n## Current snapshot\n\n| Field | Value |\n| --- | --- |\n| Snapshot block | 46732183 |\n| Snapshot time UTC | 2026-05-31 18:55:13 |\n| Current Dog | Dog #729 |\n| Current status | live |\n| Current bid | 0.01 ETH ($19.98) |\n| Current high bidder | @0xael.eth |\n| Bid payback / APR | ≈176 days / ≈207% APR |\n\n## Next\n""")


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
        write_fixture(root, index_apr="≈999% APR", apr_display="≈207% APR")
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


def test_validator_catches_stale_season6_rendered_projection() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, season6_projected_raw_display="Projected if current bid wins: ≈999 SUP / ≈$1,998 raw estimate")
        assert_raises_contains(lambda: run_validation(root), "Season 6 current bidder projection")


def test_validator_catches_season6_capped_value_over_configured_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, season6_by_winner_capped="13000")
        assert_raises_contains(lambda: run_validation(root), "Season 6 capped SUP exceeds configured cap")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"validator_tests=pass count={len(tests)}")
