#!/usr/bin/env python3
"""Refresh the dashboard's current auction surface without a full history scan.

The full builder is intentionally comprehensive, but a fresh checkout has no local
RPC log cache and can spend many minutes scanning historical Base blocks. This
fast path uses the committed dashboard tables as its baseline, re-fetches only a
small overlap before the last generated block, and updates the current auction,
feed, latest-bid, search, metrics, and rendered HTML files.
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"


def read_json(name: str) -> Any:
    return json.loads((GENERATED / f"{name}.json").read_text(encoding="utf-8"))


def write_json(name: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    for directory in (GENERATED, PUBLIC_GENERATED):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(payload, encoding="utf-8")


def read_table(name: str) -> tuple[list[str], list[dict[str, Any]]]:
    path = GENERATED / f"{name}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return list(rows[0].keys()) if rows else [], rows


def write_table(name: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
    for directory in (GENERATED, PUBLIC_GENERATED):
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    write_json(name, rows)


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or "").replace(",", ""))
    except (TypeError, ValueError):
        return default


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip().replace(" ", "T")
    if not raw:
        return None
    if not raw.endswith("Z"):
        raw += "Z"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def format_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def short_wallet(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if wallet else ""


def profile_map() -> dict[str, tuple[str, str]]:
    found: dict[str, tuple[str, str]] = {}
    for path in GENERATED.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        values = data if isinstance(data, list) else [data]
        for row in values:
            if not isinstance(row, dict):
                continue
            for wallet_key, name_key, url_key in (
                ("bidder_wallet", "bidder", "bidder_url"),
                ("winner_wallet", "winner", "winner_url"),
                ("winner_wallet", "winner_display", "winner_url"),
            ):
                wallet = str(row.get(wallet_key) or "").lower()
                name = str(row.get(name_key) or "")
                if wallet and name and wallet not in found:
                    found[wallet] = (name, str(row.get(url_key) or ""))
    return found


def traits_from_text(text: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for item in str(text or "").split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key.strip() and value.strip():
            attrs[key.strip()] = value.strip()
    return attrs


def build_rarity(history: list[dict[str, Any]], new_attrs: dict[str, str], total_supply: int) -> tuple[str, float, str, str]:
    all_attrs: dict[int, dict[str, str]] = {}
    for row in history:
        token_id = int_value(row.get("token_id"), -1)
        if token_id >= 0:
            all_attrs[token_id] = traits_from_text(str(row.get("traits") or ""))
    new_id = max(all_attrs, default=-1) + 1
    all_attrs[new_id] = new_attrs
    counts: Counter[tuple[str, str]] = Counter()
    for attrs in all_attrs.values():
        counts.update(attrs.items())
    scores: dict[int, float] = {}
    for token_id, attrs in all_attrs.items():
        scores[token_id] = sum(total_supply / max(1, counts[(key, value)]) for key, value in attrs.items())
    rank = 1 + sorted(scores, key=lambda token: (-scores[token], token)).index(new_id)
    traits = "; ".join(f"{key}: {value}" for key, value in new_attrs.items())
    rarity_items = "; ".join(
        f"{key}: {value} ({counts[(key, value)] * 100 / total_supply:.1f}%)"
        for key, value in new_attrs.items()
    )
    return f"#{rank}/{total_supply}", round(scores[new_id], 6), traits, rarity_items


def display_for(wallet: str, profiles: dict[str, tuple[str, str]]) -> tuple[str, str]:
    return profiles.get(wallet.lower(), (f"{short_wallet(wallet)}", f"https://basescan.org/address/{wallet}"))


def update_readme_snapshot(current_row: dict[str, Any], metrics: dict[str, str]) -> None:
    path = ROOT / "README.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    payback = metrics.get("reward_current_bid_payback_days", "N/A")
    payback_display = "N/A"
    try:
        payback_value = Decimal(payback)
        if payback_value > 0:
            places = 1 if payback_value < 10 else 0
            payback_display = f"≈{payback_value:,.{places}f} days"
    except (TypeError, ValueError):
        pass
    replacements = {
        "Snapshot block": str(current_row.get("latest_block", "")),
        "Snapshot time UTC": str(current_row.get("latest_block_time_utc", "")),
        "Current Dog": f"Dog #{current_row.get('token_id', '')}",
        "Current status": str(current_row.get("auction_state", "")),
        "Current bid": f"{current_row.get('current_bid_eth', '')} ETH (${Decimal(str(current_row.get('current_bid_usd', '0'))):,.2f})",
        "Current high bidder": str(current_row.get("bidder", "")),
        "Bid payback / APR": f"{payback_display} / {metrics.get('reward_current_bid_apr_display', 'N/A')}",
    }
    for label, value in replacements.items():
        pattern = rf"^\| {re.escape(label)} \|.*$"
        text = re.sub(pattern, f"| {label} | {value} |", text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")

def main() -> None:
    current_rows = read_json("current_auction")
    if not isinstance(current_rows, list) or not current_rows:
        raise RuntimeError("generated/current_auction.json has no baseline row")
    previous_block = int_value(current_rows[0].get("latest_block"))
    if previous_block <= 0:
        raise RuntimeError("current auction baseline has no latest_block")
    overlap = max(50, int(os.environ.get("MISSION3_CURRENT_SURFACE_OVERLAP", "1000")))
    from_block = max(0, previous_block - overlap)

    # Set conservative defaults before importing the full builder module. The
    # fast path needs only a few recent auction ranges and should not overload a
    # public RPC endpoint with the full builder's default worker count.
    os.environ.setdefault("BASE_FROM_BLOCK", str(from_block))
    os.environ.setdefault("BASE_LOG_WORKERS", "1")
    os.environ.setdefault("BASE_RPC_ATTEMPTS", "2")
    os.environ.setdefault("BASE_LOG_RPC_TIMEOUT", "20")
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_dashboard as builder

    latest_block = int(builder.rpc("eth_blockNumber", []), 16)
    latest_block_data = builder.rpc("eth_getBlockByNumber", [hex(latest_block), False])
    latest_time = builder.utc_from_unix(int(latest_block_data["timestamp"], 16))
    current = builder.fetch_current_auction(latest_block, latest_time, hex(latest_block))
    token_id = int(current["token_id"])
    total_supply = builder.fetch_dog_total_supply(hex(latest_block))
    metadata = builder.fetch_one_dog_metadata(token_id, hex(latest_block))

    logs = builder.fetch_logs(
        builder.AUCTION_HOUSE,
        [builder.TOPIC_AUCTION_CREATED, builder.TOPIC_AUCTION_BID, builder.TOPIC_AUCTION_SETTLED],
        from_block,
        latest_block,
    )
    created_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_CREATED]
    bid_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_BID]
    settled_logs = [row for row in logs if row["topics"][0].lower() == builder.TOPIC_AUCTION_SETTLED]
    created, bids, settled = builder.decode_auction_logs(created_logs, bid_logs, settled_logs)
    current_bids = [row for row in bids if int(row["token_id"]) == token_id]
    current_bids.sort(key=lambda row: (int(row["block_number"]), int(row["log_index"])))

    eth_usd, eth_source = builder.fetch_eth_usd_price()
    amount_eth = Decimal(str(current["amount_eth"]))
    amount_usd = (amount_eth * eth_usd).quantize(Decimal("0.01"))
    wallet = str(current.get("bidder") or "").lower()
    profiles = profile_map()
    bidder, bidder_url = display_for(wallet, profiles)
    end_dt = parse_utc(current.get("end_time_utc"))
    remaining = max(0, int((end_dt - now_utc()).total_seconds())) if end_dt else 0
    state = "live" if not int(current.get("settled") or 0) and remaining > 0 else "settled"
    bid_text = f"{amount_eth:.5f} ETH (${amount_usd:,.0f})"
    previous_latest = read_json("current_latest_bid")[0]
    bid_time = current_bids[-1].get("block_time_utc") if current_bids else previous_latest.get("bid_time_utc") or latest_time
    dog_attrs = {
        str(item.get("trait_type")): str(item.get("value"))
        for item in metadata.get("attributes", [])
        if item.get("trait_type") and item.get("value")
    }
    history = read_json("historical_dog_search")
    rarity, rarity_score, traits, trait_rarity = build_rarity(history, dog_attrs, total_supply)

    current_row = dict(current_rows[0])
    current_row.update(
        {
            "token_id": token_id,
            "dog_name": metadata.get("name") or f"Degen Dog #{token_id}",
            "dog_image_url": metadata.get("image_url") or f"https://api.degendogs.club/images/{token_id}.png",
            "dog_external_url": metadata.get("external_url") or f"https://degendogs.club/#dog{token_id}",
            "dog_opensea_url": builder.dog_opensea_url(token_id),
            "traits": traits,
            "trait_rarity": trait_rarity,
            "rarity": rarity,
            "rarity_score": rarity_score,
            "current_bid": bid_text,
            "current_bid_eth": float(amount_eth),
            "current_bid_usd": float(amount_usd),
            "current_bid_usd_live": float(amount_usd),
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "bidder": bidder,
            "bidder_url": bidder_url,
            "bidder_wallet": wallet,
            "start_time_utc": current.get("start_time_utc", ""),
            "end_time_utc": current.get("end_time_utc", ""),
            "auction_state": state,
            "seconds_remaining": remaining,
            "time_remaining": format_seconds(remaining) if state == "live" else "settled",
            "settled": int(current.get("settled") or 0),
            "latest_block": latest_block,
            "latest_block_time_utc": latest_time,
        }
    )
    current_columns, _ = read_table("current_auction")
    write_table("current_auction", current_columns, [current_row])

    latest_row = dict(read_json("current_latest_bid")[0])
    latest_row.update(
        {
            "dog": f"Dog #{token_id}",
            "dog_image_url": current_row["dog_image_url"],
            "dog_external_url": current_row["dog_external_url"],
            "dog_opensea_url": current_row["dog_opensea_url"],
            "latest_bid": bid_text,
            "latest_bid_eth": float(amount_eth),
            "latest_bid_usd": float(amount_usd),
            "latest_bid_usd_live": float(amount_usd),
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "bidder": bidder,
            "bidder_url": bidder_url,
            "bidder_wallet": wallet,
            "bid_time_utc": bid_time,
            "auction_state": state,
            "time_remaining": current_row["time_remaining"],
            "auction_end_utc": current.get("end_time_utc", ""),
            "traits": traits,
            "trait_rarity": trait_rarity,
            "rarity": rarity,
        }
    )
    latest_columns, _ = read_table("current_latest_bid")
    write_table("current_latest_bid", latest_columns, [latest_row])

    history_columns, history_rows = read_table("current_auction_bid_history")
    new_history: list[dict[str, Any]] = []
    for bid in current_bids:
        bid_wallet = str(bid.get("bidder") or "").lower()
        bid_name, bid_url = display_for(bid_wallet, profiles)
        bid_eth = Decimal(str(bid.get("bid_eth") or 0))
        bid_usd = (bid_eth * eth_usd).quantize(Decimal("0.01"))
        new_history.append(
            {
                "bid_time_utc": bid.get("block_time_utc", ""),
                "token_id": token_id,
                "dog": f"Dog #{token_id}",
                "bidder": bid_name,
                "bidder_url": bid_url,
                "bidder_wallet": bid_wallet,
                "bid": f"{bid_eth:.5f} ETH (${bid_usd:,.0f})",
                "bid_eth": float(bid_eth),
                "bid_usd": float(bid_usd),
                "eth_usd_price_live": str(eth_usd),
                "eth_usd_price_date_utc": latest_time[:10],
                "usd_estimate_source": "current_eth_usd_price",
                "usd_estimate_source_detail": eth_source,
                "usd_estimate_confidence": "live_current",
                "usd_estimate_basis": "current_auction_bid_history_live_eth_usd",
                "extended": int(bid.get("extended") or 0),
                "block_number": int(bid.get("block_number") or 0),
                "log_index": int(bid.get("log_index") or 0),
                "tx_hash": bid.get("tx_hash", ""),
            }
        )
    if not new_history:
        new_history = [dict(row) for row in history_rows]
        if new_history:
            high = max(new_history, key=lambda row: (int_value(row.get("block_number")), int_value(row.get("log_index"))))
            high.update(
                {
                    "bid_time_utc": bid_time,
                    "bidder": bidder,
                    "bidder_url": bidder_url,
                    "bidder_wallet": wallet,
                    "bid": bid_text,
                    "bid_eth": float(amount_eth),
                    "bid_usd": float(amount_usd),
                    "eth_usd_price_live": str(eth_usd),
                    "eth_usd_price_date_utc": latest_time[:10],
                    "usd_estimate_source": "current_eth_usd_price",
                    "usd_estimate_source_detail": eth_source,
                    "usd_estimate_confidence": "live_current",
                }
            )
    write_table("current_auction_bid_history", history_columns, new_history)

    feed_columns, feed_rows = read_table("auction_feed")
    old_current = dict(feed_rows[0]) if feed_rows else {}
    previous_dog = int(re.sub(r"\D", "", str(old_current.get("dog") or "-1")) or -1)
    settled_previous = None
    if previous_dog != token_id:
        settled_previous = dict(old_current)
        settled_previous["status"] = "settled"
        settled_previous["settled_time_utc"] = current.get("start_time_utc", latest_time)
        settled_previous["time_remaining"] = "settled"
    new_feed = dict(old_current)
    new_feed.update(
        {
            "status": "ongoing" if state == "live" else "settled",
            "dog": f"Dog #{token_id}",
            "dog_image_url": current_row["dog_image_url"],
            "dog_external_url": current_row["dog_external_url"],
            "dog_opensea_url": current_row["dog_opensea_url"],
            "bidder_winner": bidder,
            "bidder_winner_url": bidder_url,
            "bidder_winner_wallet": wallet,
            "bid": bid_text,
            "amount_eth": float(amount_eth),
            "amount_usd": float(amount_usd),
            "eth_usd_price_live": str(eth_usd),
            "eth_usd_price_date_utc": latest_time[:10],
            "usd_estimate_source": "current_eth_usd_price",
            "usd_estimate_source_detail": eth_source,
            "usd_estimate_confidence": "live_current",
            "usd_estimate_basis": "current_eth_usd_price",
            "auction_time_utc": bid_time,
            "last_bid_utc": bid_time,
            "auction_end_utc": current.get("end_time_utc", ""),
            "settled_time_utc": "",
            "time_remaining": current_row["time_remaining"],
            "rarity": rarity,
            "traits": traits,
            "trait_rarity": trait_rarity,
        }
    )
    remaining_feed = [row for row in feed_rows[1:] if str(row.get("dog")) != f"Dog #{token_id}"]
    output_feed = [new_feed]
    if settled_previous is not None:
        output_feed.append(settled_previous)
    output_feed.extend(remaining_feed)
    write_table("auction_feed", feed_columns, output_feed)

    # Add the newly minted/auctioned Dog to the unified searchable table.
    history_columns, history_rows = read_table("historical_dog_search")
    history_rows = [row for row in history_rows if int_value(row.get("token_id"), -1) != token_id]
    template = dict(history_rows[-1] if history_rows else {})
    new_search = dict(template)
    new_search.update(
        {
            "mission": 3,
            "chain": "Base",
            "chain_id": 8453,
            "token_id": token_id,
            "dog": f"Dog #{token_id}",
            "dog_image_url": current_row["dog_image_url"],
            "dog_external_url": current_row["dog_external_url"],
            "dog_opensea_url": current_row["dog_opensea_url"],
            "status": "ongoing" if state == "live" else "settled",
            "winner": bidder,
            "winner_url": bidder_url,
            "winner_wallet": wallet,
            "amount": bid_text,
            "amount_raw": str(current.get("amount_wei", "")),
            "bid_count": len(current_bids),
            "unique_bidder_count": len({str(row.get("bidder")).lower() for row in current_bids}),
            "auction_created_time_utc": current.get("start_time_utc", ""),
            "settled_time_utc": "",
            "rarity": rarity,
            "traits": traits,
            "trait_rarity": trait_rarity,
            "confidence": "live_base_contract_call",
            "sources": "base_rpc,dog_metadata_api",
            "search_text": f"3 Base 8453 {token_id} Dog #{token_id} {traits} {bidder} {wallet}".strip(),
        }
    )
    write_table("historical_dog_search", history_columns, [*history_rows, new_search])

    report_columns, report_rows = read_table("historical_dog_report")
    report_groups = {"all": history_rows + [new_search]}
    report_groups.update({mission: [row for row in report_groups["all"] if str(row.get("mission")) == mission] for mission in ("1", "2", "3")})
    for report in report_rows:
        group = report_groups.get(str(report.get("mission")))
        if group is None:
            continue
        statuses = [str(row.get("status") or "").lower() for row in group]
        report["dogs"] = len(group)
        report["settled"] = sum(1 for status in statuses if status == "settled" or (status.startswith("settled") and "unsettled" not in status))
        report["live_or_unsettled"] = sum(1 for status in statuses if "live" in status or "ongoing" in status or "unsettled" in status or "pending settlement" in status or "created" in status)
        report["metadata_only"] = sum(1 for status in statuses if status == "metadata_only")
        report["bid_count"] = sum(int_value(row.get("bid_count")) for row in group)
        if str(report.get("mission")) == "all":
            report["latest_activity_utc"] = latest_time
        elif str(report.get("mission")) == "3":
            report["latest_activity_utc"] = bid_time
    write_table("historical_dog_report", report_columns, report_rows)

    metrics_rows = read_json("mission3_metrics")
    unified_paths = [ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json", PUBLIC_GENERATED / "unified_dog_search_index.json"]
    unified_rows = json.loads(unified_paths[0].read_text(encoding="utf-8")) if unified_paths[0].exists() else []
    unified_rows = [row for row in unified_rows if not (int_value(row.get("mission"), -1) == 3 and int_value(row.get("dog_id"), -1) == token_id)]
    for row in unified_rows:
        if int_value(row.get("mission"), -1) == 3 and (str(row.get("status", "")).lower() == "live" or "ongoing" in str(row.get("status", "")).lower()):
            row["status"] = "settled"
            row["settlement"] = {"block_number": None, "block_time_utc": str(current.get("start_time_utc", latest_time)).replace(" ", "T") + "Z", "settled": True, "tx_hash": None, "tx_url": None}
    unified_template = next((row for row in unified_rows if int_value(row.get("mission"), -1) == 3), {})
    trait_items = []
    for item in trait_rarity.split("; "):
        match = re.match(r"^([^:]+): (.+?) (\\([^)]+%\\))$", item)
        if match:
            trait_items.append({"display": item, "trait_type": match.group(1), "value": match.group(2)})
    tx_hashes = [str(row.get("tx_hash")) for row in current_bids if row.get("tx_hash")]
    if not tx_hashes:
        tx_hashes = [str(row.get("tx_hash")) for row in history_rows if int_value(row.get("token_id"), -1) == token_id and row.get("tx_hash")]
    unified_row = json.loads(json.dumps(unified_template))
    unified_row.update(
        {
            "activity_time_basis": "last_bid_block_time",
            "activity_time_utc": str(bid_time).replace(" ", "T") + "Z",
            "amount": {"native": str(amount_eth), "native_symbol": "ETH", "price_asset_key": "ETH", "raw": str(current.get("amount_wei", "")), "usd_estimate": f"{amount_usd:.8f}", "usd_estimate_confidence": "live_current", "usd_estimate_display": f"${amount_usd:.2f}", "usd_estimate_source": "current_eth_usd_price", "usd_estimate_time_basis": "last_bid_block_time"},
            "auction_created": {"block_number": None, "block_time_utc": str(current.get("start_time_utc", "")).replace(" ", "T") + "Z", "tx_hash": None, "tx_url": None},
            "bid_stats": {"bid_count": len(current_bids) or 1, "last_bid_time_utc": str(bid_time).replace(" ", "T") + "Z", "unique_bidder_count": len({str(row.get("bidder_wallet") or row.get("bidder")).lower() for row in history_rows if int_value(row.get("token_id"), -1) == token_id}) or 1},
            "bid_tx_hashes": tx_hashes,
            "chain": "Base",
            "chain_id": 8453,
            "dog_id": token_id,
            "dog_image_url": current_row["dog_image_url"],
            "dog_item_url": current_row["dog_opensea_url"],
            "era_label": "Mission 3",
            "links": {"auction_tx": None, "dog_page": current_row["dog_external_url"], "explorer": f"https://basescan.org/address/{wallet}", "item": current_row["dog_opensea_url"], "repo_archive": f"archive/dogs/by-id/{token_id:03d}.json", "settlement_tx": None},
            "mission": 3,
            "rarity": {"display": rarity, "rank": int(rarity.split("/")[0].lstrip("#")), "total": total_supply},
            "search_text": f"dog {token_id} dog #{token_id} {token_id} mission 3 mission 3 base ongoing {wallet} {bidder} {amount_eth} eth {amount_usd} ${amount_usd:.2f} {bid_time} {rarity} {traits} {bidder}",
            "status": "ongoing" if state == "live" else "settled",
            "traits": trait_items,
            "winner_or_high_bidder": {"display": bidder, "farcaster_fid": None, "farcaster_handle": bidder.lstrip("@"), "profile_url": bidder_url, "wallet": wallet, "wallet_explorer_url": f"https://basescan.org/address/{wallet}"},
            "settlement": {"block_number": None, "block_time_utc": None, "settled": False, "tx_hash": None, "tx_url": None},
        }
    )
    unified_rows.append(unified_row)
    unified_rows.sort(key=lambda row: (1 if str(row.get("status", "")).lower() == "live" or "ongoing" in str(row.get("status", "")).lower() else 0, str(row.get("activity_time_utc", "")), int_value(row.get("dog_id"), -1)), reverse=True)
    unified_payload = json.dumps(unified_rows, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    for unified_path in unified_paths:
        unified_path.parent.mkdir(parents=True, exist_ok=True)
        unified_path.write_text(unified_payload, encoding="utf-8")

    metrics_rows = read_json("mission3_metrics")
    metrics = {str(row.get("metric")): str(row.get("value", "")) for row in metrics_rows}
    metrics["eth_usd_price"] = str(eth_usd)
    metrics.update(builder.current_bid_reward_stats(current, metrics))
    metrics.update(
        {
            "current_auction_token_id": str(token_id),
            "dog_total_supply": str(total_supply),
            "current_auction_status": state,
            "current_auction_end_utc": str(current.get("end_time_utc", "")),
            "current_bid_eth": str(current.get("amount_eth", "")),
            "current_bid_usd": str(amount_usd),
            "current_bidder": bidder,
            "current_bidder_wallet": wallet,
            "latest_block": str(latest_block),
            "latest_block_time_utc": latest_time,
        }
    )
    metric_rows = [{"metric": key, "value": value} for key, value in metrics.items()]
    metric_columns, _ = read_table("mission3_metrics")
    write_table("mission3_metrics", metric_columns or ["metric", "value"], metric_rows)
    update_readme_snapshot(current_row, metrics)
    refresh_status = read_json("refresh_status")
    refresh_status.update(
        {
            "last_refresh_result": "success_generated",
            "last_successful_refresh_time_utc": now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "latest_generated_block": latest_block,
            "latest_generated_block_time_utc": latest_time,
            "current_dog_token_id": token_id,
            "current_auction_end_time_utc": current.get("end_time_utc", ""),
            "current_auction_status": state,
            "current_bid_eth": str(current.get("amount_eth", "")),
            "current_high_bidder": bidder,
            "current_high_bidder_wallet": wallet,
            "refresh_reason": "current_surface_incremental",
        }
    )
    write_json("refresh_status", refresh_status)

    manifest_rows = []
    for name in builder.OUTPUT_TABLES:
        csv_path = GENERATED / f"{name}.csv"
        with csv_path.open(newline="", encoding="utf-8") as handle:
            row_count = max(0, sum(1 for _ in handle) - 1)
        manifest_rows.append({"table": name, "file": f"generated/{name}.csv", "rows": row_count})
    for directory in (GENERATED, PUBLIC_GENERATED):
        manifest_csv = directory / "manifest.csv"
        with manifest_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["table", "file", "rows"], lineterminator="\n")
            writer.writeheader()
            writer.writerows(manifest_rows)
    write_json("manifest", manifest_rows)

    tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    for name in builder.OUTPUT_TABLES:
        columns, rows = read_table(name)
        tables[name] = (columns, [tuple(row.get(column, "") for column in columns) for row in rows])
    builder.write_html(tables)
    print(json.dumps({"status": "success_current_surface", "token_id": token_id, "latest_block": latest_block, "new_logs": len(logs), "new_bids": len(current_bids), "current_bid_eth": str(amount_eth)}, sort_keys=True))


if __name__ == "__main__":
    main()
