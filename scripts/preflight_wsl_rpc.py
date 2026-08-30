#!/usr/bin/env python3
"""Read-only production-shaped RPC capability probe for WSL activation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_dashboard as builder  # noqa: E402
import refresh_current_surface as current_surface  # noqa: E402


def validate_log_rows(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        raise RuntimeError("Dog rarity-continuity eth_getLogs quorum returned a non-list")
    validated: list[dict[str, Any]] = []
    allowed_topics = set(current_surface.RARITY_MUTATION_TOPICS)
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Dog rarity-continuity eth_getLogs returned a non-object row")
        if str(row.get("address") or "").lower() != builder.DEGEN_DOGS.lower():
            raise RuntimeError("Dog rarity-continuity eth_getLogs returned the wrong contract")
        topics = row.get("topics")
        if not isinstance(topics, list) or not topics or str(topics[0]).lower() not in allowed_topics:
            raise RuntimeError("Dog rarity-continuity eth_getLogs returned an unexpected topic")
        validated.append(row)
    return validated


def main() -> int:
    snapshot_block, block_data, verification = builder.verified_snapshot()
    if not builder.VERIFIED_LOG_URLS:
        raise RuntimeError("verified snapshot did not establish a production log quorum")

    # Exercise the exact publish-critical JSON-RPC batch before activation:
    # cross-provider tokenURI()+exists() pairs, an EIP-1898 blockHash state
    # tag, and exact ERC721NonexistentToken revert-data classification. The
    # live auction Dog must exist while totalSupply (the next ID) must not.
    snapshot_tag = hex(snapshot_block)
    expected_hash = str(block_data.get("hash") or "").lower()
    total_supply = builder.fetch_dog_total_supply(snapshot_tag)
    latest_time = builder.utc_from_unix(int(str(block_data.get("timestamp") or ""), 16))
    current_auction = builder.fetch_current_auction(snapshot_block, latest_time, snapshot_tag)
    current_token = int(current_auction.get("token_id", -1))
    if total_supply <= 0 or current_token < 0 or current_token >= total_supply:
        raise RuntimeError(
            "Dog totalSupply/current-auction invariant failed at the verified snapshot"
        )
    token_uri_bindings = builder.fetch_token_uri_bindings(
        [current_token, total_supply],
        snapshot_tag,
        block_hash=expected_hash,
    )
    if token_uri_bindings.get(current_token) is None:
        raise RuntimeError("current auction Dog has no verified tokenURI binding")
    if token_uri_bindings.get(total_supply) is not None:
        raise RuntimeError("Dog totalSupply next ID unexpectedly has a tokenURI binding")

    # Use the same four-topic OR filter as the bounded publisher. This catches
    # providers that accept a one-topic AuctionCreated probe but reject the NFT
    # rarity-continuity filter with JSON-RPC -32602.
    span = max(1, min(5, int(builder.LOG_QUORUM_MAX_BLOCKS)))
    start_block = max(builder.FROM_BLOCK, snapshot_block - span + 1)
    rows, agreeing_urls = builder.rpc_quorum(
        "eth_getLogs",
        [
            builder.log_filter(
                builder.DEGEN_DOGS,
                list(current_surface.RARITY_MUTATION_TOPICS),
                start_block,
                snapshot_block,
            )
        ],
        urls=builder.VERIFIED_LOG_URLS,
        min_agreement=builder.RPC_QUORUM_SIZE,
        timeout=builder.LOG_RPC_TIMEOUT,
    )
    validated = validate_log_rows(rows)
    builder.verify_snapshot_unchanged(snapshot_block, expected_hash)

    # Provider URLs and credentials never appear in output. Operator labels are
    # the same public-safe values already published in refresh_status.json.
    report = {
        "kind": "degen_dogs_wsl_rpc_preflight",
        "status": "healthy",
        "snapshot_block": snapshot_block,
        "snapshot_block_hash": expected_hash,
        "dog_total_supply_count": total_supply,
        "token_uri_binding_probe_count": len(token_uri_bindings),
        "token_uri_binding_probe_status": "current_present_next_nonexistent",
        "rarity_filter_from_block": start_block,
        "rarity_filter_to_block": snapshot_block,
        "rarity_filter_log_count": len(validated),
        "rpc_quorum_size": builder.RPC_QUORUM_SIZE,
        "rpc_quorum_agreement": len(agreeing_urls),
        "rpc_quorum_providers": verification.get("rpc_quorum_providers", ""),
        "log_rpc_quorum_providers": verification.get("log_rpc_quorum_providers", ""),
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
