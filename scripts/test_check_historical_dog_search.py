#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "check_historical_dog_search.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("check_historical_dog_search", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_raises_contains(callback: Any, expected: str) -> None:
    try:
        callback()
    except AssertionError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"expected AssertionError containing {expected!r}")


def test_exact_rarity_permutation_accepts_every_rank_once() -> None:
    checker = load_module()
    rows = [
        {"token_id": "0", "rarity": "#2/3"},
        {"token_id": "1", "rarity": "#1/3"},
        {"token_id": "2", "rarity": "#3/3"},
    ]

    checker.assert_exact_rarity_permutation(rows, 3)


def test_exact_rarity_permutation_rejects_duplicate_and_missing_rank() -> None:
    checker = load_module()
    rows = [
        {"token_id": "0", "rarity": "#1/3"},
        {"token_id": "1", "rarity": "#3/3"},
        {"token_id": "2", "rarity": "#3/3"},
    ]

    assert_raises_contains(
        lambda: checker.assert_exact_rarity_permutation(rows, 3),
        "invalid competition rank 3 at position 2",
    )


def test_competition_rarity_accepts_equal_scores_with_shared_rank() -> None:
    checker = load_module()
    rows = [
        {"token_id": "0", "rarity": "#1/3"},
        {"token_id": "1", "rarity": "#1/3"},
        {"token_id": "2", "rarity": "#3/3"},
    ]

    checker.assert_exact_rarity_permutation(rows, 3)


def rarity_metrics(*, present: int, unavailable: int, metadata_verified: int, metadata_unavailable: int) -> dict[str, str]:
    metadata_status = (
        "complete_onchain_token_uri_verified"
        if metadata_unavailable == 0
        else "partial_onchain_token_uri_unavailable"
        if metadata_unavailable == unavailable
        else "incomplete_metadata_unavailable"
    )
    rarity_incomplete = metadata_unavailable - unavailable
    rarity_status = (
        "complete_verified_existing_token_universe"
        if metadata_verified > 0 and rarity_incomplete == 0
        else "unavailable_no_verified_existing_tokens"
        if metadata_verified == 0
        else "incomplete_existing_token_metadata"
    )
    return {
        "dog_token_uri_verification_status": "hash_pinned_cross_provider_exact_outcome_quorum",
        "dog_base_existence_verification_status": "hash_pinned_cross_provider_exists_token_uri_parity_quorum",
        "dog_token_uri_present_count": str(present),
        "dog_token_uri_unavailable_count": str(unavailable),
        "dog_metadata_verification_status": metadata_status,
        "dog_metadata_onchain_verified_count": str(metadata_verified),
        "dog_metadata_unavailable_count": str(metadata_unavailable),
        "dog_rarity_verification_status": rarity_status,
        "dog_rarity_universe_count": str(metadata_verified),
        "dog_rarity_excluded_nonexistent_count": str(unavailable),
        "dog_rarity_incomplete_metadata_count": str(rarity_incomplete),
        "dog_rarity_scope": "base_existing",
    }


def test_incomplete_metadata_requires_every_rarity_and_score_withheld() -> None:
    checker = load_module()
    rows = [
        {"token_id": "0", "rarity": "Unavailable", "rarity_score": "", "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": "1", "rarity": "Unavailable", "rarity_score": "", "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": "2", "rarity": "Unavailable", "rarity_score": None, "metadata_verification_status": "unavailable"},
    ]
    metrics = rarity_metrics(present=3, unavailable=0, metadata_verified=2, metadata_unavailable=1)

    assert checker.assert_metadata_rarity_state(rows, 3, metrics) == "unavailable"

    partial_rank = copy.deepcopy(rows)
    partial_rank[0]["rarity"] = "#1/3"
    assert_raises_contains(
        lambda: checker.assert_metadata_rarity_state(partial_rank, 3, metrics),
        "partial rarity display",
    )

    partial_score = copy.deepcopy(rows)
    partial_score[1]["rarity_score"] = "1.5"
    assert_raises_contains(
        lambda: checker.assert_metadata_rarity_state(partial_score, 3, metrics),
        "partial rarity score",
    )


def test_complete_metadata_never_accepts_unavailable_or_missing_ranks() -> None:
    checker = load_module()
    rows = [
        {"token_id": str(token_id), "rarity": "Unavailable", "metadata_verification_status": "onchain_token_uri_verified"}
        for token_id in range(3)
    ]
    metrics = rarity_metrics(present=3, unavailable=0, metadata_verified=3, metadata_unavailable=0)

    assert_raises_contains(
        lambda: checker.assert_metadata_rarity_state(rows, 3, metrics),
        "invalid rarity display",
    )


def test_exact_nonexistent_base_id_is_excluded_from_complete_rarity_scope() -> None:
    checker = load_module()
    rows = [
        {"token_id": "0", "rarity": "#1/2", "rarity_score": "8", "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": "1", "rarity": "Unavailable", "rarity_score": "", "metadata_verification_status": "onchain_token_uri_unavailable"},
        {"token_id": "2", "rarity": "#1/2", "rarity_score": "8", "metadata_verification_status": "onchain_token_uri_verified"},
    ]
    metrics = rarity_metrics(present=2, unavailable=1, metadata_verified=2, metadata_unavailable=1)

    assert checker.assert_metadata_rarity_state(rows, 3, metrics) == "complete"


