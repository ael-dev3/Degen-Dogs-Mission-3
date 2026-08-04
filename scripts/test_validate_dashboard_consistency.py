#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import csv
import hashlib
import io
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


def metrics_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["metric", "value"], lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


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
    existing_ids_sha256 = hashlib.sha256(
        ",".join(str(token_id) for token_id in range(792)).encode("ascii")
    ).hexdigest()
    unclaimed_ids_sha256 = hashlib.sha256(b"").hexdigest()
    apr_display_value = apr_display or index_apr
    metrics = {
        "latest_block": "46732183",
        "latest_block_time_utc": "2026-05-31 18:55:13",
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": "snapshot_hash,contract_code,current_auction,dog_total_supply,dog_token_uri_bindings,recent_event_logs",
        "onchain_chain_id": "8453",
        "snapshot_block_hash": "0x" + "a" * 64,
        "snapshot_confirmations": "1",
        "rpc_quorum_size": "2",
        "rpc_quorum_agreement": "2/2",
        "rpc_quorum_providers": "provider-one.example|provider-two.example",
        "log_rpc_quorum_providers": "provider-one.example|provider-two.example",
        "auction_house_code_sha256": "b" * 64,
        "dog_nft_code_sha256": "c" * 64,
        "current_auction_token_id": "729",
        "dog_total_supply": "792",
        "dog_id_ceiling": "792",
        "dog_token_uri_verification_status": "hash_pinned_cross_provider_exact_outcome_quorum",
        "dog_base_existence_verification_status": "hash_pinned_cross_provider_exists_token_uri_parity_quorum",
        "dog_token_uri_present_count": "792",
        "dog_token_uri_unavailable_count": "0",
        "dog_base_existing_count": "792",
        "dog_base_unclaimed_count": "0",
        "dog_base_existing_token_ids_sha256": existing_ids_sha256,
        "dog_base_unclaimed_token_ids_sha256": unclaimed_ids_sha256,
        "dog_metadata_verification_status": "complete_onchain_token_uri_verified",
        "dog_metadata_onchain_verified_count": "792",
        "dog_metadata_unavailable_count": "0",
        "dog_metadata_content_verification_status": "verified_token_uri_offchain_content_hash_observed",
        "dog_metadata_content_observed_count": "792",
        "dog_rarity_verification_status": "complete_verified_existing_token_universe",
        "dog_rarity_universe_count": "792",
        "dog_rarity_excluded_nonexistent_count": "0",
        "dog_rarity_incomplete_metadata_count": "0",
        "dog_rarity_scope": "base_existing",
        "dog_rarity_score_method": "sum_existing_token_count_divided_by_trait_frequency_v1",
        "dog_rarity_tie_policy": "competition_rank_equal_scores_share_rank",
        "dog_rarity_trait_schema": "Background|Body|Neck|Mouth|Ears|Head|Eyes",
        "dog_rarity_attested_block": "46732183",
        "dog_rarity_attested_block_hash": "0x" + "a" * 64,
        "dog_rarity_continuity_through_block": "46732183",
        "dog_rarity_continuity_through_block_hash": "0x" + "a" * 64,
        "dog_rarity_continuity_verification_status": "full_snapshot_exists_token_uri_content_schema_attested",
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
    write_text(root / "generated" / "mission3_metrics.csv", metrics_csv(metric_rows))
    write_json(root / "generated" / "mission3_metrics.json", metric_rows)
    write_json(root / "public" / "generated" / "mission3_metrics.json", metric_rows)

    current = {
        "token_id": 729,
        "current_bid": "0.01000 ETH ($20)",
        "current_bid_eth": 0.01,
        "current_bid_usd": 19.98,
        "eth_usd_price_live": "1998",
        "eth_usd_price_date_utc": "2026-05-30",
        "bidder": "@0xael.eth",
        "bidder_wallet": wallet,
        "auction_state": "live",
        "rarity": "#1/792",
        "rarity_score": 7,
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
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_verification_scope": "snapshot_hash,contract_code,current_auction,dog_total_supply,dog_token_uri_bindings,recent_event_logs",
        "onchain_chain_id": "8453",
        "snapshot_block_hash": "0x" + "a" * 64,
        "snapshot_confirmations": "1",
        "rpc_quorum_size": "2",
        "rpc_quorum_agreement": "2/2",
        "rpc_quorum_providers": "provider-one.example|provider-two.example",
        "log_rpc_quorum_providers": "provider-one.example|provider-two.example",
        "auction_house_code_sha256": "b" * 64,
        "dog_nft_code_sha256": "c" * 64,
        "dog_total_supply": 792,
        "dog_id_ceiling": 792,
        "dog_token_uri_verification_status": "hash_pinned_cross_provider_exact_outcome_quorum",
        "dog_base_existence_verification_status": "hash_pinned_cross_provider_exists_token_uri_parity_quorum",
        "dog_token_uri_present_count": 792,
        "dog_token_uri_unavailable_count": 0,
        "dog_base_existing_count": 792,
        "dog_base_unclaimed_count": 0,
        "dog_base_existing_token_ids_sha256": existing_ids_sha256,
        "dog_base_unclaimed_token_ids_sha256": unclaimed_ids_sha256,
        "dog_metadata_verification_status": "complete_onchain_token_uri_verified",
        "dog_metadata_onchain_verified_count": 792,
        "dog_metadata_unavailable_count": 0,
        "dog_metadata_content_verification_status": "verified_token_uri_offchain_content_hash_observed",
        "dog_metadata_content_observed_count": 792,
        "dog_rarity_verification_status": "complete_verified_existing_token_universe",
        "dog_rarity_universe_count": 792,
        "dog_rarity_excluded_nonexistent_count": 0,
        "dog_rarity_incomplete_metadata_count": 0,
        "dog_rarity_scope": "base_existing",
        "dog_rarity_score_method": "sum_existing_token_count_divided_by_trait_frequency_v1",
        "dog_rarity_tie_policy": "competition_rank_equal_scores_share_rank",
        "dog_rarity_trait_schema": "Background|Body|Neck|Mouth|Ears|Head|Eyes",
        "dog_rarity_attested_block": 46732183,
        "dog_rarity_attested_block_hash": "0x" + "a" * 64,
        "dog_rarity_continuity_through_block": 46732183,
        "dog_rarity_continuity_through_block_hash": "0x" + "a" * 64,
        "dog_rarity_continuity_verification_status": "full_snapshot_exists_token_uri_content_schema_attested",
    }
    write_json(root / "generated" / "refresh_status.json", refresh_status)
    write_json(root / "public" / "generated" / "refresh_status.json", refresh_status)
    write_json(root / "generated" / "current_latest_bid.json", [{"latest_bid_eth": 0.01, "latest_bid_usd": 19.98, "bidder": "@0xael.eth", "bidder_wallet": wallet, "bid_time_utc": "2026-05-30 18:40:23"}])
    current_history = [{
        "token_id": 729,
        "dog": "Dog #729",
        "bidder": "@0xael.eth",
        "bidder_wallet": wallet,
        "bid": "0.01000 ETH ($20)",
        "bid_eth": "0.01",
        "bid_usd": "19.98",
        "eth_usd_price_live": "1998",
        "usd_estimate_source": "current_eth_usd_price",
        "usd_estimate_confidence": "live_current",
        "bid_time_utc": "2026-05-30 18:40:23",
        "block_number": 46732183,
        "log_index": 1,
        "tx_hash": "0x" + "1" * 64,
    }]
    write_json(root / "generated" / "current_auction_bid_history.json", current_history)
    write_json(root / "public" / "generated" / "current_auction_bid_history.json", current_history)
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
    rarity_traits = "; ".join(f"{trait_type}: None" for trait_type in (
        "Background", "Body", "Neck", "Mouth", "Ears", "Head", "Eyes"
    ))
    rarity_percentages = "; ".join(f"{trait_type}: None (100.0%)" for trait_type in (
        "Background", "Body", "Neck", "Mouth", "Ears", "Head", "Eyes"
    ))
    historical_rows = [
        {
            "mission": 1 if token_id <= 200 else 2 if token_id <= 589 else 3,
            "token_id": token_id,
            "traits": rarity_traits,
            "trait_rarity": rarity_percentages,
            "rarity": "#1/792",
            "rarity_score": 7,
            "metadata_verification_status": "onchain_token_uri_verified",
            **(
                {
                    "winner": "@0xael.eth",
                    "winner_wallet": wallet,
                    "amount": "0.01000 ETH ($20)",
                }
                if token_id == 729
                else {}
            ),
        }
        for token_id in range(792)
    ]
    write_json(root / "generated" / "historical_dog_search.json", historical_rows)
    recent_bid = dict(current_history[0])
    write_json(root / "generated" / "recent_bids.json", [recent_bid])
    write_json(root / "public" / "generated" / "recent_bids.json", [recent_bid])
    timeline_row = {
        "token_id": 729,
        "auction_state": "live",
        "bids": 1,
        "unique_bidders": 1,
        "high_bid_eth": "0.01",
        "total_bid_eth": "0.01",
        "latest_bidder": "@0xael.eth",
        "latest_bid_eth": "0.01",
        "latest_bid_utc": "2026-05-30 18:40:23",
        "start_time_utc": "2026-05-30 18:00:00",
        "end_time_utc": "2026-05-31 20:40:09",
        "settled_eth": "",
        "settled_time_utc": "",
        "created_tx_hash": "0x" + "2" * 64,
        "settled_tx_hash": "",
    }
    daily_row = {
        "activity_day": "2026-05-30",
        "created_auctions": 0,
        "settled_auctions": 0,
        "bids": 1,
        "unique_bidders": 1,
        "bid_eth": "0.01",
        "high_bid_eth": "0.01",
        "settled_eth": "0",
    }
    bidder_row = {
        "bidder": "@0xael.eth",
        "bidder_wallet": wallet,
        "bids": 1,
        "auctions_bid": 1,
        "bid_eth": "0.01",
        "high_bid_eth": "0.01",
        "latest_bid_token_id": 729,
        "latest_bid_utc": "2026-05-30 18:40:23",
    }
    for folder in (root / "generated", root / "public" / "generated"):
        write_json(folder / "auction_timeline.json", [timeline_row])
        write_json(folder / "auction_daily_activity.json", [daily_row])
        write_json(folder / "auction_bidder_leaderboard.json", [bidder_row])
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
        "status": "ongoing",
        "winner_or_high_bidder": {"wallet": wallet, "display": "@0xael.eth"},
        "amount": {
            "native": "0.01",
            "native_symbol": "ETH",
            "usd_estimate": "19.98",
            "usd_estimate_display": "$19.98",
            "usd_estimate_price_usd": "1998",
            "usd_estimate_price_date_utc": "2026-05-30",
            "usd_estimate_source": "generated_auction_feed",
            "usd_estimate_confidence": "medium",
        },
        "activity_time_utc": "2026-05-30T18:40:23Z",
        "bid_stats": {"bid_count": 1, "unique_bidder_count": 1},
        "bid_tx_hashes": ["0x" + "1" * 64],
        "search_text": f"dog 729 {wallet} @0xael.eth 0.01 eth 19.98 $19.98 {'0x' + '1' * 64}",
    }
    write_json(root / "archive" / "data" / "generated" / "unified_dog_search_index.json", [unified_row])
    write_json(root / "public" / "generated" / "unified_dog_search_index.json", [unified_row])
    write_json(root / "archive" / "data" / "identity" / "wallet_profiles.json", {})
    write_json(root / "generated" / "auction_winners.json", [])
    archive_mission3 = root / "archive" / "mission3" / "data" / "generated"
    write_json(archive_mission3 / "mission3_auction_timeline.json", [{
        "token_id": 729,
        "auction_state": "unsettled_or_live",
        "created_tx": "0x" + "2" * 64,
        "settled_tx": None,
        "bids": 1,
        "unique_bidder_count": 1,
        "high_bid_eth": "0.01",
        "latest_bid_eth": "0.01",
        "latest_bidder": wallet,
        "latest_bid_time_utc": "2026-05-30T18:40:23Z",
        "settled_amount_eth": None,
        "start_time_utc": "2026-05-30 18:00:00",
        "end_time_utc": "2026-05-31 20:40:09",
        "settled_time_utc": None,
        "winner": None,
    }])
    write_json(archive_mission3 / "mission3_auction_winners.json", [])
    write_json(archive_mission3 / "mission3_auction_bids.json", [{
        "token_id": 729,
        "amount_eth": "0.01",
        "bidder": wallet,
        "block_number": 46732183,
        "block_time_utc": "2026-05-30T18:40:23Z",
        "log_index": 1,
        "transaction_hash": "0x" + "1" * 64,
    }])

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
        assert result["mission3_archive_parity"] == {
            "checked": True,
            "auctions": 1,
            "settlements": 0,
            "bids": 1,
        }


