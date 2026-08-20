#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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


def rarity_attrs(body: str) -> dict[str, str]:
    return {
        "Background": "None",
        "Body": body,
        "Neck": "None",
        "Mouth": "None",
        "Ears": "None",
        "Head": "None",
        "Eyes": "None",
    }


def rarity_traits(body: str) -> str:
    return "; ".join(f"{trait_type}: {value}" for trait_type, value in rarity_attrs(body).items())


def continuity_log(
    address: str,
    topics: list[str],
    *,
    block: int = 110,
    transaction_index: int = 0,
    log_index: int = 0,
    data: str = "0x",
) -> dict:
    return {
        "address": address,
        "blockNumber": hex(block),
        "blockHash": "0x" + "a" * 63 + f"{block % 16:x}",
        "transactionHash": "0x" + "b" * 63 + f"{transaction_index % 16:x}",
        "transactionIndex": hex(transaction_index),
        "logIndex": hex(log_index),
        "removed": False,
        "data": data,
        "topics": topics,
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


def test_auction_feed_status_uses_the_canonical_pending_settlement_label() -> None:
    surface = load_module()

    assert surface.auction_feed_status("live") == "ongoing"
    assert surface.auction_feed_status("ended_unsettled") == "ended pending settlement"
    assert surface.auction_feed_status("settled") == "settled"


def test_current_bid_history_public_rows_are_high_bid_first() -> None:
    surface = load_module()
    rows = [
        bid(793, 100, "0xopening", log_index=2, amount="0.001"),
        bid(793, 120, "0xhigh", log_index=3, amount="0.009"),
    ]

    ordered = surface.high_bid_first(rows)

    assert [row["tx_hash"] for row in ordered] == ["0xhigh", "0xopening"]


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
        {"token_id": 0, "traits": rarity_traits("Common"), "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": 1, "traits": rarity_traits("Rare"), "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": 2, "traits": rarity_traits("Common"), "metadata_verification_status": "onchain_token_uri_verified"},
    ]

    rarity, score, _traits, trait_rarity = surface.build_rarity(
        history,
        2,
        rarity_attrs("Common"),
        3,
    )

    assert rarity == "#2/3"
    assert score == 7.5
    assert "Body: Common (66.7%)" in trait_rarity
    assert "Background: None (100.0%)" in trait_rarity


def test_rarity_universe_rebases_every_rank_and_denominator_after_mint() -> None:
    surface = load_module()
    history = [
        {"token_id": 0, "traits": rarity_traits("Common"), "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": 1, "traits": rarity_traits("Rare"), "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": 2, "traits": rarity_traits("Common"), "metadata_verification_status": "onchain_token_uri_verified"},
    ]

    universe = surface.build_rarity_universe(history, 3, rarity_attrs("Unique"), 4)

    assert sorted(int(row["rarity"].split("/")[0].lstrip("#")) for row in universe.values()) == [1, 1, 3, 3]
    assert {row["rarity"].split("/")[1] for row in universe.values()} == {"4"}
    assert "Body: Unique (25.0%)" in universe[3]["trait_rarity"]
    assert "Body: Common (50.0%)" in universe[0]["trait_rarity"]


