#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_dashboard.py"


def load_module() -> Any:
    os.environ["MISSION3_LOG_CACHE"] = "1"
    os.environ["MISSION3_BALANCE_CACHE"] = "1"
    spec = importlib.util.spec_from_file_location("build_dashboard", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def log(block: int, tx: str, index: int) -> dict[str, Any]:
    return {"blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(index), "data": "0x", "topics": []}


def test_fetch_logs_extends_cached_ranges_with_overlap_and_dedupes() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        setattr(dashboard, "LOG_CACHE_OVERLAP_BLOCKS", 5)
        calls: list[tuple[int, int]] = []

        def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
            calls.append((start, end))
            if len(calls) == 1:
                return [log(100, "0xaaa", 0), log(150, "0xbbb", 2)]
            return [log(150, "0xbbb", 2), log(160, "0xccc", 1)]

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        first = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 150)
        assert [item["transactionHash"] for item in first] == ["0xaaa", "0xbbb"]
        assert calls == [(100, 150)]

        second = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 175)
        assert calls == [(100, 150), (146, 175)]
        assert [item["transactionHash"] for item in second] == ["0xaaa", "0xbbb", "0xccc"]

        third = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 175)
        assert calls == [(100, 150), (146, 175)]
        assert [item["transactionHash"] for item in third] == ["0xaaa", "0xbbb", "0xccc"]


def test_fetch_logs_caches_empty_ranges() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        calls: list[tuple[int, int]] = []

        def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
            calls.append((start, end))
            return []

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        assert dashboard.fetch_logs("0xabc", [dashboard.TOPIC_AUCTION_CREATED], 200, 250) == []
        assert dashboard.fetch_logs("0xabc", [dashboard.TOPIC_AUCTION_CREATED], 200, 250) == []
        assert calls == [(200, 250)]


def address_topic(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def transfer_log(dashboard: Any, block: int, from_address: str, to_address: str) -> dict[str, Any]:
    return {
        "blockNumber": hex(block),
        "transactionHash": f"0x{block:064x}",
        "logIndex": "0x0",
        "topics": [dashboard.TOPIC_TRANSFER, address_topic(from_address), address_topic(to_address)],
        "data": "0x",
    }


def test_fetch_woof_holders_reuses_cached_balances_until_address_is_touched() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    carol = "0x00000000000000000000000000000000000000c3"
    balances = {alice: 100, bob: 200, carol: 300}

    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "WOOF_BALANCE_CACHE", Path(tmp) / "woof_balances.json")
        calls: list[list[str]] = []

        def fake_fetch(addresses: list[str], _block_tag: str) -> dict[str, int]:
            calls.append(addresses)
            return {address: balances[address] for address in addresses}

        setattr(dashboard, "fetch_balances", fake_fetch)
        first_logs = [transfer_log(dashboard, 100, alice, bob)]
        first = dashboard.fetch_woof_holders(first_logs, 0, "0x64")
        assert calls == [[alice, bob]]
        assert [(row["address"], row["balance_raw"]) for row in first] == [(bob, "200"), (alice, "100")]

        second = dashboard.fetch_woof_holders(first_logs, 0, "0x65")
        assert calls == [[alice, bob], []]
        assert [(row["address"], row["balance_raw"]) for row in second] == [(bob, "200"), (alice, "100")]

        balances[bob] = 250
        third_logs = [*first_logs, transfer_log(dashboard, 102, bob, carol)]
        third = dashboard.fetch_woof_holders(third_logs, 0, "0x66")
        assert calls == [[alice, bob], [], [bob, carol]]
        assert [(row["address"], row["balance_raw"]) for row in third] == [(carol, "300"), (bob, "250"), (alice, "100")]