def test_mission3_archive_parity_accepts_exact_history_and_rejects_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dashboard_timeline = [{
            "token_id": 1,
            "auction_state": "settled",
            "created_tx_hash": "0x" + "a" * 64,
            "settled_tx_hash": "0x" + "b" * 64,
            "bids": 1,
            "unique_bidders": 1,
            "total_bid_eth": "0.5",
            "high_bid_eth": "0.5",
            "latest_bidder": "@winner",
            "latest_bid_eth": "0.5",
            "settled_eth": "0.5",
            "start_time_utc": "2026-01-01T00:00:00Z",
            "end_time_utc": "2026-01-02T00:00:00Z",
            "latest_bid_utc": "2026-01-01T12:00:00Z",
            "settled_time_utc": "2026-01-02T00:01:00Z",
        }]
        dashboard_winners = [{
            "token_id": 1,
            "winner_wallet": "0x" + "1" * 40,
            "winning_bid_eth": "0.5",
            "block_number": 123,
            "tx_hash": "0x" + "b" * 64,
            "bid_count": 1,
            "unique_bidders": 1,
            "first_bid_utc": "2026-01-01T12:00:00Z",
            "last_bid_utc": "2026-01-01T12:00:00Z",
            "settled_time_utc": "2026-01-02T00:01:00Z",
        }]
        archive_root = root / "archive" / "mission3" / "data" / "generated"
        write_json(root / "generated" / "auction_timeline.json", dashboard_timeline)
        write_json(root / "generated" / "auction_winners.json", dashboard_winners)
        write_json(archive_root / "mission3_auction_timeline.json", [{
            "token_id": 1,
            "auction_state": "settled",
            "created_tx": "0x" + "a" * 64,
            "settled_tx": "0x" + "b" * 64,
            "bids": 1,
            "unique_bidder_count": 1,
            "high_bid_eth": "0.5",
            "latest_bid_eth": "0.5",
            "latest_bidder": "0x" + "1" * 40,
            "latest_bid_time_utc": "2026-01-01T12:00:00Z",
            "settled_amount_eth": "0.5",
            "settled_block": 123,
            "winner": "0x" + "1" * 40,
            "start_time_utc": "2026-01-01T00:00:00Z",
            "end_time_utc": "2026-01-02T00:00:00Z",
            "settled_time_utc": "2026-01-02T00:01:00Z",
        }])
        write_json(archive_root / "mission3_auction_winners.json", [{
            "token_id": 1,
            "winner": "0x" + "1" * 40,
            "amount_eth": "0.5",
            "settled_block": 123,
            "settled_tx": "0x" + "b" * 64,
            "bid_count": 1,
            "unique_bidder_count": 1,
            "first_bid_time_utc": "2026-01-01T12:00:00Z",
            "last_bid_time_utc": "2026-01-01T12:00:00Z",
            "settled_time_utc": "2026-01-02T00:01:00Z",
        }])
        write_json(archive_root / "mission3_auction_bids.json", [{
            "token_id": 1,
            "amount_eth": "0.5",
            "bidder": "0x" + "1" * 40,
            "block_number": 100,
            "block_time_utc": "2026-01-01T12:00:00Z",
            "log_index": 7,
            "transaction_hash": "0x" + "c" * 64,
        }])

        validator = load_module()
        result = validator.validate_mission3_archive_parity(root=root)
        assert result == {"checked": True, "auctions": 1, "settlements": 1, "bids": 1}

        dashboard_winners[0]["winning_bid_eth"] = "0.4"
        write_json(root / "generated" / "auction_winners.json", dashboard_winners)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "winning bid differs",
        )