def test_verified_canonical_mint_extends_sparse_cached_rarity_and_aggregate_hashes() -> None:
    surface = load_module()
    history = [
        {
            "token_id": 0,
            "traits": rarity_traits("Rare"),
            "metadata_verification_status": "onchain_token_uri_verified",
        },
        {
            "token_id": 1,
            "traits": "",
            "metadata_verification_status": "onchain_token_uri_unavailable",
        },
        {
            "token_id": 2,
            "traits": rarity_traits("Common"),
            "metadata_verification_status": "onchain_token_uri_verified",
        },
    ]
    baseline_hash = hashlib.sha256(b"0,2").hexdigest()
    metrics = [
        {"metric": "dog_base_existing_count", "value": "2"},
        {"metric": "dog_base_existing_token_ids_sha256", "value": baseline_hash},
        {"metric": "dog_token_uri_present_count", "value": "2"},
        {"metric": "dog_metadata_onchain_verified_count", "value": "2"},
        {"metric": "dog_metadata_content_observed_count", "value": "2"},
        {"metric": "dog_rarity_universe_count", "value": "2"},
    ]

    universe = surface.build_extended_rarity_universe(
        history,
        {3: rarity_attrs("Unique")},
        (3,),
        2,
        baseline_hash,
    )
    updates = surface.extend_rarity_aggregate_metrics(metrics, (3,), set(universe))

    assert set(universe) == {0, 2, 3}
    assert {row["rarity"].split("/")[1] for row in universe.values()} == {"3"}
    assert "Body: Unique (33.3%)" in universe[3]["trait_rarity"]
    assert updates == {
        "dog_token_uri_present_count": "3",
        "dog_base_existing_count": "3",
        "dog_base_existing_token_ids_sha256": hashlib.sha256(b"0,2,3").hexdigest(),
        "dog_metadata_onchain_verified_count": "3",
        "dog_metadata_content_observed_count": "3",
        "dog_rarity_universe_count": "3",
    }


def test_fast_rarity_cache_enforces_per_entry_freshness_and_trait_identity() -> None:
    surface = load_module()
    observed_at = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
    history = [
        {
            "token_id": 0,
            "traits": rarity_traits("Blue"),
            "metadata_verification_status": "onchain_token_uri_verified",
        }
    ]

    def record(
        age_seconds: int,
        *,
        body: str = "Blue",
        fetched_at: str | None = None,
        verified_block: int = 100,
    ) -> dict:
        metadata = {
            "token_id": 0,
            "name": "Degen Dog #0",
            "attributes": [
                {"trait_type": trait_type, "value": value}
                for trait_type, value in rarity_attrs(body).items()
            ],
        }
        metadata_payload = json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "token_uri_sha256": "a" * 64,
            "content_sha256": "b" * 64,
            "metadata_sha256": hashlib.sha256(metadata_payload.encode("utf-8")).hexdigest(),
            "verified_block": verified_block,
            "fetched_at_utc": fetched_at
            or (observed_at - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z"),
            "metadata": metadata,
        }

    cache: dict[str, dict] = {}
    builder = SimpleNamespace(
        DOG_METADATA_CACHE_MAX_AGE_SECONDS=100,
        load_dog_cache=lambda: cache,
    )
    expected_hash = surface.rarity_token_ids_sha256({0})
    for age_seconds in (99, 100):
        cache = {"0": record(age_seconds), "999": {"ignored": True}}
        projected = surface.validate_fresh_rarity_cache(
            builder,
            history,
            1,
            expected_hash,
            100,
            observed_at=observed_at,
        )
        assert set(projected) == {"0"}

    for bad_record, expected in (
        (record(101), "expired"),
        (record(0, fetched_at="2026-08-20T12:00:01Z"), "future-dated"),
        (record(0, fetched_at="not-a-time"), "malformed observation time"),
        (record(0, fetched_at="2026-08-20T12:00:00"), "timezone-less observation time"),
        (record(0, body="Red"), "traits differ"),
        (record(0, verified_block=101), "verified block is invalid"),
    ):
        cache = {"0": bad_record}
        try:
            surface.validate_fresh_rarity_cache(
                builder,
                history,
                1,
                expected_hash,
                100,
                observed_at=observed_at,
            )
        except surface.FullRefreshRequired as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"invalid cache record was accepted: {expected}")

    cache = {}
    try:
        surface.validate_fresh_rarity_cache(
            builder,
            history,
            1,
            expected_hash,
            100,
            observed_at=observed_at,
        )
    except surface.FullRefreshRequired as exc:
        assert "does not cover" in str(exc)
    else:
        raise AssertionError("missing rarity cache coverage was accepted")


def test_rarity_history_requires_exact_verified_metadata_provenance() -> None:
    surface = load_module()
    unverified = {
        "token_id": 0,
        "traits": rarity_traits("Blue"),
        "metadata_verification_status": "",
    }
    try:
        surface.verified_rarity_history_attributes([unverified])
    except surface.FullRefreshRequired as exc:
        assert "without verified metadata provenance" in str(exc)
    else:
        raise AssertionError("traits without exact metadata provenance entered rarity scoring")

    unavailable = dict(unverified)
    unavailable["metadata_verification_status"] = "onchain_token_uri_unavailable"
    assert surface.verified_rarity_history_attributes([unavailable]) == {}