def test_current_bid_reward_stats_calculates_payback_daily_roi_and_simple_apr() -> None:
    dashboard = load_module()
    stats = dashboard.current_bid_reward_stats(
        {"amount_wei": "10000000000000000"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0.113508"},
    )
    assert stats["reward_current_bid_payback_days"] == "176.02"
    assert stats["reward_current_bid_daily_roi_pct"] == "0.5681"
    assert stats["reward_current_bid_apr_pct"] == "207.36"
    assert stats["reward_current_bid_apr_display"] == "≈207% APR"


def test_current_bid_reward_stats_unavailable_when_bid_or_daily_flow_missing() -> None:
    dashboard = load_module()
    zero_bid = dashboard.current_bid_reward_stats(
        {"amount_wei": "0"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0.113508"},
    )
    assert zero_bid["reward_current_bid_payback_days"] == "N/A"
    assert zero_bid["reward_current_bid_apr_pct"] == "N/A"
    assert zero_bid["reward_current_bid_apr_display"] == "N/A"

    zero_flow = dashboard.current_bid_reward_stats(
        {"amount_wei": "10000000000000000"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0"},
    )
    assert zero_flow["reward_current_bid_payback_days"] == "N/A"
    assert zero_flow["reward_current_bid_apr_pct"] == "N/A"
    assert zero_flow["reward_current_bid_apr_display"] == "N/A"


def test_timer_urgency_stays_calm_until_less_than_one_hour_remains() -> None:
    dashboard = load_module()
    assert dashboard.timer_urgency_state(3601, "live") == "calm"
    assert dashboard.timer_urgency_state(3600, "live") == "calm"
    assert dashboard.timer_urgency_state(3599, "live") == "urgent"
    assert dashboard.timer_urgency_state(600, "live") == "critical"
    assert dashboard.timer_urgency_state(0, "live") == "ended"


def write_reward_snapshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "snapshot_utc": "2026-06-02T20:55:15Z",
        "reward_account_dogs_count": "133",
        "account_woof_flow_per_day": "20494201.30",
        "account_sup_flow_per_day": "199.58",
        "account_woof_received": "2856495886.75",
        "account_sup_received": "38733.66",
        "derived_woof_per_dog_per_day": "154091.739097744361",
        "derived_sup_per_dog_per_day": "1.5006015037593985",
        "basis_source": "observed_stream_snapshot_133_dogs",
        "note": "Observed reward account stream snapshot; update when the live stream changes.",
    }, indent=2) + "\n", encoding="utf-8")


def test_load_reward_stream_snapshot_derives_observed_per_dog_values() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "config" / "reward_stream_snapshot.json"
        write_reward_snapshot(snapshot_path)
        snapshot = dashboard.load_reward_stream_snapshot(snapshot_path)
        assert snapshot.dogs_count == Decimal("133")
        assert snapshot.woof_flow_per_day == Decimal("20494201.30")
        assert snapshot.sup_flow_per_day == Decimal("199.58")
        assert dashboard.decimal_value_str(snapshot.woof_per_dog_per_day, 12) == "154091.739097744361"
        assert dashboard.decimal_value_str(snapshot.sup_per_dog_per_day, 16) == "1.5006015037593985"


def test_reward_token_stats_uses_observed_snapshot_for_per_dog_flows_and_usd_totals() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "config" / "reward_stream_snapshot.json"
        write_reward_snapshot(snapshot_path)
        snapshot = dashboard.load_reward_stream_snapshot(snapshot_path)
    stats = dashboard.reward_token_stats(Decimal("0.0000005"), Decimal("0.02"), snapshot=snapshot)
    assert stats["reward_basis_dogs"] == "133"
    assert stats["reward_basis_source"] == "observed_stream_snapshot_133_dogs"
    assert stats["reward_observed_dogs_count"] == "133"
    assert stats["reward_observed_woof_flow_per_day"] == "20494201.3"
    assert stats["reward_observed_sup_flow_per_day"] == "199.58"
    assert stats["reward_observed_woof_received"] == "2856495886.75"
    assert stats["reward_observed_sup_received"] == "38733.66"
    assert stats["reward_observed_woof_per_dog_per_day"] == "154091.739097744361"
    assert stats["reward_observed_sup_per_dog_per_day"] == "1.5006015037593985"
    assert stats["reward_woof_per_dog_per_day"] == "154091.739097744361"
    assert stats["reward_sup_per_dog_per_day"] == "1.5006015037593985"
    assert stats["reward_total_per_dog_usd_per_day"] == "0.107058"