def test_mission3_archive_parity_rejects_configured_archive_with_missing_inputs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "archive" / "mission3" / "config").mkdir(parents=True)
        validator = load_module()
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "archive parity inputs missing",
        )

        archive_root = root / "archive" / "mission3" / "data" / "generated"
        for path in (
            root / "generated" / "auction_timeline.json",
            root / "generated" / "auction_winners.json",
            archive_root / "mission3_auction_timeline.json",
            archive_root / "mission3_auction_winners.json",
            archive_root / "mission3_auction_bids.json",
        ):
            write_json(path, [])
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "archive timeline cannot be empty",
        )


def test_mission3_archive_parity_rejects_unknown_and_duplicate_raw_logs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        validator = load_module()
        bids_path = root / "archive" / "mission3" / "data" / "generated" / "mission3_auction_bids.json"
        bids = json.loads(bids_path.read_text(encoding="utf-8"))
        bids[0]["token_id"] = 730
        write_json(bids_path, bids)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "references unknown Dog #730",
        )

        bids[0]["token_id"] = 729
        bids.append(dict(bids[0]))
        write_json(bids_path, bids)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "duplicate log",
        )


def test_mission3_archive_parity_rejects_malformed_onchain_values_even_when_surfaces_match() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        validator = load_module()
        dashboard_path = root / "generated" / "auction_timeline.json"
        archive_path = root / "archive" / "mission3" / "data" / "generated" / "mission3_auction_timeline.json"
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        archive = json.loads(archive_path.read_text(encoding="utf-8"))
        malformed_hash = "0x" + "g" * 64
        dashboard[0]["created_tx_hash"] = malformed_hash
        archive[0]["created_tx"] = malformed_hash
        write_json(dashboard_path, dashboard)
        write_json(archive_path, archive)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "canonical transaction hash",
        )