def test_sparse_verified_base_universe_excludes_unclaimed_history_rows() -> None:
    surface = load_module()
    history = [
        {
            "token_id": 0,
            "traits": rarity_traits("Rare"),
            "metadata_verification_status": "onchain_token_uri_verified",
        },
        {
            "token_id": 1,
            "traits": "",
            "metadata_verification_status": "onchain_token_uri_unavailable",
        },
        {
            "token_id": 2,
            "traits": rarity_traits("Common"),
            "metadata_verification_status": "onchain_token_uri_verified",
        },
    ]
    expected_hash = hashlib.sha256(b"0,2").hexdigest()

    universe = surface.build_rarity_universe(
        history,
        2,
        rarity_attrs("Common"),
        2,
        expected_hash,
    )

    assert set(universe) == {0, 2}
    assert {row["rarity"].split("/")[1] for row in universe.values()} == {"2"}


def test_incremental_rarity_rejects_same_size_phantom_token_set() -> None:
    surface = load_module()
    history = [
        {
            "token_id": 0,
            "traits": rarity_traits("Rare"),
            "metadata_verification_status": "onchain_token_uri_verified",
        },
        {
            "token_id": 99,
            "traits": rarity_traits("Common"),
            "metadata_verification_status": "onchain_token_uri_verified",
        },
    ]
    try:
        surface.build_rarity_universe(
            history,
            0,
            rarity_attrs("Rare"),
            2,
            hashlib.sha256(b"0,2").hexdigest(),
        )
    except surface.FullRefreshRequired as exc:
        assert "verified Base existence set" in str(exc)
    else:
        raise AssertionError("same-size phantom rarity token set was accepted")


def test_exact_nonexistent_base_ids_do_not_withhold_incremental_rarity() -> None:
    surface = load_module()
    metrics = [
        {"metric": "dog_total_supply", "value": "792"},
        {"metric": "dog_id_ceiling", "value": "792"},
        {"metric": "dog_token_uri_present_count", "value": "667"},
        {"metric": "dog_token_uri_unavailable_count", "value": "125"},
        {"metric": "dog_base_existing_count", "value": "667"},
        {"metric": "dog_base_unclaimed_count", "value": "125"},
        {"metric": "dog_base_existing_token_ids_sha256", "value": "a" * 64},
        {"metric": "dog_base_unclaimed_token_ids_sha256", "value": "b" * 64},
        {"metric": "dog_metadata_onchain_verified_count", "value": "667"},
        {"metric": "dog_metadata_unavailable_count", "value": "125"},
        {"metric": "dog_metadata_content_verification_status", "value": "verified_token_uri_offchain_content_hash_observed"},
        {"metric": "dog_metadata_content_observed_count", "value": "667"},
        {"metric": "dog_token_uri_verification_status", "value": "hash_pinned_cross_provider_exact_outcome_quorum"},
        {"metric": "dog_base_existence_verification_status", "value": "hash_pinned_cross_provider_exists_token_uri_parity_quorum"},
        {"metric": "dog_metadata_verification_status", "value": "partial_onchain_token_uri_unavailable"},
        {"metric": "dog_rarity_verification_status", "value": "complete_verified_existing_token_universe"},
        {"metric": "dog_rarity_universe_count", "value": "667"},
        {"metric": "dog_rarity_excluded_nonexistent_count", "value": "125"},
        {"metric": "dog_rarity_incomplete_metadata_count", "value": "0"},
        {"metric": "dog_rarity_scope", "value": "base_existing"},
        {"metric": "dog_rarity_score_method", "value": "sum_existing_token_count_divided_by_trait_frequency_v1"},
        {"metric": "dog_rarity_tie_policy", "value": "competition_rank_equal_scores_share_rank"},
        {"metric": "dog_rarity_trait_schema", "value": "Background|Body|Neck|Mouth|Ears|Head|Eyes"},
    ]

    assert surface.baseline_rarity_universe_size(metrics, 792) == 667


