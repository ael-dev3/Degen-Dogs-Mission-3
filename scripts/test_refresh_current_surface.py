#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "refresh_current_surface.py"


def load_module():
    spec = importlib.util.spec_from_file_location("refresh_current_surface", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bid(
    token_id: int,
    block: int,
    tx_hash: str,
    *,
    log_index: int = 0,
    wallet: str = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    amount: str = "0.01",
    timestamp: str = "2026-08-02 12:00:00",
) -> dict:
    return {
        "bid_time_utc": timestamp,
        "token_id": token_id,
        "bidder_wallet": wallet,
        "bidder": wallet,
        "bid_eth": amount,
        "block_number": block,
        "log_index": log_index,
        "tx_hash": tx_hash,
    }


def test_overlap_history_drops_reorg_orphan_before_merge() -> None:
    surface = load_module()
    old = [
        bid(10, 90, "0xkeep"),
        bid(10, 100, "0xorphan", log_index=2),
    ]
    fresh = [bid(10, 101, "0xcanonical", log_index=3)]

    merged = surface.merge_overlap_bid_history(
        old,
        fresh,
        from_block=100,
        token_ids={10},
    )[10]

    assert [row["tx_hash"] for row in merged] == ["0xkeep", "0xcanonical"]


def test_read_table_preserves_header_for_zero_row_bid_history() -> None:
    surface = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        generated = Path(tmp)
        (generated / "empty.csv").write_text("tx_hash,log_index\n", encoding="utf-8")
        surface.GENERATED = generated

        columns, rows = surface.read_table("empty")

    assert columns == ["tx_hash", "log_index"]
    assert rows == []


def test_overlap_history_keeps_new_zero_bid_token_empty() -> None:
    surface = load_module()
    merged = surface.merge_overlap_bid_history(
        [bid(10, 90, "0xold")],
        [],
        from_block=80,
        token_ids={10, 11},
    )

    assert merged[10] == []
    assert merged[11] == []


def test_recent_bid_reconciliation_reports_canonical_delta_without_trimming_removals() -> None:
    surface = load_module()
    existing = [
        bid(10, 90, "0xold"),
        bid(10, 100, "0xorphan"),
    ]
    fresh = [bid(10, 101, "0xcanonical")]
    active = [bid(10, 90, "0xold"), bid(10, 101, "0xcanonical")]

    recent, added, removed, all_rows = surface.reconcile_recent_bid_rows(
        existing,
        fresh,
        active,
        from_block=100,
        limit=100,
    )

    assert [row["tx_hash"] for row in recent] == ["0xcanonical", "0xold"]
    assert [row["tx_hash"] for row in added] == ["0xcanonical"]
    assert [row["tx_hash"] for row in removed] == ["0xorphan"]
    assert [row["tx_hash"] for row in all_rows] == ["0xcanonical", "0xold"]


def test_recent_bid_reconciliation_fails_when_top_100_does_not_cover_overlap() -> None:
    surface = load_module()
    existing = [bid(10, block, f"0x{block:x}") for block in range(200, 300)]

    try:
        surface.reconcile_recent_bid_rows(existing, [], [], from_block=100, limit=100)
    except surface.FullRefreshRequired as exc:
        assert "does not cover overlap" in str(exc)
    else:
        raise AssertionError("expected a full-refresh signal")


def test_untracked_timeline_gap_fails_closed_unless_active_history_recovers_it() -> None:
    surface = load_module()
    recent = [bid(10, 100, "0xrecent", timestamp="2026-08-01 12:00:00")]
    timeline = [{"token_id": 11, "bids": 1, "latest_bid_utc": "2026-08-02 12:00:00"}]

    try:
        surface.ensure_no_untracked_bid_gap(timeline, recent, [])
    except surface.FullRefreshRequired as exc:
        assert "tokens=11" in str(exc)
    else:
        raise AssertionError("expected a full-refresh signal")

    surface.ensure_no_untracked_bid_gap(
        timeline,
        recent,
        [bid(11, 101, "0xrecovered", timestamp="2026-08-02 12:00:00")],
    )


def test_timeline_bid_count_without_latest_event_fails_closed() -> None:
    surface = load_module()
    recent = [bid(10, 100, "0xrecent", timestamp="2026-08-01 12:00:00")]
    timeline = [{"token_id": 11, "bids": 2, "latest_bid_utc": ""}]

    try:
        surface.ensure_no_untracked_bid_gap(timeline, recent, [])
    except surface.FullRefreshRequired as exc:
        assert "tokens=11" in str(exc)
    else:
        raise AssertionError("expected a full-refresh signal")


def test_leaderboard_delta_updates_totals_distinct_auctions_and_latest_bid() -> None:
    surface = load_module()
    wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    leaderboard = [{
        "bidder": "old",
        "bidder_url": "old-url",
        "bidder_wallet": wallet,
        "bids": 2,
        "auctions_bid": 2,
        "bid_eth": "0.02",
        "high_bid_eth": "0.015",
        "auction_wins": 0,
        "winning_eth": "0",
        "latest_bid_token_id": 9,
        "latest_bid_utc": "2026-08-01 00:00:00",
    }]
    added = [bid(10, 110, "0xnew", wallet=wallet, amount="0.01")]

    output = surface.apply_bidder_leaderboard_delta(
        leaderboard,
        added,
        [],
        added,
        {wallet: ("@alice", "https://farcaster.xyz/alice")},
    )

    assert output[0]["bids"] == 3
    assert output[0]["auctions_bid"] == 3
    assert output[0]["bid_eth"] == "0.03"
    assert output[0]["high_bid_eth"] == "0.015"
    assert output[0]["latest_bid_token_id"] == 10
    assert output[0]["bidder"] == "@alice"


def test_leaderboard_delta_does_not_double_count_existing_auction_membership() -> None:
    surface = load_module()
    wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    leaderboard = [{
        "bidder_wallet": wallet,
        "bids": 2,
        "auctions_bid": 2,
        "bid_eth": "0.02",
        "high_bid_eth": "0.01",
    }]
    prior = bid(10, 100, "0xprior", wallet=wallet)
    added = bid(10, 110, "0xnew", wallet=wallet, amount="0.02")

    output = surface.apply_bidder_leaderboard_delta(
        leaderboard,
        [added],
        [],
        [prior, added],
        {},
    )

    assert output[0]["bids"] == 3
    assert output[0]["auctions_bid"] == 2
    assert output[0]["high_bid_eth"] == "0.02"


def test_leaderboard_removal_fails_closed_at_unpersisted_rank_boundary() -> None:
    surface = load_module()
    wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    leaderboard = [{
        "bidder_wallet": wallet,
        "bids": 2,
        "auctions_bid": 1,
        "bid_eth": "0.02",
        "high_bid_eth": "0.01",
    }]

    try:
        surface.apply_bidder_leaderboard_delta(
            leaderboard,
            [],
            [bid(10, 100, "0xremoved", wallet=wallet)],
            [],
            {},
        )
    except surface.FullRefreshRequired as exc:
        assert "rank may change" in str(exc)
    else:
        raise AssertionError("expected a full-refresh signal")


def test_daily_activity_recomputes_exact_affected_day_from_known_events() -> None:
    surface = load_module()
    known = [
        bid(9, 90, "0xolder", timestamp="2026-08-01 23:59:00"),
        bid(10, 100, "0xa", wallet="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", amount="0.01", timestamp="2026-08-02 01:00:00"),
        bid(10, 101, "0xb", wallet="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", amount="0.02", timestamp="2026-08-02 02:00:00"),
    ]
    timeline = [
        {"token_id": 10, "start_time_utc": "2026-08-02 00:30:00", "settled_time_utc": "2026-08-02 03:00:00", "settled_eth": "0.02"},
    ]

    output = surface.recompute_daily_activity([], timeline, known, {"2026-08-02"})

    assert output == [{
        "activity_day": "2026-08-02",
        "created_auctions": 1,
        "settled_auctions": 1,
        "bids": 2,
        "unique_bidders": 2,
        "bid_eth": "0.03",
        "high_bid_eth": "0.02",
        "settled_eth": "0.02",
    }]


def test_winner_stats_recompute_from_complete_winner_table() -> None:
    surface = load_module()
    wallet = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    leaderboard = [{"bidder_wallet": wallet, "auction_wins": 99, "winning_eth": "99"}]
    winners = [
        {"winner_wallet": wallet, "winning_bid_eth": "0.01"},
        {"winner_wallet": wallet, "winning_bid_eth": "0.02"},
    ]

    output = surface.apply_winner_stats_to_leaderboard(leaderboard, winners)

    assert output[0]["auction_wins"] == 2
    assert output[0]["winning_eth"] == "0.03"


def test_rarity_replaces_current_token_instead_of_scoring_phantom_supply() -> None:
    surface = load_module()
    history = [
        {"token_id": 0, "traits": "Body: Common"},
        {"token_id": 1, "traits": "Body: Rare"},
        {"token_id": 2, "traits": "Body: Common"},
    ]

    rarity, score, _traits, trait_rarity = surface.build_rarity(
        history,
        2,
        {"Body": "Common"},
        3,
    )

    assert rarity == "#3/3"
    assert score == 1.5
    assert trait_rarity == "Body: Common (66.7%)"


def test_rarity_universe_rebases_every_rank_and_denominator_after_mint() -> None:
    surface = load_module()
    history = [
        {"token_id": 0, "traits": "Body: Common"},
        {"token_id": 1, "traits": "Body: Rare"},
        {"token_id": 2, "traits": "Body: Common"},
    ]

    universe = surface.build_rarity_universe(history, 3, {"Body": "Unique"}, 4)

    assert sorted(int(row["rarity"].split("/")[0].lstrip("#")) for row in universe.values()) == [1, 2, 3, 4]
    assert {row["rarity"].split("/")[1] for row in universe.values()} == {"4"}
    assert universe[3]["trait_rarity"] == "Body: Unique (25.0%)"
    assert universe[0]["trait_rarity"] == "Body: Common (50.0%)"


def test_prior_settled_winner_block_and_historical_price_survive_fast_refresh_merge() -> None:
    surface = load_module()
    tx_hash = "0x" + "a" * 64
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        generated = root / "generated"
        generated.mkdir(parents=True)
        winner = {
            "token_id": 9,
            "block_number": "12345",
            "tx_hash": tx_hash,
            "winning_bid_usd_at_settlement": "20.00",
            "eth_usd_price_at_event": "2000",
            "eth_usd_price_date_utc": "2026-08-01",
            "usd_estimate_source": "defillama_coin_prices",
            "usd_estimate_source_detail": "coins.llama.fi/chart/coingecko:ethereum",
            "usd_estimate_confidence": "medium",
        }
        (generated / "auction_winners.json").write_text(json.dumps([winner]), encoding="utf-8")
        (generated / "auction_feed.json").write_text("[]", encoding="utf-8")
        surface.ROOT = root
        surface.GENERATED = generated

        settlement = surface.preserve_settlement_fields(
            9,
            {
                "amount_eth": "0.01",
                "block_number": "",
                "block_time_utc": "2026-08-01 12:00:00",
                "tx_hash": tx_hash,
            },
        )
        assert settlement is not None
        historical = surface.canonical_historical_usd(9, surface.Decimal("0.01"), settlement["block_time_utc"])
        merged = surface.merge_settled_winner_row(
            winner,
            settlement,
            historical,
            {"token_id": 9, "winning_bid_eth": "0.01"},
        )

    assert settlement["block_number"] == "12345"
    assert merged["block_number"] == "12345"
    assert merged["tx_hash"] == tx_hash
    assert merged["winning_bid_usd_at_settlement"] == "20.00"
    assert merged["eth_usd_price_at_event"] == "2000"
    assert merged["usd_estimate_source"] == "defillama_coin_prices"


def test_prior_settled_winner_bid_times_survive_an_empty_rotated_ledger() -> None:
    surface = load_module()
    existing = {
        "first_bid_utc": "2026-08-01 13:14:59",
        "last_bid_utc": "2026-08-01 13:14:59",
    }
    settlement = {
        "block_number": 49443635,
        "tx_hash": "0x" + "a" * 64,
    }
    historical = {
        "amount_usd_at_event": "20.00",
        "eth_usd_price_at_event": "2000",
        "eth_usd_price_date_utc": "2026-08-01",
        "usd_estimate_source": "defillama_coin_prices",
    }

    merged = surface.merge_settled_winner_row(
        existing,
        settlement,
        historical,
        {"first_bid_utc": "", "last_bid_utc": ""},
    )

    assert merged["first_bid_utc"] == existing["first_bid_utc"]
    assert merged["last_bid_utc"] == existing["last_bid_utc"]


def test_settled_price_merge_ignores_live_candidate_and_uses_canonical_archive() -> None:
    surface = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        generated = root / "generated"
        dog_dir = root / "archive" / "dogs" / "by-id"
        generated.mkdir(parents=True)
        dog_dir.mkdir(parents=True)
        live = {
            "dog": "Dog #7",
            "amount_usd_at_event": "21.00",
            "eth_usd_price_at_event": "2100",
            "eth_usd_price_date_utc": "2026-08-02",
            "usd_estimate_source": "current_eth_usd_price",
        }
        canonical = {
            "record": {
                "dog_id": 7,
                "amount": {
                    "amount_usd_at_event": "20.12345678",
                    "eth_usd_price_at_event": "2012.345678",
                    "eth_usd_price_date_utc": "2026-08-01",
                    "usd_estimate": "20.12345678",
                    "usd_estimate_source": "defillama_coin_prices",
                    "usd_estimate_source_detail": "coins.llama.fi/chart/coingecko:ethereum",
                    "usd_estimate_confidence": "medium",
                },
            }
        }
        (generated / "auction_feed.json").write_text(json.dumps([live]), encoding="utf-8")
        (generated / "auction_winners.json").write_text("[]", encoding="utf-8")
        (dog_dir / "007.json").write_text(json.dumps(canonical), encoding="utf-8")
        surface.ROOT = root
        surface.GENERATED = generated

        historical = surface.canonical_historical_usd(7, surface.Decimal("0.01"), "2026-08-01T12:00:00Z")

    assert historical["amount_usd_at_event"] == "20.12345678"
    assert historical["usd_estimate_source"] == "defillama_coin_prices"


def test_settled_search_amount_uses_event_time_usd_not_live_eth_price() -> None:
    surface = load_module()
    historical = {
        "amount_usd_at_event": "20.12345678",
        "eth_usd_price_at_event": "2012.345678",
        "eth_usd_price_date_utc": "2026-08-01",
        "usd_estimate_source": "defillama_coin_prices",
    }

    display = surface.historical_settlement_amount_display(
        7,
        surface.Decimal("0.01"),
        historical,
    )

    assert display == "0.01 ETH ($20)"
    assert "previous_amount_eth * eth_usd" not in MODULE_PATH.read_text(encoding="utf-8")
    try:
        surface.historical_settlement_amount_display(7, surface.Decimal("0.01"), None)
    except surface.FullRefreshRequired as exc:
        assert "historical USD provenance" in str(exc)
    else:
        raise AssertionError("settled search amount accepted missing historical USD provenance")

    assert surface.decimal_or_none("NaN") is None
    assert surface.decimal_or_none("Infinity") is None


def test_canonical_historical_usd_rejects_stale_amount_and_event_date() -> None:
    surface = load_module()
    original_candidates = surface.archive_candidates
    original_local = surface.local_historical_usd
    fallback = {
        "amount_usd_at_event": "40.00000000",
        "eth_usd_price_at_event": "2000",
        "eth_usd_price_date_utc": "2026-08-01",
        "usd_estimate_source": "defillama_coin_prices",
    }
    base_candidate = {
        "amount_usd_at_event": "20.00",
        "eth_usd_price_at_event": "2000",
        "eth_usd_price_date_utc": "2026-08-01",
        "usd_estimate_source": "defillama_coin_prices",
    }
    try:
        surface.local_historical_usd = lambda *_args, **_kwargs: fallback
        surface.archive_candidates = lambda _token_id: [base_candidate]
        assert surface.canonical_historical_usd(
            7,
            surface.Decimal("0.02"),
            "2026-08-01T12:00:00Z",
        ) == fallback

        wrong_date = dict(fallback)
        wrong_date["eth_usd_price_date_utc"] = "2026-07-01"
        surface.archive_candidates = lambda _token_id: [wrong_date]
        assert surface.canonical_historical_usd(
            7,
            surface.Decimal("0.02"),
            "2026-08-01T12:00:00Z",
        ) == fallback
    finally:
        surface.archive_candidates = original_candidates
        surface.local_historical_usd = original_local


def test_current_creation_recovers_verified_block_from_preceding_settlement() -> None:
    surface = load_module()
    tx_hash = "0x" + "a" * 64

    created = surface.resolve_current_auction_creation(
        790,
        "2026-08-01T13:29:43Z",
        {},
        {
            "created_tx_hash": tx_hash,
            "start_time_utc": "2026-08-01 13:29:43",
        },
        {"auction_created": {"block_number": None, "tx_hash": None}},
        {},
        [{"block_number": "49400365", "tx_hash": tx_hash}],
    )

    assert created == {
        "block_number": 49400365,
        "block_time_utc": "2026-08-01T13:29:43Z",
        "tx_hash": tx_hash,
        "tx_url": f"https://basescan.org/tx/{tx_hash}",
    }


def test_current_creation_fails_closed_without_verified_block_mapping() -> None:
    surface = load_module()
    tx_hash = "0x" + "b" * 64

    try:
        surface.resolve_current_auction_creation(
            790,
            "2026-08-01T13:29:43Z",
            {},
            {"created_tx_hash": tx_hash, "start_time_utc": "2026-08-01T13:29:43Z"},
            {"auction_created": {"block_number": 999, "tx_hash": tx_hash}},
            {},
            [],
        )
    except surface.FullRefreshRequired as exc:
        assert "no verified onchain block mapping" in str(exc)
    else:
        raise AssertionError("expected a full-refresh signal")


def test_current_creation_rejects_transaction_or_block_conflicts() -> None:
    surface = load_module()
    timeline_tx = "0x" + "c" * 64
    fresh_tx = "0x" + "d" * 64

    try:
        surface.resolve_current_auction_creation(
            790,
            "2026-08-01T13:29:43Z",
            {"block_number": 100, "block_time_utc": "2026-08-01T13:29:43Z", "tx_hash": fresh_tx},
            {"created_tx_hash": timeline_tx, "start_time_utc": "2026-08-01T13:29:43Z"},
            {},
            {},
            [],
        )
    except surface.FullRefreshRequired as exc:
        assert "transaction sources disagree" in str(exc)
    else:
        raise AssertionError("expected a transaction-conflict full-refresh signal")

    try:
        surface.resolve_current_auction_creation(
            790,
            "2026-08-01T13:29:43Z",
            {},
            {"created_tx_hash": timeline_tx, "start_time_utc": "2026-08-01T13:29:43Z"},
            {"auction_created": {"block_number": 101, "tx_hash": timeline_tx}},
            {},
            [{"block_number": 100, "tx_hash": timeline_tx}],
        )
    except surface.FullRefreshRequired as exc:
        assert "block sources disagree" in str(exc)
    else:
        raise AssertionError("expected a block-conflict full-refresh signal")


def test_unified_trait_items_parses_every_display_trait() -> None:
    surface = load_module()
    traits = surface.unified_trait_items("Body: SugarSkull (6.8%); Eyes: EyePatch (8.0%)")

    assert traits == [
        {"display": "Body: SugarSkull (6.8%)", "trait_type": "Body", "value": "SugarSkull"},
        {"display": "Eyes: EyePatch (8.0%)", "trait_type": "Eyes", "value": "EyePatch"},
    ]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"refresh_current_surface_tests=pass count={len(tests)}")
