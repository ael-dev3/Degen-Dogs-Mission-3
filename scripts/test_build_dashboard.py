#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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


def test_reward_strip_renders_apr_inside_bid_payback_card_with_caveat_copy() -> None:
    dashboard = load_module()
    metrics = {
        "reward_woof_per_dog_per_day": "158351.896454",
        "reward_woof_per_dog_usd_per_day": "0.08",
        "reward_sup_per_dog_per_day": "2.687",
        "reward_sup_per_dog_usd_per_day": "0.03",
        "reward_total_per_dog_usd_per_day": "0.113508",
        "reward_current_bid_payback_days": "176.02",
        "reward_current_bid_apr_pct": "207.36",
        "reward_current_bid_apr_display": "≈207% APR",
    }
    rendered = dashboard.render_reward_strip(metrics)
    assert "<b>Bid payback</b>" in rendered
    assert "≈176 days" in rendered
    assert "≈207% APR" in rendered
    assert "Current bid / per-Dog WOOF + SUP flow" in rendered
    assert "Simple APR estimate" in rendered
    assert "not guaranteed" in rendered.lower()
    assert "guaranteed return" not in rendered.lower()


def season6_test_config(dashboard: Any, *, total: str = "1000", cap: str = "600") -> Any:
    return dashboard.Season6SupConfig(
        total_sup=Decimal(total),
        cap_sup=Decimal(cap),
        xp_per_settled_win=Decimal("100"),
        xp_start_utc="2026-06-02T00:00:00Z",
        reward_start_utc="2026-06-02T00:00:00Z",
        campaign_end_utc="2026-06-02T00:01:40Z",
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
    assert status["current_bidder_wallet"] == alice
    assert status["prior_s6_wins_confirmed"] == 1
    assert status["prior_s6_xp_confirmed"] == 100
    assert status["projected_s6_wins_if_current_bid_wins"] == 2
    assert status["projected_s6_xp_if_current_bid_wins"] == 200
    assert status["projected_capped_sup_if_current_bid_wins"] == "600"
    assert status["current_bidder_cap_status"] == "cap_limited_projected"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"build_dashboard_tests=pass count={len(tests)}")