def test_incremental_rarity_rejects_stale_metadata_supply_counts() -> None:
    surface = load_module()
    metrics = [
        {"metric": "dog_total_supply", "value": "791"},
        {"metric": "dog_id_ceiling", "value": "791"},
        {"metric": "dog_token_uri_present_count", "value": "791"},
        {"metric": "dog_token_uri_unavailable_count", "value": "0"},
        {"metric": "dog_base_existing_count", "value": "791"},
        {"metric": "dog_base_unclaimed_count", "value": "0"},
        {"metric": "dog_base_existing_token_ids_sha256", "value": "a" * 64},
        {"metric": "dog_base_unclaimed_token_ids_sha256", "value": "b" * 64},
        {"metric": "dog_metadata_onchain_verified_count", "value": "791"},
        {"metric": "dog_metadata_unavailable_count", "value": "0"},
        {"metric": "dog_metadata_content_verification_status", "value": "verified_token_uri_offchain_content_hash_observed"},
        {"metric": "dog_metadata_content_observed_count", "value": "791"},
        {"metric": "dog_token_uri_verification_status", "value": "hash_pinned_cross_provider_exact_outcome_quorum"},
        {"metric": "dog_base_existence_verification_status", "value": "hash_pinned_cross_provider_exists_token_uri_parity_quorum"},
        {"metric": "dog_metadata_verification_status", "value": "complete_onchain_token_uri_verified"},
        {"metric": "dog_rarity_verification_status", "value": "complete_verified_existing_token_universe"},
        {"metric": "dog_rarity_universe_count", "value": "791"},
        {"metric": "dog_rarity_excluded_nonexistent_count", "value": "0"},
        {"metric": "dog_rarity_incomplete_metadata_count", "value": "0"},
        {"metric": "dog_rarity_scope", "value": "base_existing"},
        {"metric": "dog_rarity_score_method", "value": "sum_existing_token_count_divided_by_trait_frequency_v1"},
        {"metric": "dog_rarity_tie_policy", "value": "competition_rank_equal_scores_share_rank"},
        {"metric": "dog_rarity_trait_schema", "value": "Background|Body|Neck|Mouth|Ears|Head|Eyes"},
    ]

    try:
        surface.baseline_rarity_universe_size(metrics, 792)
    except surface.FullRefreshRequired as exc:
        assert "baseline metadata covers supply 791" in str(exc)
    else:
        raise AssertionError("stale metadata supply counts were accepted")