def test_dashboard_wiring_accepts_generated_loader_abstraction() -> None:
    checker = load_module()
    html = "\n".join(checker.DASHBOARD_ARCHIVE_MARKERS)

    checker.assert_dashboard_archive_wiring(html)


def test_dashboard_wiring_rejects_missing_unified_loader_call() -> None:
    checker = load_module()
    required_call = "fetchGenerated('unified_dog_search_index',target.block)"
    html = "\n".join(marker for marker in checker.DASHBOARD_ARCHIVE_MARKERS if marker != required_call)

    assert_raises_contains(lambda: checker.assert_dashboard_archive_wiring(html), required_call)


def mission3_parity_fixture() -> tuple[list[dict[str, object]], ...]:
    genesis_tx = "0x" + "1" * 64
    transition_tx = "0x" + "2" * 64
    wallet = "0x" + "a" * 40
    historical = [
        {
            "mission": "3",
            "token_id": "590",
            "status": "settled",
            "winner_wallet": wallet,
            "amount": "1 ETH ($1)",
            "bid_count": "1",
            "unique_bidder_count": "1",
            "auction_created_time_utc": "2026-01-01 00:00:00",
            "settled_time_utc": "2026-01-02 00:00:00",
        },
        {
            "mission": "3",
            "token_id": "591",
            "status": "live",
            "winner_wallet": "",
            "amount": "",
            "bid_count": "0",
            "unique_bidder_count": "0",
            "auction_created_time_utc": "2026-01-02 00:00:00",
            "settled_time_utc": "",
        },
    ]
    timeline = [
        {
            "token_id": 590,
            "auction_state": "settled",
            "bids": 1,
            "unique_bidders": 1,
            "start_time_utc": "2026-01-01 00:00:00",
            "settled_time_utc": "2026-01-02 00:00:00",
            "created_tx_hash": genesis_tx,
            "settled_tx_hash": transition_tx,
        },
        {
            "token_id": 591,
            "auction_state": "live",
            "bids": 0,
            "unique_bidders": 0,
            "start_time_utc": "2026-01-02 00:00:00",
            "created_tx_hash": transition_tx,
        },
    ]
    winners = [{
        "token_id": 590,
        "winner_wallet": wallet,
        "winning_bid_eth": "1",
        "bid_count": 1,
        "unique_bidders": 1,
        "settled_time_utc": "2026-01-02 00:00:00",
        "block_number": 200,
        "tx_hash": transition_tx,
    }]
    source = [
        {"token_id": 590, "auction_created_block": 100},
        {"token_id": 591, "auction_created_block": None},
    ]
    unified = [
        {
            "mission": 3,
            "dog_id": 590,
            "status": "settled",
            "bid_stats": {"bid_count": 1, "unique_bidder_count": 1},
            "auction_created": {
                "block_number": 100,
                "block_time_utc": "2026-01-01T00:00:00Z",
                "tx_hash": genesis_tx,
            },
            "settlement": {
                "block_number": 200,
                "block_time_utc": "2026-01-02T00:00:00Z",
                "tx_hash": transition_tx,
            },
            "winner_or_high_bidder": {"wallet": wallet},
            "amount": {"native": "1"},
            "source": {"sources": ["generated_auction_timeline", "generated_auction_winners"]},
        },
        {
            "mission": 3,
            "dog_id": 591,
            "status": "ongoing",
            "bid_stats": {"bid_count": 0, "unique_bidder_count": 0},
            "auction_created": {
                "block_number": 200,
                "block_time_utc": "2026-01-02T00:00:00Z",
                "tx_hash": transition_tx,
            },
            "settlement": {"settled": False},
            "winner_or_high_bidder": {"wallet": None},
            "amount": {"native": None},
            "source": {"sources": ["generated_auction_timeline"]},
        },
    ]
    return unified, historical, timeline, winners, source


def test_mission3_onchain_parity_accepts_tx_derived_created_blocks() -> None:
    checker = load_module()
    checker.assert_mission3_onchain_parity(*mission3_parity_fixture())


def test_mission3_onchain_parity_rejects_stale_created_block() -> None:
    checker = load_module()
    fixture = copy.deepcopy(mission3_parity_fixture())
    fixture[0][1]["auction_created"]["block_number"] = 199

    assert_raises_contains(lambda: checker.assert_mission3_onchain_parity(*fixture), "created block differs")


def test_mission3_onchain_parity_rejects_duplicate_tokens() -> None:
    checker = load_module()
    fixture = copy.deepcopy(mission3_parity_fixture())
    fixture[2].append(copy.deepcopy(fixture[2][0]))

    assert_raises_contains(lambda: checker.assert_mission3_onchain_parity(*fixture), "duplicate Dog #590")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"historical_dog_search_checker_tests=pass count={len(tests)}")
