#!/usr/bin/env python3
"""Build the static cross-mission Degen Dogs auction search index.

The script is intentionally offline: it only reads generated/local archive files and
writes static JSON artifacts for the dashboard and repo archive.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_GENERATED = ROOT / "archive" / "data" / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"
DOG_ARCHIVE = ROOT / "archive" / "dogs"
IDENTITY_PATH = ROOT / "archive" / "data" / "identity" / "wallet_profiles.json"

MISSION_INDEXES = {
    1: ROOT / "archive" / "mission1" / "data" / "generated" / "mission1_dog_search_index.json",
    2: ROOT / "archive" / "mission2" / "data" / "generated" / "mission2_dog_search_index.json",
    3: ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_dog_search_index.json",
}
MISSION_BID_FILES = {
    2: ROOT / "archive" / "mission2" / "data" / "generated" / "mission2_auction_bids.json",
    3: ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_auction_bids.json",
}
HISTORICAL_SEARCH = ROOT / "generated" / "historical_dog_search.json"
AUCTION_FEED = ROOT / "generated" / "auction_feed.json"
CURRENT_AUCTION = ROOT / "generated" / "current_auction.json"
RECENT_BIDS = ROOT / "generated" / "recent_bids.json"

MISSION_CONFIG = {
    1: {
        "era_label": "Mission 1",
        "chain": "Polygon",
        "chain_id": 137,
        "currency": "WETH",
        "price_asset_key": "ETH",
        "address_explorer": "https://polygonscan.com/address/{address}",
        "tx_explorer": "https://polygonscan.com/tx/{tx}",
        "source_note": "Mission 1 bid currency is verified as Polygon WETH; historical USD uses ETH/WETH pricing.",
    },
    2: {
        "era_label": "Mission 2",
        "chain": "Degen Chain",
        "chain_id": 666666666,
        "currency": "DEGEN",
        "price_asset_key": "DEGEN",
        "address_explorer": "https://explorer.degen.tips/address/{address}",
        "tx_explorer": "https://explorer.degen.tips/tx/{tx}",
        "source_note": "Mission 2 contract uses verified 18-decimal WDEGEN/DEGEN auction amounts.",
    },
    3: {
        "era_label": "Mission 3",
        "chain": "Base",
        "chain_id": 8453,
        "currency": "ETH",
        "price_asset_key": "ETH",
        "address_explorer": "https://basescan.org/address/{address}",
        "tx_explorer": "https://basescan.org/tx/{tx}",
        "source_note": "Mission 3 live/current source of truth is Base auction logs and current auction contract state.",
    },
}
ZERO = "0x0000000000000000000000000000000000000000"
BASE_DOG_CONTRACT = "0x09154248ffdbaf8aa877ae8a4bf8ce1503596428"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalize_address(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().lower()
    return text if text.startswith("0x") and len(text) == 42 else ""


def short_address(address: str) -> str:
    address = normalize_address(address)
    if not address:
        return ""
    return f"{address[:6]}…{address[-4:]}"


def int_value(value: Any, default: Any = 0) -> Any:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def first_text(*values: Any) -> str:
    for value in values:
        text = text_value(value)
        if text:
            return text
    return ""


def decimal_value(value: Any) -> Decimal | None:
    text = text_value(value).replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def dog_id_from_row(row: dict[str, Any]) -> int:
    for key in ("dog_id", "token_id"):
        dog_id = int_value(row.get(key), -1)
        if dog_id >= 0:
            return dog_id
    text = first_text(row.get("dog"), row.get("dog_name"))
    parts = "".join(ch if ch.isdigit() else " " for ch in text).split()
    return int_value(parts[-1], -1) if parts else -1


def iso_utc(value: Any) -> str:
    text = first_text(value)
    if not text:
        return ""
    text = text.replace(" ", "T")
    return text if text.endswith("Z") else f"{text}Z"


def eth_to_wei(value: Any) -> str:
    amount = decimal_value(value)
    if amount is None:
        return ""
    return str(int(amount * (Decimal(10) ** 18)))


def usd_display(value: Any) -> str:
    amount = decimal_value(value)
    if amount is None:
        return ""
    return f"${amount.quantize(Decimal('0.01')):,.2f}"


def parse_historical_amount_display(value: Any, fallback_symbol: str = "ETH") -> tuple[str, str, str]:
    """Parse historical_dog_search amount strings like `0.01210 ETH ($24.44)`."""
    text = first_text(value)
    native = ""
    symbol = fallback_symbol
    usd = ""
    native_match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*([A-Za-z]+)", text)
    if native_match:
        native = native_match.group(1).replace(",", "")
        symbol = native_match.group(2).upper()
    usd_match = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", text)
    if usd_match:
        usd = usd_match.group(1).replace(",", "")
    return native, symbol, usd


def as_sources(value: Any) -> list[str]:
    if isinstance(value, list):
        return [text_value(item) for item in value if text_value(item)]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def confidence_bucket(value: Any) -> str:
    text = text_value(value).lower()
    if "verified" in text or "onchain" in text or "receipt" in text:
        return "verified"
    if "partial" in text or "candidate" in text:
        return "partial"
    if text:
        return "candidate"
    return "unknown"


def tx_url(mission: int, tx_hash: Any) -> str:
    tx_hash = text_value(tx_hash)
    if not tx_hash.startswith("0x"):
        return ""
    return MISSION_CONFIG[mission]["tx_explorer"].format(tx=tx_hash)


def address_url(mission: int, address: Any) -> str:
    address = normalize_address(address)
    if not address or address == ZERO:
        return ""
    return MISSION_CONFIG[mission]["address_explorer"].format(address=address)


def mission3_item_url(dog_id: int) -> str:
    return f"https://opensea.io/item/base/{BASE_DOG_CONTRACT}/{dog_id}"


def load_metadata() -> dict[int, dict[str, Any]]:
    rows = load_json(HISTORICAL_SEARCH, [])
    out: dict[int, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            dog_id = int_value(row.get("dog_id", row.get("token_id")), -1)
            if dog_id >= 0:
                out[dog_id] = row
    return out


def build_identity_cache() -> dict[str, dict[str, Any]]:
    cached = load_json(IDENTITY_PATH, {})
    profiles: dict[str, dict[str, Any]] = {normalize_address(k): v for k, v in cached.items() if normalize_address(k) and isinstance(v, dict)} if isinstance(cached, dict) else {}

    for path in [AUCTION_FEED, HISTORICAL_SEARCH]:
        rows = load_json(path, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            wallet = normalize_address(row.get("bidder_winner_wallet") or row.get("winner_wallet"))
            display = first_text(row.get("bidder_winner"), row.get("winner"))
            url = first_text(row.get("bidder_winner_url"), row.get("winner_url"))
            if not wallet or not display or not display.startswith("@"):
                continue
            profiles.setdefault(wallet, {
                "wallet": wallet,
                "display": display,
                "farcaster_handle": display.lstrip("@"),
                "farcaster_fid": None,
                "profile_url": url if "farcaster.xyz" in url else f"https://farcaster.xyz/{display.lstrip('@')}",
                "sources": ["generated_dashboard_identity"],
                "updated_at_utc": utc_now(),
            })
    write_json(IDENTITY_PATH, profiles)
    return profiles


def load_bid_lookup() -> dict[tuple[int, int], list[dict[str, Any]]]:
    lookup: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for mission, path in MISSION_BID_FILES.items():
        rows = load_json(path, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            dog_id = int_value(row.get("dog_id", row.get("token_id")), -1)
            if dog_id < 0:
                continue
            tx_hash = first_text(row.get("tx_hash"), row.get("transaction_hash"))
            lookup[(mission, dog_id)].append({
                "bidder": normalize_address(row.get("bidder")) or text_value(row.get("bidder")),
                "block_number": int_value(row.get("block_number"), 0) or None,
                "block_time_utc": first_text(row.get("block_time_utc")),
                "tx_hash": tx_hash,
                "tx_url": tx_url(mission, tx_hash),
                "native_amount_raw": first_text(row.get("value_raw"), row.get("amount_raw"), row.get("bid_wei")),
                "native_amount": first_text(row.get("value_display_native"), row.get("amount_eth"), row.get("bid_eth")),
            })
    return lookup


def load_recent_bids_by_dog() -> dict[int, list[dict[str, Any]]]:
    """Load current dashboard bid rows keyed by Dog id.

    This file is generated from the same fresh log scan as the top card/feed,
    so it can fill live-row bid stats and tx hashes when the deeper archive
    index is behind the latest current-auction bid.
    """
    rows = load_json(RECENT_BIDS, [])
    by_dog: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if not isinstance(rows, list):
        return by_dog
    for row in rows:
        if not isinstance(row, dict):
            continue
        dog_id = dog_id_from_row(row)
        if dog_id < 0:
            continue
        by_dog[dog_id].append(row)
    for dog_rows in by_dog.values():
        dog_rows.sort(key=lambda row: (first_text(row.get("bid_time_utc")), int_value(row.get("block_number"), 0)), reverse=True)
    return by_dog


def load_historical_rows_by_dog() -> dict[int, dict[str, Any]]:
    rows = load_json(HISTORICAL_SEARCH, [])
    by_dog: dict[int, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return by_dog
    for row in rows:
        if not isinstance(row, dict) or int_value(row.get("mission"), 0) != 3:
            continue
        dog_id = dog_id_from_row(row)
        if dog_id >= 0:
            by_dog[dog_id] = row
    return by_dog


def load_mission_rows() -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    notes: list[str] = []
    for mission, path in MISSION_INDEXES.items():
        data = load_json(path, [])
        if not isinstance(data, list):
            notes.append(f"missing_or_invalid:{path.relative_to(ROOT)}")
            continue
        for row in data:
            if isinstance(row, dict):
                enriched = dict(row)
                enriched["_mission"] = mission
                rows.append(enriched)
        notes.append(f"loaded:{path.relative_to(ROOT)}:{len(data)}")
    return rows, notes


def parse_rarity(value: Any) -> dict[str, Any]:
    text = text_value(value)
    if text.startswith("#") and "/" in text:
        left, right = text[1:].split("/", 1)
        return {"rank": int_value(left, None), "total": int_value(right, None), "display": text}
    return {"rank": None, "total": None, "display": text or None}


def parse_traits(value: Any) -> list[dict[str, str]]:
    items = []
    for part in text_value(value).split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            trait_type, trait_value = part.split(":", 1)
            trait_value = trait_value.strip()
            if " (" in trait_value and trait_value.endswith(")"):
                trait_value = trait_value.rsplit(" (", 1)[0].strip()
            items.append({"trait_type": trait_type.strip(), "value": trait_value, "display": part})
        else:
            items.append({"trait_type": "", "value": part, "display": part})
    return items


def amount_for_row(row: dict[str, Any], mission: int) -> dict[str, Any]:
    if mission == 1:
        native = first_text(row.get("amount_display_weth"), row.get("amount_weth"))
        symbol = "WETH"
        key = "ETH"
    elif mission == 2:
        native = first_text(row.get("amount_degen"), row.get("amount_display_native"))
        symbol = "DEGEN"
        key = "DEGEN"
    else:
        native = first_text(row.get("amount_eth"), row.get("settled_amount_eth"))
        symbol = "ETH"
        key = "ETH"
    return {
        "raw": first_text(row.get("amount_raw"), row.get("amount_wei")),
        "native": native or None,
        "native_symbol": symbol,
        "price_asset_key": key,
        "usd_estimate": None,
        "usd_estimate_display": None,
        "usd_estimate_source": None,
        "usd_estimate_confidence": "missing",
        "usd_estimate_time_basis": None,
    }


def normalize_status_text(status_text: Any, mission: int = 0) -> str:
    """Normalize dashboard/archive status labels without turning unsettled into settled.

    Mission 3 archive rows can be `settled=false` long after their auction timer
    ended. The live row is overlaid separately from current_auction, so generic
    Mission 3 archive rows should be treated as pending settlement rather than a
    second ongoing auction.
    """
    lowered = text_value(status_text).lower().strip().replace("-", "_")
    squashed = " ".join(lowered.replace("_", " ").split())
    if not squashed:
        return ""
    if squashed in {"ongoing", "live"}:
        return "ongoing"
    if "pending settlement" in squashed or "unsettled" in squashed:
        return "ended pending settlement"
    if mission == 3 and squashed == "ended":
        return "ended pending settlement"
    if "settled" in squashed:
        return "settled"
    return squashed


def status_for_row(row: dict[str, Any], mission: int) -> str:
    status = first_text(row.get("auction_status"), row.get("auction_state"), row.get("status"))
    if status:
        return normalize_status_text(status, mission)
    settled = row.get("settled")
    if settled is True or text_value(settled).lower() in {"true", "1", "yes"} or row.get("settled_block"):
        return "settled"
    if settled is False or text_value(settled).lower() in {"false", "0", "no"}:
        return "ended pending settlement" if mission == 3 else "unsettled"
    return "recovered"


def event_time_for_row(row: dict[str, Any], status: str) -> tuple[str | None, str]:
    settled_time = first_text(row.get("settled_time_utc"))
    if settled_time:
        return settled_time, "settlement_block_time"
    bid_time = first_text(row.get("last_bid_time_utc"), row.get("last_bid_utc"))
    if bid_time:
        return bid_time, "last_bid_block_time"
    created_time = first_text(row.get("auction_created_time_utc"), row.get("mint_time_utc"))
    if created_time:
        return created_time, "auction_created_block_time" if "auction" in status else "mint_time"
    return None, "unknown"


def normalize_record(row: dict[str, Any], metadata: dict[int, dict[str, Any]], identity: dict[str, dict[str, Any]], bid_lookup: dict[tuple[int, int], list[dict[str, Any]]]) -> dict[str, Any] | None:
    mission = int_value(row.get("_mission", row.get("mission")), 0)
    if mission not in MISSION_CONFIG:
        return None
    dog_id = int_value(row.get("dog_id", row.get("token_id")), -1)
    if dog_id < 0:
        return None
    cfg = MISSION_CONFIG[mission]
    meta = metadata.get(dog_id, {})
    status = status_for_row(row, mission)
    event_time, time_basis = event_time_for_row(row, status)
    amount = amount_for_row(row, mission)
    amount["usd_estimate_time_basis"] = time_basis if amount.get("native") else None

    winner_wallet = normalize_address(row.get("winner") or row.get("high_bidder") or row.get("bidder"))

    auction_created_tx = first_text(row.get("auction_created_tx"), row.get("created_tx_hash"))
    settled_tx = first_text(row.get("settled_tx"), row.get("settled_tx_hash"))
    dog_item_url = first_text(row.get("opensea_url")) if mission == 3 else ""
    if mission == 3 and not dog_item_url:
        dog_item_url = mission3_item_url(dog_id)

    bids = list(row.get("bid_history") or []) if isinstance(row.get("bid_history"), list) else []
    bids.extend(bid_lookup.get((mission, dog_id), []))
    bid_tx_hashes = []
    last_bid_time = ""
    top_bid: dict[str, Any] | None = None
    top_bid_amount: Decimal | None = None
    for bid in bids:
        if not isinstance(bid, dict):
            continue
        tx_hash = first_text(bid.get("tx_hash"), bid.get("transaction_hash"))
        if tx_hash and tx_hash not in bid_tx_hashes:
            bid_tx_hashes.append(tx_hash)
        btime = first_text(bid.get("block_time_utc"))
        if btime and btime > last_bid_time:
            last_bid_time = btime
        native_amount = decimal_value(first_text(bid.get("native_amount"), bid.get("amount_eth"), bid.get("value_display_native"), bid.get("bid_eth")))
        if native_amount is not None and (top_bid_amount is None or native_amount > top_bid_amount or (native_amount == top_bid_amount and btime > first_text(top_bid.get("block_time_utc") if top_bid else ""))):
            top_bid = bid
            top_bid_amount = native_amount

    if top_bid and (not amount.get("native") or status not in {"settled", "no_auction_dogmaster_reward"}):
        amount["raw"] = first_text(top_bid.get("native_amount_raw"), top_bid.get("amount_raw"), top_bid.get("value_raw"), amount.get("raw")) or None
        amount["native"] = first_text(top_bid.get("native_amount"), top_bid.get("amount_eth"), top_bid.get("value_display_native"), top_bid.get("bid_eth"), amount.get("native")) or None
        amount["native_symbol"] = cfg["currency"]
        amount["price_asset_key"] = cfg["price_asset_key"]
        amount["usd_estimate_time_basis"] = "last_bid_block_time"
        event_time = first_text(top_bid.get("block_time_utc"), event_time) or event_time
        time_basis = "last_bid_block_time"
        if not winner_wallet:
            winner_wallet = normalize_address(top_bid.get("bidder"))

    profile = identity.get(winner_wallet, {}) if winner_wallet else {}
    display = first_text(profile.get("display"), short_address(winner_wallet), row.get("winner"))
    profile_url = first_text(profile.get("profile_url"))

    rarity_display = first_text(meta.get("rarity"), row.get("rarity"))
    traits_text = first_text(meta.get("traits"), row.get("traits"))
    source_conf = confidence_bucket(first_text(row.get("confidence"), row.get("source_confidence")))
    repo_archive = f"archive/dogs/by-id/{dog_id:03d}.json"
    notes = cfg["source_note"]
    if status.startswith("no_auction"):
        notes += " This is a verified non-auction/special-case record, not a fake auction row."

    record = {
        "schema_version": 1,
        "dog_id": dog_id,
        "mission": mission,
        "era_label": cfg["era_label"],
        "chain": cfg["chain"],
        "chain_id": cfg["chain_id"],
        "status": status,
        "dog_image_url": first_text(meta.get("dog_image_url"), row.get("dog_image_url"), f"https://api.degendogs.club/images/{dog_id}.png"),
        "dog_item_url": dog_item_url or None,
        "auction_house": first_text(row.get("auction_house")) or None,
        "auction_created": {
            "block_number": int_value(row.get("auction_created_block"), None),
            "block_time_utc": first_text(row.get("auction_created_time_utc")) or None,
            "tx_hash": auction_created_tx or None,
            "tx_url": tx_url(mission, auction_created_tx) or None,
        },
        "settlement": {
            "settled": status == "settled" or bool(row.get("settled_block")),
            "block_number": int_value(row.get("settled_block"), None),
            "block_time_utc": first_text(row.get("settled_time_utc")) or None,
            "tx_hash": settled_tx or None,
            "tx_url": tx_url(mission, settled_tx) or None,
        },
        "winner_or_high_bidder": {
            "wallet": winner_wallet or None,
            "display": display or None,
            "farcaster_fid": profile.get("farcaster_fid"),
            "farcaster_handle": profile.get("farcaster_handle"),
            "profile_url": profile_url or None,
            "wallet_explorer_url": address_url(mission, winner_wallet) or None,
        },
        "amount": amount,
        "bid_stats": {
            "bid_count": int_value(row.get("bid_count"), 0),
            "unique_bidder_count": int_value(row.get("unique_bidder_count"), 0),
            "last_bid_time_utc": last_bid_time or None,
        },
        "bid_tx_hashes": bid_tx_hashes,
        "rarity": parse_rarity(rarity_display),
        "traits": parse_traits(first_text(meta.get("trait_rarity"), traits_text)),
        "links": {
            "item": dog_item_url or None,
            "dog_page": first_text(meta.get("dog_external_url"), row.get("dog_external_url"), f"https://degendogs.club/#dog{dog_id}"),
            "auction_tx": tx_url(mission, auction_created_tx) or None,
            "settlement_tx": tx_url(mission, settled_tx) or None,
            "explorer": address_url(mission, winner_wallet) or None,
            "repo_archive": repo_archive,
        },
        "source": {
            "confidence": source_conf,
            "raw_confidence": first_text(row.get("confidence"), row.get("source_confidence")) or None,
            "sources": as_sources(row.get("sources")),
            "notes": notes,
        },
        "activity_time_utc": event_time,
        "activity_time_basis": time_basis,
    }
    search_parts: list[Any] = [
        f"dog {dog_id}", f"dog #{dog_id}", str(dog_id), cfg["era_label"], f"mission {mission}", cfg["chain"],
        status, winner_wallet, display, profile.get("farcaster_handle"), amount.get("native"), amount.get("native_symbol"),
        auction_created_tx, settled_tx, rarity_display, traits_text,
    ]
    search_parts.extend(bid_tx_hashes)
    search_parts.extend(as_sources(row.get("sources")))
    record["search_text"] = " ".join(str(part) for part in search_parts if part).lower()
    return record


def archive_status_from_feed(status_text: str) -> str:
    status = normalize_status_text(status_text, 3)
    if status:
        return status
    lowered = text_value(status_text).lower()
    if "ended" in lowered or "pending settlement" in lowered or "unsettled" in lowered:
        return "ended pending settlement"
    if "settled" in lowered:
        return "settled"
    return lowered


def record_sort_key(record: dict[str, Any]) -> tuple[int, str, int]:
    status = text_value(record.get("status")).lower()
    current_rank = 1 if status in {"ongoing", "live"} else 0
    activity = text_value(record.get("activity_time_utc"))
    return (current_rank, activity, int_value(record.get("dog_id"), 0))


def current_overlay_search_text(record: dict[str, Any], dog_id: int) -> str:
    """Build fresh search terms for the live Mission 3 row after overlay.

    Do not append to the previous archive-derived search text: that text can
    contain stale high-bidder or amount terms from a lagging Mission 3 archive
    snapshot, which makes searches for an old bidder return the current row.
    """
    raw_who = record.get("winner_or_high_bidder")
    who: dict[str, Any] = raw_who if isinstance(raw_who, dict) else {}
    raw_amount = record.get("amount")
    amount: dict[str, Any] = raw_amount if isinstance(raw_amount, dict) else {}
    raw_rarity = record.get("rarity")
    rarity: dict[str, Any] = raw_rarity if isinstance(raw_rarity, dict) else {}
    raw_source = record.get("source")
    source: dict[str, Any] = raw_source if isinstance(raw_source, dict) else {}
    raw_auction_created = record.get("auction_created")
    auction_created: dict[str, Any] = raw_auction_created if isinstance(raw_auction_created, dict) else {}
    raw_settlement = record.get("settlement")
    settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
    raw_traits = record.get("traits")
    traits: list[Any] = raw_traits if isinstance(raw_traits, list) else []

    parts: list[Any] = [
        f"dog {dog_id}",
        f"dog #{dog_id}",
        str(dog_id),
        "Mission 3",
        "mission 3",
        "Base",
        record.get("status"),
        who.get("wallet"),
        who.get("display"),
        who.get("farcaster_handle"),
        amount.get("native"),
        amount.get("native_symbol"),
        amount.get("raw"),
        amount.get("usd_estimate"),
        amount.get("usd_estimate_display"),
        record.get("activity_time_utc"),
        auction_created.get("tx_hash"),
        settlement.get("tx_hash"),
        rarity.get("display"),
        "generated auction feed",
    ]
    for bid_tx_hash in record.get("bid_tx_hashes") or []:
        parts.append(bid_tx_hash)
    for trait in traits:
        if isinstance(trait, dict):
            parts.extend([trait.get("display"), trait.get("trait_type"), trait.get("value")])
        else:
            parts.append(trait)
    parts.extend(as_sources(source.get("sources")))
    return " ".join(str(part) for part in parts if text_value(part)).lower()


def load_existing_per_dog_records() -> dict[int, dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
    by_id = DOG_ARCHIVE / "by-id"
    if not by_id.exists():
        return records
    for path in by_id.glob("*.json"):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        record = payload.get("record")
        if not isinstance(record, dict):
            continue
        dog_id = int_value(record.get("dog_id"), -1)
        mission = int_value(record.get("mission"), -1)
        if mission == 3 and dog_id >= 0:
            records[dog_id] = record
    return records


def fill_missing_fields(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if value in (None, "", [], {}):
            continue
        if target.get(key) in (None, "", [], {}):
            target[key] = value


def merge_existing_historical_context(record: dict[str, Any], existing: dict[str, Any] | None) -> None:
    if not existing:
        return
    if int_value(existing.get("mission"), -1) != 3 or int_value(existing.get("dog_id"), -1) != int_value(record.get("dog_id"), -2):
        return
    if text_value(existing.get("status")).lower() not in {"settled", "ended pending settlement"}:
        return

    for key in ("auction_created", "settlement", "links"):
        target_raw = record.get(key)
        source_raw = existing.get(key)
        target: dict[str, Any] = target_raw if isinstance(target_raw, dict) else {}
        source: dict[str, Any] = source_raw if isinstance(source_raw, dict) else {}
        fill_missing_fields(target, source)
        record[key] = target

    amount_raw = record.get("amount")
    existing_amount_raw = existing.get("amount")
    amount: dict[str, Any] = amount_raw if isinstance(amount_raw, dict) else {}
    existing_amount: dict[str, Any] = existing_amount_raw if isinstance(existing_amount_raw, dict) else {}
    fill_missing_fields(amount, existing_amount)
    if existing_amount.get("amount_usd_at_event") or existing_amount.get("usd_estimate_source_detail"):
        for key in (
            "usd_estimate",
            "usd_estimate_display",
            "usd_estimate_source",
            "usd_estimate_source_detail",
            "usd_estimate_confidence",
            "usd_estimate_notes",
            "usd_estimate_price_usd",
            "usd_estimate_price_date_utc",
            "usd_estimate_time_basis",
            "amount_usd_at_event",
            "eth_usd_price_at_event",
            "eth_usd_price_date_utc",
        ):
            value = existing_amount.get(key)
            if value not in (None, "", [], {}):
                amount[key] = value
    record["amount"] = amount

    bid_stats_raw = record.get("bid_stats")
    existing_stats_raw = existing.get("bid_stats")
    bid_stats: dict[str, Any] = bid_stats_raw if isinstance(bid_stats_raw, dict) else {}
    existing_stats: dict[str, Any] = existing_stats_raw if isinstance(existing_stats_raw, dict) else {}
    for count_key in ("bid_count", "unique_bidder_count"):
        bid_stats[count_key] = max(int_value(bid_stats.get(count_key), 0), int_value(existing_stats.get(count_key), 0))
    if not bid_stats.get("last_bid_time_utc") and existing_stats.get("last_bid_time_utc"):
        bid_stats["last_bid_time_utc"] = existing_stats.get("last_bid_time_utc")
    record["bid_stats"] = bid_stats

    tx_hashes = list(record.get("bid_tx_hashes") or [])
    for tx_hash in existing.get("bid_tx_hashes") or []:
        if tx_hash and tx_hash not in tx_hashes:
            tx_hashes.append(tx_hash)
    record["bid_tx_hashes"] = tx_hashes

    source_raw = record.get("source")
    existing_source_raw = existing.get("source")
    source: dict[str, Any] = source_raw if isinstance(source_raw, dict) else {}
    existing_source: dict[str, Any] = existing_source_raw if isinstance(existing_source_raw, dict) else {}
    sources = as_sources(existing_source.get("sources"))
    for item in as_sources(source.get("sources")):
        if item not in sources:
            sources.append(item)
    source["sources"] = sources
    fill_missing_fields(source, existing_source)
    record["source"] = source


def historical_mission3_record(row: dict[str, Any], metadata: dict[int, dict[str, Any]], identity: dict[str, dict[str, Any]], existing_record: dict[str, Any] | None = None) -> dict[str, Any] | None:
    dog_id = int_value(row.get("token_id", row.get("dog_id")), -1)
    if dog_id < 0:
        return None
    status = normalize_status_text(row.get("status"), 3)
    if status not in {"ongoing", "settled", "ended pending settlement"}:
        return None

    meta = metadata.get(dog_id, {})
    native, symbol, usd_value = parse_historical_amount_display(row.get("amount"), MISSION_CONFIG[3]["currency"])
    wallet = normalize_address(row.get("winner_wallet") or row.get("winner"))
    profile = identity.get(wallet, {}) if wallet else {}
    display = first_text(row.get("winner"), profile.get("display"), short_address(wallet))
    profile_url = first_text(row.get("winner_url"), profile.get("profile_url"))
    auction_created_time = iso_utc(row.get("auction_created_time_utc"))
    settlement_time = iso_utc(row.get("settled_time_utc"))
    activity = settlement_time if status == "settled" else first_text(settlement_time, auction_created_time)
    amount = {
        "raw": first_text(row.get("amount_raw"), eth_to_wei(native)) or None,
        "native": native or None,
        "native_symbol": symbol or MISSION_CONFIG[3]["currency"],
        "price_asset_key": MISSION_CONFIG[3]["price_asset_key"],
        "usd_estimate": str(decimal_value(usd_value) or usd_value) if usd_value else None,
        "usd_estimate_display": usd_display(usd_value) or None,
        "usd_estimate_source": "historical_dog_search_amount" if usd_value else None,
        "usd_estimate_confidence": "medium" if usd_value else "missing",
        "usd_estimate_time_basis": "settlement_block_time" if status == "settled" and native else ("auction_created_block_time" if native else None),
        "amount_usd_at_event": str(decimal_value(usd_value) or usd_value) if usd_value and status != "ongoing" else None,
        "eth_usd_price_at_event": None,
        "eth_usd_price_date_utc": None,
    }
    sources = as_sources(row.get("sources"))
    if "historical_dog_search_backfill" not in sources:
        sources.append("historical_dog_search_backfill")
    record = {
        "schema_version": 1,
        "dog_id": dog_id,
        "mission": 3,
        "era_label": "Mission 3",
        "chain": "Base",
        "chain_id": 8453,
        "status": status,
        "dog_image_url": first_text(row.get("dog_image_url"), meta.get("dog_image_url"), f"https://api.degendogs.club/images/{dog_id}.png"),
        "dog_item_url": first_text(row.get("dog_opensea_url"), meta.get("dog_opensea_url"), mission3_item_url(dog_id)) or None,
        "auction_house": None,
        "auction_created": {
            "block_number": None,
            "block_time_utc": auction_created_time or None,
            "tx_hash": None,
            "tx_url": None,
        },
        "settlement": {
            "settled": status == "settled",
            "block_number": None,
            "block_time_utc": settlement_time or None,
            "tx_hash": None,
            "tx_url": None,
        },
        "winner_or_high_bidder": {
            "wallet": wallet or None,
            "display": display or None,
            "farcaster_fid": profile.get("farcaster_fid"),
            "farcaster_handle": first_text(profile.get("farcaster_handle"), display.lstrip("@") if display.startswith("@") else "") or None,
            "profile_url": profile_url or None,
            "wallet_explorer_url": address_url(3, wallet) or None,
        },
        "amount": amount,
        "bid_stats": {
            "bid_count": int_value(row.get("bid_count"), 0),
            "unique_bidder_count": int_value(row.get("unique_bidder_count"), 0),
            "last_bid_time_utc": None,
        },
        "bid_tx_hashes": [],
        "rarity": parse_rarity(first_text(row.get("rarity"), meta.get("rarity"))),
        "traits": parse_traits(first_text(row.get("trait_rarity"), meta.get("trait_rarity"), row.get("traits"), meta.get("traits"))),
        "links": {
            "item": first_text(row.get("dog_opensea_url"), meta.get("dog_opensea_url"), mission3_item_url(dog_id)) or None,
            "dog_page": first_text(row.get("dog_external_url"), meta.get("dog_external_url"), f"https://degendogs.club/#dog{dog_id}"),
            "auction_tx": None,
            "settlement_tx": None,
            "explorer": address_url(3, wallet) or None,
            "repo_archive": f"archive/dogs/by-id/{dog_id:03d}.json",
        },
        "source": {
            "confidence": confidence_bucket(row.get("confidence")),
            "raw_confidence": first_text(row.get("confidence")) or None,
            "sources": sources,
            "notes": MISSION_CONFIG[3]["source_note"] + " Backfilled from generated historical_dog_search so settled Mission 3 rows do not disappear when they age out of the recent feed.",
        },
        "activity_time_utc": activity or None,
        "activity_time_basis": "settlement_block_time" if status == "settled" else "auction_created_block_time",
    }
    merge_existing_historical_context(record, existing_record)
    record["search_text"] = current_overlay_search_text(record, dog_id)
    historical_search = first_text(row.get("search_text"))
    if historical_search:
        record["search_text"] = f"{record['search_text']} {historical_search.lower()}"
    return record


def apply_historical_mission3_backfill(records: list[dict[str, Any]], metadata: dict[int, dict[str, Any]], identity: dict[str, dict[str, Any]]) -> int:
    historical_rows = load_json(HISTORICAL_SEARCH, [])
    if not isinstance(historical_rows, list):
        return 0
    existing_by_id = load_existing_per_dog_records()
    by_key = {(int_value(record.get("mission")), int_value(record.get("dog_id"))): record for record in records}
    added = 0
    for row in historical_rows:
        if not isinstance(row, dict) or int_value(row.get("mission"), 0) != 3:
            continue
        dog_id = int_value(row.get("token_id", row.get("dog_id")), -1)
        if dog_id < 0 or (3, dog_id) in by_key:
            continue
        record = historical_mission3_record(row, metadata, identity, existing_by_id.get(dog_id))
        if not record:
            continue
        records.append(record)
        by_key[(3, dog_id)] = record
        added += 1
    return added


def generated_feed_record(feed: dict[str, Any], current: dict[str, Any], identity: dict[str, dict[str, Any]]) -> dict[str, Any]:
    dog_id = dog_id_from_row(feed)
    status_text = text_value(feed.get("status")).lower()
    status = archive_status_from_feed(status_text)
    wallet = normalize_address(feed.get("bidder_winner_wallet") or current.get("bidder_wallet"))
    profile = identity.get(wallet, {}) if wallet else {}
    display = first_text(feed.get("bidder_winner"), current.get("bidder"), profile.get("display"), short_address(wallet))
    profile_url = first_text(feed.get("bidder_winner_url"), current.get("bidder_url"), profile.get("profile_url"))
    native = first_text(feed.get("amount_eth"), current.get("current_bid_eth"))
    event_priced = status != "ongoing"
    event_usd_value = first_text(
        feed.get("amount_usd_at_event"),
        feed.get("amount_usd") if event_priced else "",
    )
    usd_value = first_text(
        event_usd_value,
        feed.get("amount_usd"),
        current.get("current_bid_usd"),
    )
    price_usd = first_text(feed.get("eth_usd_price_at_event"), feed.get("eth_usd_price_live"), current.get("eth_usd_price_live"))
    price_date = first_text(feed.get("eth_usd_price_date_utc"), current.get("eth_usd_price_date_utc"))
    usd_source = first_text(feed.get("usd_estimate_source"), "generated_auction_feed" if usd_value else "")
    usd_confidence = first_text(feed.get("usd_estimate_confidence"), "medium" if usd_value else "missing")
    amount = {
        "raw": eth_to_wei(native) or None,
        "native": native or None,
        "native_symbol": "ETH",
        "price_asset_key": "ETH",
        "usd_estimate": str(decimal_value(usd_value) or usd_value) if usd_value else None,
        "usd_estimate_display": usd_display(usd_value) or None,
        "usd_estimate_source": usd_source or None,
        "usd_estimate_source_detail": first_text(feed.get("usd_estimate_source_detail")) or None,
        "usd_estimate_confidence": usd_confidence,
        "usd_estimate_price_usd": price_usd or None,
        "usd_estimate_price_date_utc": price_date or None,
        "amount_usd_at_event": str(decimal_value(event_usd_value) or event_usd_value) if event_usd_value not in (None, "") else None,
        "eth_usd_price_at_event": first_text(feed.get("eth_usd_price_at_event")) or None,
        "eth_usd_price_date_utc": price_date or None,
        "usd_estimate_time_basis": ("settlement_block_time" if status == "settled" else "last_bid_block_time") if native else None,
    }
    activity = iso_utc(first_text(
        feed.get("settled_time_utc") if status == "settled" else "",
        feed.get("last_bid_utc"),
        feed.get("auction_time_utc"),
        current.get("latest_block_time_utc"),
        current.get("start_time_utc"),
    ))
    auction_created_time = iso_utc(first_text(current.get("start_time_utc"), feed.get("auction_time_utc")))
    settlement_time = iso_utc(first_text(feed.get("settled_time_utc"), feed.get("auction_time_utc") if status == "settled" else ""))
    last_bid_time = iso_utc(first_text(feed.get("last_bid_utc"), current.get("latest_block_time_utc") if status != "settled" else ""))
    record = {
        "schema_version": 1,
        "dog_id": dog_id,
        "mission": 3,
        "era_label": "Mission 3",
        "chain": "Base",
        "chain_id": 8453,
        "status": status,
        "dog_image_url": first_text(feed.get("dog_image_url"), current.get("dog_image_url"), f"https://api.degendogs.club/images/{dog_id}.png"),
        "dog_item_url": first_text(feed.get("dog_opensea_url"), current.get("dog_opensea_url"), mission3_item_url(dog_id)) or None,
        "auction_house": None,
        "auction_created": {
            "block_number": None,
            "block_time_utc": auction_created_time or None,
            "tx_hash": None,
            "tx_url": None,
        },
        "settlement": {
            "settled": status == "settled",
            "block_number": None,
            "block_time_utc": settlement_time or None,
            "tx_hash": None,
            "tx_url": None,
        },
        "winner_or_high_bidder": {
            "wallet": wallet or None,
            "display": display or None,
            "farcaster_fid": profile.get("farcaster_fid"),
            "farcaster_handle": first_text(profile.get("farcaster_handle"), display.lstrip("@") if display.startswith("@") else "") or None,
            "profile_url": profile_url or None,
            "wallet_explorer_url": address_url(3, wallet) or None,
        },
        "amount": amount,
        "bid_stats": {
            "bid_count": 0,
            "unique_bidder_count": 0,
            "last_bid_time_utc": last_bid_time or None,
        },
        "bid_tx_hashes": [],
        "rarity": parse_rarity(first_text(feed.get("rarity"), current.get("rarity"))),
        "traits": parse_traits(first_text(current.get("trait_rarity"), feed.get("trait_rarity"), current.get("traits"), feed.get("traits"))),
        "links": {
            "item": first_text(feed.get("dog_opensea_url"), current.get("dog_opensea_url"), mission3_item_url(dog_id)) or None,
            "dog_page": first_text(feed.get("dog_external_url"), current.get("dog_external_url"), f"https://degendogs.club/#dog{dog_id}"),
            "auction_tx": None,
            "settlement_tx": None,
            "explorer": address_url(3, wallet) or None,
            "repo_archive": f"archive/dogs/by-id/{dog_id:03d}.json",
        },
        "source": {
            "confidence": "verified",
            "raw_confidence": "generated_auction_feed",
            "sources": ["generated_auction_feed"],
            "notes": MISSION_CONFIG[3]["source_note"],
        },
        "activity_time_utc": activity or None,
        "activity_time_basis": "settlement_block_time" if status == "settled" else "last_bid_block_time",
    }
    record["search_text"] = current_overlay_search_text(record, dog_id)
    return record


def apply_current_auction_overrides(records: list[dict[str, Any]], identity: dict[str, dict[str, Any]]) -> int:
    """Keep the live/current Mission 3 row aligned with the rendered auction feed.

    The full Mission 3 archive can lag the current auction bid stream between
    hourly archive refreshes. The visible dashboard card and static auction feed
    are built from current auction state, so the unified archive/search index must
    overlay that same ongoing row before it is published to public/generated.
    """
    feed_rows = load_json(AUCTION_FEED, [])
    current_rows = load_json(CURRENT_AUCTION, [])
    if not isinstance(feed_rows, list):
        feed_rows = []
    if not isinstance(current_rows, list):
        current_rows = []
    current_by_id = {dog_id_from_row(row): row for row in current_rows if isinstance(row, dict) and dog_id_from_row(row) >= 0}
    recent_bids_by_id = load_recent_bids_by_dog()
    historical_by_id = load_historical_rows_by_dog()
    by_key = {(int_value(record.get("mission")), int_value(record.get("dog_id"))): record for record in records}
    updates = 0
    for feed in feed_rows:
        if not isinstance(feed, dict):
            continue
        status_text = text_value(feed.get("status")).lower()
        feed_status = archive_status_from_feed(status_text)
        if feed_status not in {"ongoing", "settled", "ended pending settlement"}:
            continue
        dog_id = dog_id_from_row(feed)
        record = by_key.get((3, dog_id))
        current = current_by_id.get(dog_id, {})
        if not record:
            record = generated_feed_record(feed, current, identity)
            records.append(record)
            by_key[(3, dog_id)] = record
        wallet = normalize_address(feed.get("bidder_winner_wallet") or current.get("bidder_wallet"))
        prior_who = record.get("winner_or_high_bidder") if isinstance(record.get("winner_or_high_bidder"), dict) else {}
        if not wallet:
            wallet = normalize_address(prior_who.get("wallet"))
        profile = identity.get(wallet, {}) if wallet else {}
        display = first_text(feed.get("bidder_winner"), current.get("bidder"), profile.get("display"), short_address(wallet), prior_who.get("display"))
        profile_url = first_text(feed.get("bidder_winner_url"), current.get("bidder_url"), profile.get("profile_url"), prior_who.get("profile_url"))
        record["status"] = feed_status
        record["dog_image_url"] = first_text(feed.get("dog_image_url"), current.get("dog_image_url"), record.get("dog_image_url")) or None
        record["dog_item_url"] = first_text(feed.get("dog_opensea_url"), current.get("dog_opensea_url"), record.get("dog_item_url"), mission3_item_url(dog_id)) or None
        record["winner_or_high_bidder"] = {
            "wallet": wallet or None,
            "display": display or None,
            "farcaster_fid": profile.get("farcaster_fid"),
            "farcaster_handle": first_text(profile.get("farcaster_handle"), display.lstrip("@") if display.startswith("@") else "") or None,
            "profile_url": profile_url or None,
            "wallet_explorer_url": address_url(3, wallet) or None,
        }
        raw_amount = record.get("amount")
        amount: dict[str, Any] = raw_amount if isinstance(raw_amount, dict) else {}
        native = first_text(feed.get("amount_eth"), current.get("current_bid_eth"), amount.get("native"))
        amount["native"] = native or None
        amount["native_symbol"] = "ETH"
        amount["price_asset_key"] = "ETH"
        amount["raw"] = eth_to_wei(native) or amount.get("raw")
        event_priced = feed_status != "ongoing"
        event_usd_value = first_text(
            feed.get("amount_usd_at_event"),
            feed.get("amount_usd") if event_priced else "",
        )
        usd_value = first_text(
            event_usd_value,
            feed.get("amount_usd"),
            current.get("current_bid_usd"),
            amount.get("usd_estimate"),
        )
        if usd_value:
            amount["usd_estimate"] = str(decimal_value(usd_value) or usd_value)
            amount["usd_estimate_display"] = usd_display(usd_value) or amount.get("usd_estimate_display")
            amount["usd_estimate_source"] = first_text(feed.get("usd_estimate_source"), amount.get("usd_estimate_source"), "generated_auction_feed")
            amount["usd_estimate_source_detail"] = first_text(feed.get("usd_estimate_source_detail"), amount.get("usd_estimate_source_detail")) or None
            amount["usd_estimate_confidence"] = first_text(feed.get("usd_estimate_confidence"), amount.get("usd_estimate_confidence"), "medium")
            price_usd = first_text(feed.get("eth_usd_price_at_event"), feed.get("eth_usd_price_live"), current.get("eth_usd_price_live"), amount.get("usd_estimate_price_usd"))
            price_date = first_text(feed.get("eth_usd_price_date_utc"), current.get("eth_usd_price_date_utc"), amount.get("usd_estimate_price_date_utc"))
            amount["usd_estimate_price_usd"] = price_usd or None
            amount["usd_estimate_price_date_utc"] = price_date or None
            amount["amount_usd_at_event"] = str(decimal_value(event_usd_value) or event_usd_value) if event_usd_value not in (None, "") else amount.get("amount_usd_at_event")
            amount["eth_usd_price_at_event"] = first_text(feed.get("eth_usd_price_at_event"), amount.get("eth_usd_price_at_event")) or None
            amount["eth_usd_price_date_utc"] = price_date or None
            amount["usd_estimate_time_basis"] = "settlement_block_time" if feed_status == "settled" else "last_bid_block_time"
        record["amount"] = amount
        activity = iso_utc(first_text(
            feed.get("settled_time_utc") if record["status"] == "settled" else "",
            feed.get("last_bid_utc"),
            feed.get("auction_time_utc"),
            current.get("latest_block_time_utc"),
            record.get("activity_time_utc"),
        ))
        if activity:
            record["activity_time_utc"] = activity
            record["activity_time_basis"] = "settlement_block_time" if record["status"] == "settled" else "last_bid_block_time"
        raw_settlement = record.get("settlement")
        settlement: dict[str, Any] = raw_settlement if isinstance(raw_settlement, dict) else {}
        if record["status"] == "settled":
            settlement["settled"] = True
            settlement["block_time_utc"] = first_text(settlement.get("block_time_utc"), iso_utc(feed.get("settled_time_utc")), iso_utc(feed.get("auction_time_utc"))) or None
        else:
            settlement["settled"] = False
        record["settlement"] = settlement
        raw_bid_stats = record.get("bid_stats")
        bid_stats: dict[str, Any] = raw_bid_stats if isinstance(raw_bid_stats, dict) else {}
        historical = historical_by_id.get(dog_id, {})
        recent_bids = recent_bids_by_id.get(dog_id, [])
        last_bid_time = iso_utc(first_text(feed.get("last_bid_utc"), historical.get("last_bid_time_utc"), bid_stats.get("last_bid_time_utc")))
        if last_bid_time:
            bid_stats["last_bid_time_utc"] = last_bid_time
        bid_count = int_value(historical.get("bid_count"), int_value(bid_stats.get("bid_count"), 0))
        unique_bidder_count = int_value(historical.get("unique_bidder_count"), int_value(bid_stats.get("unique_bidder_count"), 0))
        if recent_bids:
            bid_count = max(bid_count, len(recent_bids))
            recent_wallets = {normalize_address(row.get("bidder_wallet") or row.get("bidder")) for row in recent_bids}
            recent_wallets.discard("")
            unique_bidder_count = max(unique_bidder_count, len(recent_wallets))
            bid_tx_hashes = list(record.get("bid_tx_hashes") or [])
            for bid_row in recent_bids:
                tx_hash = first_text(bid_row.get("tx_hash"), bid_row.get("transaction_hash"))
                if tx_hash and tx_hash not in bid_tx_hashes:
                    bid_tx_hashes.append(tx_hash)
            record["bid_tx_hashes"] = bid_tx_hashes
        if bid_count:
            bid_stats["bid_count"] = bid_count
        if unique_bidder_count:
            bid_stats["unique_bidder_count"] = unique_bidder_count
        record["bid_stats"] = bid_stats
        record["rarity"] = parse_rarity(first_text(feed.get("rarity"), current.get("rarity"), record.get("rarity", {}).get("display") if isinstance(record.get("rarity"), dict) else ""))
        record["traits"] = parse_traits(first_text(current.get("trait_rarity"), feed.get("trait_rarity"), current.get("traits"), feed.get("traits"))) or record.get("traits", [])
        links = record.get("links") if isinstance(record.get("links"), dict) else {}
        links["item"] = record.get("dog_item_url")
        links["dog_page"] = first_text(feed.get("dog_external_url"), current.get("dog_external_url"), links.get("dog_page"), f"https://degendogs.club/#dog{dog_id}")
        links["explorer"] = address_url(3, wallet) or links.get("explorer")
        record["links"] = links
        source_raw = record.get("source")
        source: dict[str, Any] = source_raw if isinstance(source_raw, dict) else {}
        sources = [item for item in as_sources(source.get("sources")) if item != "historical_dog_search_backfill"]
        if "generated_auction_feed" not in sources:
            sources.append("generated_auction_feed")
        source["sources"] = sources
        source["confidence"] = "verified"
        source_note = first_text(source.get("notes"), MISSION_CONFIG[3]["source_note"])
        if "historical_dog_search" in source_note:
            source_note = MISSION_CONFIG[3]["source_note"]
        source["notes"] = source_note
        record["source"] = source
        record["search_text"] = current_overlay_search_text(record, dog_id)
        updates += 1
    return updates


def write_per_dog_records(records: list[dict[str, Any]]) -> None:
    by_id = DOG_ARCHIVE / "by-id"
    by_id.mkdir(parents=True, exist_ok=True)
    current_files = set()
    now = utc_now()
    for record in records:
        dog_id = int_value(record.get("dog_id"), -1)
        if dog_id < 0:
            continue
        path = by_id / f"{dog_id:03d}.json"
        existing = load_json(path, {})
        existing_generated_at = now
        if isinstance(existing, dict):
            existing_generated_at = text_value(existing.get("generated_at_utc")) or now
            if existing.get("record") == record:
                current_files.add(path.name)
                continue
        payload = {
            "schema_version": 1,
            "generated_at_utc": existing_generated_at,
            "record": record,
        }
        write_json(path, payload)
        current_files.add(path.name)
    for stale in by_id.glob("*.json"):
        if stale.name not in current_files:
            stale.unlink()
    manifest = {
        "schema_version": 1,
        "updated_at_utc": now,
        "record_count": len(records),
        "paths": [f"archive/dogs/by-id/{int_value(record.get('dog_id')):03d}.json" for record in records],
    }
    write_json(DOG_ARCHIVE / "manifest.json", manifest)


def build_unified_index() -> dict[str, Any]:
    metadata = load_metadata()
    identity = build_identity_cache()
    bid_lookup = load_bid_lookup()
    rows, notes = load_mission_rows()
    records: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        record = normalize_record(row, metadata, identity, bid_lookup)
        if not record:
            continue
        key = (int(record["mission"]), int(record["dog_id"]))
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    historical_mission3_backfills = apply_historical_mission3_backfill(records, metadata, identity)
    current_overrides = apply_current_auction_overrides(records, identity)
    records.sort(key=record_sort_key, reverse=True)

    counts_by_mission = Counter(str(record["mission"]) for record in records)
    counts_by_conf = Counter(record["source"]["confidence"] for record in records)
    manifest = {
        "schema_version": 1,
        "updated_at_utc": utc_now(),
        "total_records": len(records),
        "records_by_mission": {str(mission): counts_by_mission.get(str(mission), 0) for mission in [1, 2, 3]},
        "records_by_confidence": dict(sorted(counts_by_conf.items())),
        "source_files": [str(path.relative_to(ROOT)) for path in MISSION_INDEXES.values() if path.exists()],
        "notes": notes + [f"historical_mission3_backfills:{historical_mission3_backfills}", f"current_auction_overrides:{current_overrides}", "Initial dashboard view remains capped at the latest 10; search loads this index for all verified archive matches."],
    }

    write_json(ARCHIVE_GENERATED / "unified_dog_search_index.json", records)
    write_json(ARCHIVE_GENERATED / "unified_dog_search_manifest.json", manifest)
    write_json(PUBLIC_GENERATED / "unified_dog_search_index.json", records)
    write_json(PUBLIC_GENERATED / "unified_dog_search_manifest.json", manifest)
    write_per_dog_records(records)
    return manifest


def main() -> None:
    manifest = build_unified_index()
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