def test_fast_rarity_continuity_accepts_only_exact_canonical_mint_extension() -> None:
    surface = load_module()
    attested_hash = "0x" + "a" * 64
    latest_hash = "0x" + "b" * 64
    dog_address = "0x" + "1" * 40
    auction_address = "0x" + "4" * 40
    code_hash = "c" * 64
    metrics = [
        {"metric": "dog_total_supply", "value": "100"},
        {"metric": "dog_rarity_attested_block", "value": "100"},
        {"metric": "dog_rarity_attested_block_hash", "value": attested_hash},
        {"metric": "dog_rarity_continuity_through_block", "value": "100"},
        {"metric": "dog_rarity_continuity_through_block_hash", "value": attested_hash},
        {"metric": "latest_block", "value": "100"},
        {"metric": "snapshot_block_hash", "value": attested_hash},
        {"metric": "dog_token_uri_verification_status", "value": surface.FULL_TOKEN_URI_VERIFICATION_STATUS},
        {"metric": "dog_base_existence_verification_status", "value": surface.FULL_EXISTENCE_VERIFICATION_STATUS},
        {"metric": "dog_rarity_continuity_verification_status", "value": surface.FULL_RARITY_CONTINUITY_STATUS},
        {"metric": "dog_nft_code_sha256", "value": code_hash},
    ]

    def builder_with_logs(logs: list[dict]) -> SimpleNamespace:
        def rpc_quorum(method: str, params: list, **_kwargs):
            if method == "eth_getBlockByNumber":
                requested_block = int(params[0], 16)
                requested_hash = latest_hash if requested_block == 120 else attested_hash
                return (
                    {"number": hex(requested_block), "hash": requested_hash},
                    ["https://one.example", "https://two.example"],
                )
            if method == "eth_getLogs":
                return logs, ["https://one.example", "https://two.example"]
            raise AssertionError(f"unexpected RPC method {method}")

        return SimpleNamespace(
            DEGEN_DOGS=dog_address,
            AUCTION_HOUSE=auction_address,
            VERIFIED_LOG_URLS=["https://one.example", "https://two.example"],
            RPC_QUORUM_SIZE=2,
            LOG_RPC_TIMEOUT=20,
            LOG_QUORUM_MAX_BLOCKS=10_000,
            log_filter=lambda address, topics, start, end: {
                "address": address,
                "topics": [topics],
                "fromBlock": hex(start),
                "toBlock": hex(end),
            },
            rpc_quorum=rpc_quorum,
        )

    normal_transfer = continuity_log(
        dog_address,
        [
            surface.RARITY_MUTATION_TOPICS[0],
            "0x" + "0" * 24 + "2" * 40,
            "0x" + "0" * 24 + "3" * 40,
            "0x" + "0" * 63 + "1",
        ],
        block=109,
        log_index=0,
    )
    result = surface.verify_rarity_universe_continuity(
        builder_with_logs([normal_transfer]),
        metrics,
        latest_block=120,
        latest_block_hash=latest_hash,
        latest_dog_code_sha256=code_hash,
        latest_total_supply=100,
    )
    assert result["metrics"]["dog_rarity_continuity_through_block"] == "120"
    assert result["metrics"]["dog_rarity_continuity_through_block_hash"] == latest_hash
    assert (
        result["metrics"]["dog_rarity_continuity_verification_status"]
        == surface.INCREMENTAL_RARITY_CONTINUITY_STATUS
    )
    assert result["minted_token_ids"] == ()

    canonical_mint = continuity_log(
        dog_address,
        [
            surface.RARITY_MUTATION_TOPICS[0],
            surface.ZERO_ADDRESS_TOPIC,
            "0x" + "0" * 24 + auction_address[2:],
            "0x" + f"{100:064x}",
        ],
        block=110,
        log_index=1,
    )
    extended = surface.verify_rarity_universe_continuity(
        builder_with_logs([normal_transfer, canonical_mint]),
        metrics,
        latest_block=120,
        latest_block_hash=latest_hash,
        latest_dog_code_sha256=code_hash,
        latest_total_supply=101,
    )
    assert extended["minted_token_ids"] == (100,)
    assert (
        extended["metrics"]["dog_rarity_continuity_verification_status"]
        == surface.EXTENDED_RARITY_CONTINUITY_STATUS
    )
    assert extended["metrics"]["dog_rarity_extension_mint_count"] == "1"
    assert extended["metrics"]["dog_rarity_extension_mint_token_ids"] == "100"
    assert extended["metrics"]["dog_rarity_extension_mint_token_ids_sha256"] == hashlib.sha256(
        b"100"
    ).hexdigest()
    persisted_metrics = {row["metric"]: str(row["value"]) for row in metrics}
    persisted_metrics.update(extended["metrics"])
    persisted_metrics.update(
        {
            "dog_total_supply": "101",
            "latest_block": "120",
            "snapshot_block_hash": latest_hash,
        }
    )
    persisted = surface.verify_rarity_universe_continuity(
        builder_with_logs([]),
        [{"metric": key, "value": value} for key, value in persisted_metrics.items()],
        latest_block=120,
        latest_block_hash=latest_hash,
        latest_dog_code_sha256=code_hash,
        latest_total_supply=101,
    )
    assert persisted["minted_token_ids"] == ()
    assert persisted["metrics"]["dog_rarity_extension_mint_token_ids"] == "100"

    non_suffix_metrics = dict(persisted_metrics)
    non_suffix_metrics.update(
        {
            "dog_rarity_extension_mint_count": "1",
            "dog_rarity_extension_mint_token_ids": "50",
            "dog_rarity_extension_mint_token_ids_sha256": hashlib.sha256(b"50").hexdigest(),
        }
    )
    try:
        surface.verify_rarity_universe_continuity(
            builder_with_logs([]),
            [{"metric": key, "value": value} for key, value in non_suffix_metrics.items()],
            latest_block=120,
            latest_block_hash=latest_hash,
            latest_dog_code_sha256=code_hash,
            latest_total_supply=101,
        )
    except surface.FullRefreshRequired as exc:
        assert "mint-extension provenance is inconsistent" in str(exc)
    else:
        raise AssertionError("expected non-suffix extension provenance to require a full refresh")

    second_latest_hash = "0x" + "d" * 64
    second_mint = continuity_log(
        dog_address,
        [
            surface.RARITY_MUTATION_TOPICS[0],
            surface.ZERO_ADDRESS_TOPIC,
            "0x" + "0" * 24 + auction_address[2:],
            "0x" + f"{101:064x}",
        ],
        block=125,
        log_index=0,
    )
    second_extension = surface.verify_rarity_universe_continuity(
        builder_with_logs([second_mint]),
        [{"metric": key, "value": value} for key, value in persisted_metrics.items()],
        latest_block=130,
        latest_block_hash=second_latest_hash,
        latest_dog_code_sha256=code_hash,
        latest_total_supply=102,
    )
    assert second_extension["minted_token_ids"] == (101,)
    assert second_extension["metrics"]["dog_rarity_extension_mint_count"] == "2"
    assert second_extension["metrics"]["dog_rarity_extension_mint_token_ids"] == "100,101"

    burn = dict(normal_transfer)
    burn["topics"] = list(normal_transfer["topics"])
    burn["topics"][2] = surface.ZERO_ADDRESS_TOPIC
    noncanonical_mint = dict(canonical_mint)
    noncanonical_mint["topics"] = list(canonical_mint["topics"])
    noncanonical_mint["topics"][2] = "0x" + "0" * 24 + "3" * 40
    metadata_update = continuity_log(
        dog_address,
        [surface.RARITY_MUTATION_TOPICS[1]],
        block=110,
        log_index=1,
    )
    malformed = dict(normal_transfer)
    malformed.pop("blockNumber")
    for logs, latest_supply, expected in (
        ([burn], 100, "burn changed"),
        ([metadata_update], 100, "metadata changed"),
        ([noncanonical_mint], 101, "recipient is not the canonical auction house"),
        ([canonical_mint], 102, "gapped or mismatched"),
        ([malformed], 100, "malformed blockNumber"),
        ([normal_transfer, normal_transfer], 100, "duplicated"),
    ):
        try:
            surface.verify_rarity_universe_continuity(
                builder_with_logs(logs),
                metrics,
                latest_block=120,
                latest_block_hash=latest_hash,
                latest_dog_code_sha256=code_hash,
                latest_total_supply=latest_supply,
            )
        except surface.FullRefreshRequired as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("noncanonical rarity continuity was accepted by the fast refresh")