def test_mission3_archive_parity_rejects_nonfinite_bid_and_latest_bidder_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        validator = load_module()
        archive_root = root / "archive" / "mission3" / "data" / "generated"
        bids_path = archive_root / "mission3_auction_bids.json"
        bids = json.loads(bids_path.read_text(encoding="utf-8"))
        bids[0]["amount_eth"] = "NaN"
        write_json(bids_path, bids)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "canonical decimal",
        )

        bids[0]["amount_eth"] = "0.01"
        bids[0]["bidder"] = "0x" + "g" * 40
        write_json(bids_path, bids)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "canonical nonzero address",
        )

        bids[0]["bidder"] = "0x76d0e7a13248945ee9f808b4a472262b28778942"
        write_json(bids_path, bids)
        timeline_path = archive_root / "mission3_auction_timeline.json"
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        timeline[0]["latest_bidder"] = "0x" + "3" * 40
        write_json(timeline_path, timeline)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "latest bidder differs from archive raw logs",
        )


def test_mission3_archive_parity_rejects_effective_end_time_drift() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        validator = load_module()
        timeline_path = root / "archive" / "mission3" / "data" / "generated" / "mission3_auction_timeline.json"
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        timeline[0]["end_time_utc"] = "2026-05-31T20:45:09Z"
        write_json(timeline_path, timeline)
        assert_raises_contains(
            lambda: validator.validate_mission3_archive_parity(root=root),
            "end_time_utc differs from quorum archive",
        )


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
        assert_raises_contains(lambda: run_validation(root), "current row exact USD quote provenance")


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