def test_reward_strip_renders_apr_inside_bid_payback_card_with_caveat_copy() -> None:
    dashboard = load_module()
    metrics = {
        "reward_basis_dogs": "133",
        "reward_basis_source": "observed_stream_snapshot_133_dogs",
        "reward_woof_per_dog_per_day": "154091.739097744361",
        "reward_woof_per_dog_usd_per_day": "0.077046",
        "reward_sup_per_dog_per_day": "1.5006015037593985",
        "reward_sup_per_dog_usd_per_day": "0.030012",
        "reward_total_per_dog_usd_per_day": "0.107058",
        "reward_current_bid_payback_days": "186.63",
        "reward_current_bid_apr_pct": "195.58",
        "reward_current_bid_apr_display": "≈196% APR",
    }
    rendered = dashboard.render_reward_strip(metrics)
    assert "<b>Bid payback</b>" in rendered
    assert "Observed 133-Dog stream" in rendered
    assert "≈154,092 WOOF + ≈1.50 SUP / Dog / day" in rendered
    assert "≈187 days" in rendered
    assert "≈196% APR" in rendered
    assert "Current bid / observed per-Dog flow" in rendered
    assert "Simple APR estimate" in rendered
    assert "not guaranteed" in rendered.lower()
    assert "guaranteed return" not in rendered.lower()


def season6_test_config(
    dashboard: Any,
    *,
    total: str = "1000",
    cap: str = "600",
    campaign_seconds: int = 100,
    expected_future_settlement_interval_seconds: int = 0,
    projection_model: str = "time_weighted_xp_unit_test",
) -> Any:
    campaign_end = f"2026-06-02T00:{campaign_seconds // 60:02d}:{campaign_seconds % 60:02d}Z"
    return dashboard.Season6SupConfig(
        enabled=True,
        sup_token=dashboard.SUP.lower(),
        season_start_utc="2026-06-02T00:00:00Z",
        season_end_utc=campaign_end,
        total_sup=Decimal(total),
        cap_sup=Decimal(cap),
        xp_per_settled_win=Decimal("100"),
        reward_start_delay_days=0,
        cap_level="wallet_estimate",
        projection_model=projection_model,
        expected_future_settlement_interval_seconds=expected_future_settlement_interval_seconds,
        visible_dashboard_mode="compact_final_estimate_only",
        cap_percent_label="5% cap",
    )


def test_season6_time_sliced_rewards_split_after_later_xp_event() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:50Z"},
        ],
        {"token_id": 3, "bidder": bob, "amount_eth": 0.03, "end_time_utc": "2026-06-02T00:01:40Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard),
    )
    by_winner = {row["winner_wallet"]: row for row in outputs["season6_sup_by_winner"]}
    assert by_winner[alice]["season6_wins_confirmed"] == 1
    assert by_winner[alice]["season6_xp_confirmed"] == 100
    assert by_winner[alice]["season6_raw_sup_projected_full"] == "750"
    assert by_winner[bob]["season6_raw_sup_projected_full"] == "250"
    assert by_winner[alice]["season6_capped_sup_projected_full"] == "600"
    assert by_winner[alice]["season6_cap_limited"] == "true"
    assert outputs["season6_metrics"]["season6_sup_unallocated_due_to_zero_xp"] == "0"


def test_season6_cap_uses_explicit_12500_sup_not_percent_math() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": wallet, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {},
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard, total="251340", cap="12500"),
    )
    row = outputs["season6_sup_by_winner"][0]
    assert row["season6_raw_sup_projected_full"] == "251340"
    assert row["season6_cap_sup"] == "12500"
    assert row["season6_capped_sup_projected_full"] == "12500"
    assert row["season6_cap_limited"] == "true"


def test_season6_price_missing_keeps_raw_sup_and_na_usd() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": wallet, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {},
        {"sup_usd_price": "0", "sup_usd_source": "unavailable"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard),
    )
    row = outputs["season6_sup_by_winner"][0]
    assert row["season6_raw_sup_projected_full"] == "1000"
    assert row["season6_raw_usd_projected_full"] == "N/A"
    assert row["season6_capped_usd_projected_full"] == "N/A"


def test_season6_current_bidder_projection_adds_hypothetical_win_and_prior_status() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {"token_id": 2, "bidder": alice, "amount_eth": 0.02, "end_time_utc": "2026-06-02T00:00:50Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:00:50Z",
        config=season6_test_config(dashboard),
    )
    status = outputs["season6_sup_current_bidder_status"][0]
    metrics = outputs["season6_metrics"]
    assert status["current_bidder_wallet"] == alice
    assert status["prior_s6_wins_confirmed"] == 1
    assert status["prior_s6_xp_confirmed"] == 100
    assert status["projected_s6_wins_if_current_bid_wins"] == 2
    assert status["projected_s6_xp_if_current_bid_wins"] == 200
    assert status["projected_capped_sup_if_current_bid_wins"] == "600"
    assert status["current_bidder_cap_status"] == "wallet_near_cap"
    assert metrics["season6_sup_current_bidder_prior_s6_wins"] == "1"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_sup"] == "0"
    assert metrics["season6_sup_current_bid_estimate_status"] == "wallet_near_cap"