def test_fast_rarity_continuity_advances_from_verified_checkpoint_only() -> None:
    surface = load_module()
    attested_hash = "0x" + "a" * 64
    checkpoint_hash = "0x" + "b" * 64
    latest_hash = "0x" + "c" * 64
    dog_address = "0x" + "1" * 40
    auction_address = "0x" + "4" * 40
    code_hash = "d" * 64
    metrics = [
        {"metric": "dog_total_supply", "value": "100"},
        {"metric": "dog_rarity_attested_block", "value": "100"},
        {"metric": "dog_rarity_attested_block_hash", "value": attested_hash},
        {"metric": "dog_rarity_continuity_through_block", "value": "120"},
        {"metric": "dog_rarity_continuity_through_block_hash", "value": checkpoint_hash},
        {"metric": "latest_block", "value": "120"},
        {"metric": "snapshot_block_hash", "value": checkpoint_hash},
        {"metric": "dog_token_uri_verification_status", "value": surface.CONTINUITY_TOKEN_URI_VERIFICATION_STATUS},
        {"metric": "dog_base_existence_verification_status", "value": surface.CONTINUITY_EXISTENCE_VERIFICATION_STATUS},
        {"metric": "dog_rarity_continuity_verification_status", "value": surface.INCREMENTAL_RARITY_CONTINUITY_STATUS},
        {"metric": "dog_nft_code_sha256", "value": code_hash},
    ]
    calls: list[tuple[str, list]] = []

    def rpc_quorum(method: str, params: list, **_kwargs):
        calls.append((method, params))
        if method == "eth_getBlockByNumber":
            assert params == [hex(120), False]
            return (
                {"number": hex(120), "hash": checkpoint_hash},
                ["https://one.example", "https://two.example"],
            )
        if method == "eth_getLogs":
            return [], ["https://one.example", "https://two.example"]
        raise AssertionError(f"unexpected RPC method {method}")

    builder = SimpleNamespace(
        DEGEN_DOGS=dog_address,
        AUCTION_HOUSE=auction_address,
        VERIFIED_LOG_URLS=["https://one.example", "https://two.example"],
        RPC_QUORUM_SIZE=2,
        LOG_RPC_TIMEOUT=20,
        LOG_QUORUM_MAX_BLOCKS=7,
        log_filter=lambda address, topics, start, end: {
            "address": address,
            "topics": [topics],
            "fromBlock": hex(start),
            "toBlock": hex(end),
        },
        rpc_quorum=rpc_quorum,
    )

    same_checkpoint = surface.verify_rarity_universe_continuity(
        builder,
        metrics,
        latest_block=120,
        latest_block_hash=checkpoint_hash,
        latest_dog_code_sha256=code_hash,
        latest_total_supply=100,
    )
    assert same_checkpoint["metrics"]["dog_rarity_continuity_through_block"] == "120"
    assert calls == []

    for invalid_block, invalid_hash, expected_exception, expected in (
        (120, latest_hash, surface.FullRefreshRequired, "checkpoint hash changed"),
        (119, latest_hash, surface.TransientSnapshotLag, "preserving newer artifacts"),
    ):
        try:
            surface.verify_rarity_universe_continuity(
                builder,
                metrics,
                latest_block=invalid_block,
                latest_block_hash=invalid_hash,
                latest_dog_code_sha256=code_hash,
                latest_total_supply=100,
            )
        except expected_exception as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("an invalid latest rarity checkpoint was accepted")
    assert calls == []

    result = surface.verify_rarity_universe_continuity(
        builder,
        metrics,
        latest_block=140,
        latest_block_hash=latest_hash,
        latest_dog_code_sha256=code_hash,
        latest_total_supply=100,
    )

    assert [method for method, _params in calls] == [
        "eth_getBlockByNumber",
        "eth_getLogs",
        "eth_getLogs",
        "eth_getLogs",
    ]
    assert [
        (params[0]["fromBlock"], params[0]["toBlock"])
        for method, params in calls
        if method == "eth_getLogs"
    ] == [
        (hex(121), hex(127)),
        (hex(128), hex(134)),
        (hex(135), hex(140)),
    ]
    assert result["metrics"]["dog_rarity_attested_block"] == "100"
    assert result["metrics"]["dog_rarity_attested_block_hash"] == attested_hash
    assert result["metrics"]["dog_rarity_continuity_through_block"] == "140"
    assert result["metrics"]["dog_rarity_continuity_through_block_hash"] == latest_hash

    wrong_checkpoint_hash = "0x" + "e" * 64
    for row in metrics:
        if row["metric"] in {"dog_rarity_continuity_through_block_hash", "snapshot_block_hash"}:
            row["value"] = wrong_checkpoint_hash
    try:
        surface.verify_rarity_universe_continuity(
            builder,
            metrics,
            latest_block=140,
            latest_block_hash=latest_hash,
            latest_dog_code_sha256=code_hash,
            latest_total_supply=100,
        )
    except surface.FullRefreshRequired as exc:
        assert "checkpoint hash changed" in str(exc)
    else:
        raise AssertionError("a changed continuity checkpoint hash was accepted")


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
