#!/usr/bin/env python3
"""Write human-readable Mission 1 reconciliation report from generated archive data."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission1"
SUMMARY_PATH = ARCHIVE / "data" / "generated" / "reconciliation_summary.json"
REPORT_PATH = ARCHIVE / "docs" / "reconciliation_report.md"
NOTES_PATH = ARCHIVE / "docs" / "verification_notes.md"


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def main() -> int:
    if not SUMMARY_PATH.exists():
        raise SystemExit(f"Missing {SUMMARY_PATH}; run archive_mission1_index.py first")
    s = json.loads(SUMMARY_PATH.read_text())
    lines = [
        "# Mission 1 Reconciliation Report",
        "",
        f"Generated at UTC: `{now()}`",
        f"Indexer run: `{s.get('run_id')}`",
        "",
        "## Status",
        "",
        f"- Recovery status: `{s.get('recovery_status')}`",
        f"- Method: {s.get('data_recovery_method')}",
        "- Classification: source-backed partial archive, not a complete official accounting yet.",
        "",
        "## Counts Recovered",
        "",
        f"- PolygonScan auction-house transactions found: `{s.get('polygonscan_auction_transactions_found')}`",
        f"- Receipts fetched: `{s.get('receipts_fetched')}`",
        f"- Raw relevant logs stored: `{s.get('raw_relevant_logs')}`",
        f"- AuctionCreated rows: `{s['auction_created']['count']}` (Dog IDs `{s['auction_created']['min_dog_id']}` to `{s['auction_created']['max_dog_id']}`)",
        f"- AuctionBid rows: `{s['auction_bids']['count']}` (Dog IDs `{s['auction_bids']['min_dog_id']}` to `{s['auction_bids']['max_dog_id']}`)",
        f"- Per-Dog bid summary rows: `{s.get('dog_bid_coverage', {}).get('per_dog_rows')}` (one row for every minted Mission 1 Dog token ID 0-200)",
        f"- Bid coverage statuses: `{s.get('dog_bid_coverage', {}).get('bid_coverage_status_counts')}`",
        f"- Auction Dogs with explicit zero recovered bids: `{s.get('dog_bid_coverage', {}).get('auction_dogs_without_recovered_bids_count')}`",
        f"- AuctionSettled rows: `{s['auction_settled']['count']}` (Dog IDs `{s['auction_settled']['min_dog_id']}` to `{s['auction_settled']['max_dog_id']}`)",
        f"- Latest auction state from `auction()` eth_call: Dog `{s.get('latest_auction_state', {}).get('dog_id')}`, amount `{s.get('latest_auction_state', {}).get('amount_display_weth')}` WETH, settled `{s.get('latest_auction_state', {}).get('settled')}`.",
        f"- NFT mint-transfer distinct token IDs: `{s['nft_mint_transfers']['count_distinct_token_ids']}` (range `{s['nft_mint_transfers']['min_token_id']}` to `{s['nft_mint_transfers']['max_token_id']}`)",
        f"- BSCT transfer rows: `{s.get('bsct_transfer_rows')}`",
        "",
        "## 201 Polygon Dogs Claim",
        "",
        f"- Status: `{s['polygon_dogs_claim_201_reconciliation']['status']}`",
        f"- Onchain Dog `totalSupply()` observed during discovery: `{s.get('dog_total_supply_verified_from_onchain_call')}`",
        f"- Receipt archive max created Dog ID: `{s['polygon_dogs_claim_201_reconciliation']['receipt_recovered_auction_created_max_id']}`",
        f"- DogMaster reward IDs excluded from auction coverage: `{', '.join(map(str, s.get('dogmaster_reward_rule', {}).get('recovered_reward_ids', [])))}`",
        f"- Notes: {s['polygon_dogs_claim_201_reconciliation']['notes']}",
        "",
        "## Dog #1 / Ukraine Dog",
        "",
        f"- Status: `{s['dog1_ukraine_auction']['status']}`",
        f"- Created tx: `{s['dog1_ukraine_auction']['created_tx']}`",
        f"- Settled tx: `{s['dog1_ukraine_auction']['settled_tx']}`",
        f"- Source note: {s['dog1_ukraine_auction']['source_note']}",
        "",
        "## Dune",
        "",
        f"- Dune status: `{s['dune']['status']}`",
        "",
        "## Known Gaps / Mismatches",
        "",
    ]
    gaps = s.get("known_gaps", [])
    if not gaps:
        lines.append("- No gaps recorded.")
    else:
        for gap in gaps[:40]:
            lines.append(f"- `{gap['gap_id']}` ({gap['severity']}): {gap['reason']}")
        if len(gaps) > 40:
            lines.append(f"- ... {len(gaps) - 40} additional gaps in `mission1_index_gaps.csv`.")
    lines.extend([
        "",
        "## Interpretation",
        "",
        "The recovered archive verifies core Mission 1 Polygon contracts and captures a substantial receipt-backed auction/log dataset. It should not yet be treated as a final official Mission 1 accounting because free public RPC endpoints pruned old `eth_getLogs` history and Dune/PolygonScan API access was unavailable. Future reconciliation should use Dune exports, Etherscan V2/PolygonScan API with a key, or an archive Polygon RPC to compare every expected Dog ID and flow.",
    ])
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n")

    NOTES_PATH.write_text("\n".join([
        "# Mission 1 Verification Notes",
        "",
        f"Generated at UTC: `{now()}`",
        "",
        "Verified means backed by at least one public source plus onchain/PolygonScan evidence. Candidate means source-backed but not yet reconciled end-to-end. Unknown means intentionally left blank.",
        "",
        "## Verified in this pass",
        "",
        "- Polygon PoS chain ID `137`.",
        "- Mission 1 Dog NFT, auction house, BSCT, governance, treasury, donation, WETH, Idle WETH, and Superfluid-related addresses from docs/GitHub plus PolygonScan verified pages/onchain calls.",
        "- Dog NFT `name() = Degen Dogs`, `symbol() = DOG`, `totalSupply() = 201` from Polygon RPC calls during discovery.",
        "- BSCT `name() = Dog Biscuits`, `symbol() = BSCT`, `decimals() = 18` from Polygon RPC calls during discovery.",
        "- Dog #1 Ukraine auction claim from the Degen Dogs Medium article plus receipt-backed AuctionCreated/AuctionSettled rows for Dog #1.",
        "- DogMaster reward mint rule from `Dog.sol`: token ID `0` and every `11th` Dog were minted to the dogMaster, explaining why `totalSupply() = 201` while auction rows skip those IDs.",
        "- `mission1_dog_bid_summary` has one row for every minted Mission 1 Dog token ID `0-200`; `mission1_dog_search_index.json` embeds the recovered per-Dog `bid_history` arrays, with explicit empty bid histories for DogMaster reward mints and no-bid auctions.",
        "",
        "## Not fully verified yet",
        "",
        "- Complete treasury, Idle, Superfluid stream, and Unchain Ukraine transfer accounting.",
        "- Dog #200 final state beyond latest `auction()` result; archive currently observes Dog 200 created with amount 0 and unsettled at latest block.",
        "- Complete Dune dashboard/query recovery, because public Dune UI returned HTTP 403 and no `DUNE_API_KEY` was available.",
    ]) + "\n")
    print(f"wrote {REPORT_PATH.relative_to(ROOT)} and {NOTES_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