def test_validator_catches_recent_settled_unified_row_missing_usd() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        settled_wallet = "0x0000000000000000000000000000000000000728"
        feed = json.loads((root / "generated" / "auction_feed.json").read_text(encoding="utf-8"))
        feed.append({
            "status": "settled",
            "dog": "Dog #728",
            "bidder_winner": "@settled",
            "bidder_winner_wallet": settled_wallet,
            "bid": "0.02662 ETH ($48)",
            "amount_eth": "0.02662",
            "amount_usd": "48.22",
            "amount_usd_at_event": "48.22",
            "eth_usd_price_at_event": "1811.346676900944",
            "eth_usd_price_date_utc": "2026-06-04",
            "usd_estimate_source": "defillama_coin_prices",
            "usd_estimate_confidence": "medium",
            "auction_time_utc": "2026-06-07 20:10:25",
            "settled_time_utc": "2026-06-07 20:10:25",
            "last_bid_utc": "2026-06-07 20:02:17",
        })
        write_json(root / "generated" / "auction_feed.json", feed)
        stale_unified_row = {
            "mission": 3,
            "dog_id": 728,
            "status": "settled",
            "winner_or_high_bidder": {"wallet": settled_wallet, "display": "@settled"},
            "amount": {
                "native": "0.02662",
                "native_symbol": "ETH",
                "usd_estimate": None,
                "usd_estimate_display": None,
                "usd_estimate_source": None,
                "usd_estimate_confidence": "missing",
            },
            "activity_time_utc": "2026-06-07T20:10:25Z",
            "search_text": "dog 728 @settled 0.02662 eth",
        }
        for rel in [
            "archive/data/generated/unified_dog_search_index.json",
            "public/generated/unified_dog_search_index.json",
        ]:
            rows = json.loads((root / rel).read_text(encoding="utf-8"))
            rows.append(stale_unified_row)
            write_json(root / rel, rows)
        assert_raises_contains(lambda: run_validation(root), "recent archive USD estimate")