def test_season6_three_equal_winners_split_one_third_after_third_win() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    carol = "0x00000000000000000000000000000000000000c3"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:30Z"},
            {"token_id": 3, "winner": carol, "amount_eth": 0.03, "block_time_utc": "2026-06-02T00:01:00Z"},
        ],
        {},
        {"sup_usd_price": "1", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:30Z",
        config=season6_test_config(dashboard, total="90", cap="1000", campaign_seconds=90),
    )
    by_winner = {row["winner_wallet"]: row for row in outputs["season6_sup_by_winner"]}
    assert by_winner[alice]["season6_raw_sup_projected_full"] == "55"
    assert by_winner[bob]["season6_raw_sup_projected_full"] == "25"
    assert by_winner[carol]["season6_raw_sup_projected_full"] == "10"


def test_season6_current_bid_estimate_is_incremental_cap_aware_and_counts_prior_wins() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:50Z"},
        ],
        {"token_id": 3, "bidder": alice, "amount_eth": 0.03, "end_time_utc": "2026-06-02T00:01:15Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:15Z",
        config=season6_test_config(dashboard, total="1000", cap="760", campaign_seconds=100),
    )
    metrics = outputs["season6_metrics"]
    assert metrics["season6_sup_current_bidder_prior_s6_wins"] == "1"
    assert metrics["season6_sup_current_bidder_prior_s6_xp"] == "100"
    assert metrics["season6_sup_current_bid_projected_total_without_win_sup"] == "750"
    assert metrics["season6_sup_current_bid_projected_total_with_win_sup"] == "791.666667"
    assert metrics["season6_sup_current_bid_estimated_raw_incremental_sup"] == "41.666667"
    assert metrics["season6_sup_current_bid_cap_remaining_before_win_sup"] == "10"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_sup"] == "10"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_usd"] == "20"


def test_season6_future_daily_dilution_reduces_current_bid_estimate() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    current = {"token_id": 1, "bidder": alice, "amount_eth": 0.01, "end_time_utc": "2026-06-02T00:00:00Z"}
    no_future = dashboard.build_season6_sup_outputs(
        [],
        current,
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:00:00Z",
        config=season6_test_config(dashboard, total="1000", cap="2000", campaign_seconds=100, expected_future_settlement_interval_seconds=0),
    )["season6_metrics"]
    with_future = dashboard.build_season6_sup_outputs(
        [],
        current,
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:00:00Z",
        config=season6_test_config(dashboard, total="1000", cap="2000", campaign_seconds=100, expected_future_settlement_interval_seconds=50),
    )["season6_metrics"]
    assert no_future["season6_sup_current_bid_estimated_cap_aware_sup"] == "1000"
    assert with_future["season6_sup_future_dilution_enabled"] == "true"
    assert with_future["season6_sup_current_bid_estimated_cap_aware_sup"] == "750"
    assert Decimal(with_future["season6_sup_current_bid_estimated_cap_aware_sup"]) < Decimal(no_future["season6_sup_current_bid_estimated_cap_aware_sup"])


def test_season6_compact_card_uses_final_cap_aware_estimate_only() -> None:
    dashboard = load_module()
    rendered = dashboard.render_season6_strip({
        "season6_sup_enabled": "true",
        "season6_sup_estimate_status": "estimated",
        "season6_sup_current_bid_estimated_cap_aware_sup": "11240.25",
        "season6_sup_current_bid_estimated_cap_aware_usd": "118.02",
    })
    assert "Season 6 SUP estimate" in rendered
    assert "≈11,240 SUP" in rendered
    assert "≈$118 if current bid wins" in rendered
    assert "Adjusted for prior S6 wins; estimate only." in rendered
    forbidden = ["Pool:", "Cap:", "100 XP per settled Dog win", "Projected if current bid wins", "Cap-limited estimate"]
    assert not any(text in rendered for text in forbidden)


def test_season6_compact_card_neutral_without_current_high_bidder() -> None:
    dashboard = load_module()
    rendered = dashboard.render_season6_strip({
        "season6_sup_enabled": "true",
        "season6_sup_estimate_status": "no_current_bid",
    })
    assert "Bid to estimate S6 SUP" in rendered
    assert "Pool:" not in rendered
    assert "Cap:" not in rendered


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"build_dashboard_tests=pass count={len(tests)}")