def test_validator_catches_current_bid_history_bad_live_usd_math() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        history = json.loads((root / "generated" / "current_auction_bid_history.json").read_text(encoding="utf-8"))
        history[0]["bid_usd"] = "1.00"
        write_json(root / "generated" / "current_auction_bid_history.json", history)
        write_json(root / "public" / "generated" / "current_auction_bid_history.json", history)
        assert_raises_contains(lambda: run_validation(root), "current_auction_bid_history high bid USD differs from current_auction")


def test_validator_catches_current_bid_history_wrong_live_eth_price_math() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        history = json.loads((root / "generated" / "current_auction_bid_history.json").read_text(encoding="utf-8"))
        history[0]["eth_usd_price_live"] = "1"
        write_json(root / "generated" / "current_auction_bid_history.json", history)
        write_json(root / "public" / "generated" / "current_auction_bid_history.json", history)
        assert_raises_contains(lambda: run_validation(root), "current_auction_bid_history bid_usd does not equal bid_eth * live ETH/USD")


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
        write_text(root / "generated" / "mission3_metrics.csv", metrics_csv(metric_rows))
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
        assert_raises_contains(lambda: run_validation(root), "rarity attestation block range")


def test_validator_rejects_missing_cross_provider_verification() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        rows = json.loads((root / "generated" / "mission3_metrics.json").read_text(encoding="utf-8"))
        for row in rows:
            if row.get("metric") == "onchain_verification_status":
                row["value"] = "single_provider"
        for folder in (root / "generated", root / "public" / "generated"):
            write_json(folder / "mission3_metrics.json", rows)
        status = json.loads((root / "generated" / "refresh_status.json").read_text(encoding="utf-8"))
        status["onchain_verification_status"] = "single_provider"
        for folder in (root / "generated", root / "public" / "generated"):
            write_json(folder / "refresh_status.json", status)
        write_text(root / "generated" / "mission3_metrics.csv", metrics_csv(rows))
        index = (root / "index.html").read_text(encoding="utf-8").replace(
            "<td>onchain_verification_status</td><td>current_snapshot_cross_provider_verified</td>",
            "<td>onchain_verification_status</td><td>single_provider</td>",
        )
        write_text(root / "index.html", index)
        assert_raises_contains(lambda: run_validation(root), "not cross-provider verified")


def test_validator_rejects_recent_bids_that_lag_current_history() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        for folder in (root / "generated", root / "public" / "generated"):
            write_json(folder / "recent_bids.json", [])
        assert_raises_contains(lambda: run_validation(root), "recent_bids missing current auction")


def test_validator_rejects_stale_current_timeline_volume() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        rows = json.loads((root / "generated" / "auction_timeline.json").read_text(encoding="utf-8"))
        rows[0]["total_bid_eth"] = "0"
        for folder in (root / "generated", root / "public" / "generated"):
            write_json(folder / "auction_timeline.json", rows)
        assert_raises_contains(lambda: run_validation(root), "total_bid_eth differs")


def test_validator_accepts_generated_rendered_surfaces_that_match_observed_onchain_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        current = json.loads((root / "generated" / "current_auction.json").read_text(encoding="utf-8"))
        write_json(root / "public" / "generated" / "current_auction.json", current)
        validator = load_module()
        observed = {
            "last_observed_token_id": 729,
            "last_observed_high_bidder": "0x76d0e7a13248945ee9f808b4a472262b28778942",
            "last_observed_amount_wei": "10000000000000000",
            "last_observed_bid_log_id": "46732183:0x111:1",
            "last_observed_bid_tx": "0x" + "1" * 64,
            "last_observed_block": 46732183,
        }
        result = validator.validate_current_surface_against_observed_state(observed, root=root)
        assert result["observed_bid_eth"] == "0.01"


def test_validator_catches_stale_generated_public_and_rendered_current_auction_against_observed_onchain_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        current = json.loads((root / "generated" / "current_auction.json").read_text(encoding="utf-8"))
        write_json(root / "public" / "generated" / "current_auction.json", current)
        validator = load_module()
        observed = {
            "last_observed_token_id": 729,
            "last_observed_high_bidder": "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "last_observed_amount_wei": "30000000000000000",
            "last_observed_bid_log_id": "46740000:0x222:2",
            "last_observed_bid_tx": "0x" + "2" * 64,
            "last_observed_block": 46740000,
        }
        assert_raises_contains(
            lambda: validator.validate_current_surface_against_observed_state(observed, root=root),
            "observed onchain current auction",
        )


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"validator_tests=pass count={len(tests)}")
