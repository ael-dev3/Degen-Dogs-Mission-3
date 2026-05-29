#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import concurrent.futures
import html
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 80

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "sql" / "mission3_dashboard.sql"
GENERATED = ROOT / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"
CACHE_DIR = ROOT / ".cache"
DOG_METADATA_CACHE = CACHE_DIR / "dog_metadata.json"
README_TEMPLATE_PATH = ROOT / "README.template.md"
HISTORICAL_ARCHIVE_INDEXES = {
    1: ROOT / "archive" / "mission1" / "data" / "generated" / "mission1_dog_search_index.json",
    2: ROOT / "archive" / "mission2" / "data" / "generated" / "mission2_dog_search_index.json",
    3: ROOT / "archive" / "mission3" / "data" / "generated" / "mission3_dog_search_index.json",
}
MISSION_CHAIN = {
    1: ("Polygon", 137),
    2: ("Degen Chain", 666666666),
    3: ("Base", 8453),
}

DEFAULT_RPC_URLS = [
    "https://base-rpc.publicnode.com",
    "https://mainnet.base.org",
    "https://developer-access-mainnet.base.org",
]
DEFAULT_LOG_RPC_URLS = ["https://mainnet.base.org"]
RPC_URLS = [os.environ["BASE_RPC_URL"]] if os.environ.get("BASE_RPC_URL") else [
    url.strip()
    for url in os.environ.get("BASE_RPC_URLS", ",".join(DEFAULT_RPC_URLS)).split(",")
    if url.strip()
]
LOG_RPC_URLS = [os.environ["BASE_RPC_URL"]] if os.environ.get("BASE_RPC_URL") else [
    url.strip()
    for url in os.environ.get("BASE_LOG_RPC_URLS", ",".join(DEFAULT_LOG_RPC_URLS)).split(",")
    if url.strip()
]
FROM_BLOCK = int(os.environ.get("BASE_FROM_BLOCK", "40500000"))
LOG_CHUNK = max(1, min(int(os.environ.get("BASE_LOG_CHUNK", "10000")), 10000))
LOG_WORKERS = max(1, min(int(os.environ.get("BASE_LOG_WORKERS", "8")), 16))
RPC_BATCH_LIMIT = max(1, min(int(os.environ.get("BASE_RPC_BATCH_LIMIT", "10")), 10))

AUCTION_HOUSE = "0x8F34fe11ce28893DEA6A802c8d0b3d0FFC7f5CeA"
DEGEN_DOGS = "0x09154248fFDbaF8aA877aE8A4bf8cE1503596428"
WOOF = "0x3e5c4FA0cAA794516eD0DF77f31daA534918d492"
SUP = "0xa69f80524381275A7fFdb3AE01c54150644c8792"
ZERO = "0x0000000000000000000000000000000000000000"
OPENSEA_ITEM_BASE = "https://opensea.io/item/base"
OPENSEA_COLLECTION_URL = "https://opensea.io/collection/degen-dogs-club"


def dog_opensea_url(token_id: int | str) -> str:
    return f"{OPENSEA_ITEM_BASE}/{DEGEN_DOGS.lower()}/{int(token_id)}"


def opensea_trait_url(trait_type: str, trait_value: str) -> str:
    payload = json.dumps(
        [{"traitType": str(trait_type), "values": [str(trait_value)]}],
        separators=(",", ":"),
    )
    encoded = urllib.parse.quote(payload, safe="[]{}:,")
    return f"{OPENSEA_COLLECTION_URL}?traits={encoded}"

# Rewards snapshot supplied by Ael for a 141-Dog wallet. WOOF Vault Bonus is
# intentionally excluded so the per-Dog reward estimate reflects only the base
# WOOF stream and SUP stream a new bidder should reason about.
REWARD_DOG_COUNT = Decimal("141")
REWARD_WOOF_RECEIVED = Decimal("2750407020.46")
REWARD_WOOF_FLOW_PER_DAY = Decimal("22327617.40")
REWARD_SUP_RECEIVED = Decimal("36935.51")
REWARD_SUP_FLOW_PER_DAY = Decimal("379.01")

OUTPUT_TABLES = [
    "mission3_metrics",
    "auction_feed",
    "historical_dog_search",
    "historical_dog_report",
    "current_latest_bid",
    "recent_auction_winners",
    "current_auction",
    "auction_timeline",
    "auction_daily_activity",
    "auction_bidder_leaderboard",
    "season5_sup_by_winner",
    "season5_sup_rewards_by_auction",
    "auction_winners",
    "recent_bids",
    "top_woof_holders",
]
PRIMARY_TABLES = ["auction_feed"]

DATASET_DESCRIPTIONS = {
    "mission3_metrics": "Key dashboard metrics, refresh metadata, and verified contract snapshot values.",
    "auction_feed": "Homepage-ready current auction plus recent settled auctions.",
    "historical_dog_search": "Combined all-mission Dog lookup with one hosted row per current Dog token ID and searchable hidden metadata.",
    "historical_dog_report": "Mission-level coverage report for the combined historical Dog lookup.",
    "current_latest_bid": "Current auction latest bid and high-bidder snapshot.",
    "recent_auction_winners": "Recent settled winners formatted for the homepage.",
    "current_auction": "Full current auction state, dog metadata, rarity, and countdown fields.",
    "auction_timeline": "One row per auction with bid, winner, and settlement summary.",
    "auction_daily_activity": "Daily auction counts, settlement counts, and bid/settlement volume.",
    "auction_bidder_leaderboard": "Ranked bidder activity across decoded auction events.",
    "season5_sup_by_winner": "Estimated Season 5 SUP rewards grouped by winning wallet/profile.",
    "season5_sup_rewards_by_auction": "Estimated Season 5 SUP rewards per auction.",
    "auction_winners": "Settled auction winners with bid values and identity fields.",
    "recent_bids": "Latest bid events decoded from the auction house.",
    "top_woof_holders": "WOOF holder snapshot from transfer participants and balance checks.",
}

CONFIGURATION_ENV_VARS = [
    ("BASE_RPC_URL", "Single Base RPC endpoint for contract calls; also overrides log RPC lists when set."),
    ("BASE_RPC_URLS", "Comma-separated fallback Base RPC endpoints for contract calls."),
    ("BASE_LOG_RPC_URLS", "Comma-separated Base RPC endpoints used for `eth_getLogs` history scans."),
    ("BASE_FROM_BLOCK", "First Base block scanned for Mission 3 logs; defaults to the known Mission 3 start range."),
    ("BASE_LOG_CHUNK", "Maximum block range per log request, capped at 10,000 for public Base RPC compatibility."),
    ("BASE_LOG_WORKERS", "Concurrent log-fetch workers, capped by the builder to avoid public RPC overload."),
    ("BASE_RPC_BATCH_LIMIT", "Maximum JSON-RPC batch size for balance/metadata calls, capped at 10."),
    ("DOG_METADATA_WORKERS", "Concurrent Dog metadata fetch workers, capped by the builder."),
    ("NEYNAR_API_KEY", "Optional Neynar API key for Farcaster identity resolution."),
    ("WOOF_USD_PRICE", "Optional manual WOOF/USD override; otherwise fetched from Dexscreener Base pools."),
    ("SUP_USD_PRICE", "Optional manual SUP/USD override; otherwise fetched from Dexscreener Base pools."),
]


TOPIC_AUCTION_BID = "0x1159164c56f277e6fc99c11731bd380e0347deb969b75523398734c252706ea3"
TOPIC_AUCTION_CREATED = "0xd6eddd1118d71820909c1197aa966dbc15ed6f508554252169cc3d5ccac756ca"
TOPIC_AUCTION_SETTLED = "0xc9f72b276a388619c6d185d146697036241880c36654b1a3ffdad07c24038d99"
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

SELECTOR_NAME = "0x06fdde03"
SELECTOR_SYMBOL = "0x95d89b41"
SELECTOR_DECIMALS = "0x313ce567"
SELECTOR_TOTAL_SUPPLY = "0x18160ddd"
SELECTOR_AUCTION = "0x7d9f6db5"
SELECTOR_BALANCE_OF = "0x70a08231"
SELECTOR_TOKEN_URI = "0xc87b56dd"


def post_json(payload: dict[str, Any] | list[dict[str, Any]], timeout: int, url: str) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "degen-dogs-mission3-builder/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail[:500] or f"HTTP {exc.code}") from exc
    return json.loads(text)


def rpc(method: str, params: list[Any], timeout: int = 60, urls: list[str] | None = None) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    active_urls = urls or RPC_URLS
    last: Exception | None = None
    for attempt in range(5):
        try:
            data = post_json(payload, timeout, active_urls[attempt % len(active_urls)])
            if "error" in data:
                raise RuntimeError(json.dumps(data["error"], sort_keys=True))
            return data["result"]
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 4:
                raise
            time.sleep(0.75 * (attempt + 1))
    raise RuntimeError(last)


def rpc_batch(calls: list[tuple[str, list[Any]]], timeout: int = 120, urls: list[str] | None = None) -> list[Any]:
    if not calls:
        return []
    active_urls = urls or RPC_URLS
    if len(calls) > RPC_BATCH_LIMIT:
        out: list[Any] = []
        for i in range(0, len(calls), RPC_BATCH_LIMIT):
            out.extend(rpc_batch(calls[i : i + RPC_BATCH_LIMIT], timeout=timeout, urls=active_urls))
        return out
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    for attempt in range(5):
        try:
            items = post_json(payload, timeout, active_urls[attempt % len(active_urls)])
            if not isinstance(items, list):
                raise RuntimeError(f"Unexpected batch response: {items!r}")
            by_id = {item.get("id"): item for item in items if isinstance(item, dict)}
            out = []
            for i in range(len(calls)):
                item = by_id.get(i)
                if not item or "error" in item:
                    method, params = calls[i]
                    out.append(rpc(method, params, timeout=timeout, urls=active_urls))
                else:
                    out.append(item.get("result"))
            return out
        except Exception:
            if attempt == 4:
                raise
            time.sleep(0.75 * (attempt + 1))
    return []


def log_filter(address: str, topic_filter: str | list[str], start: int, end: int) -> dict[str, Any]:
    return {"address": address, "fromBlock": hex(start), "toBlock": hex(end), "topics": [topic_filter]}


def fetch_logs(address: str, topics: str | list[str], from_block: int, to_block: int) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    topic_filter: str | list[str] = topics
    ranges: list[tuple[int, int]] = []
    start = from_block
    while start <= to_block:
        end = min(to_block, start + LOG_CHUNK - 1)
        ranges.append((start, end))
        start = end + 1

    def fetch_range(bounds: tuple[int, int]) -> list[dict[str, Any]]:
        a, b = bounds
        return rpc("eth_getLogs", [log_filter(address, topic_filter, a, b)], timeout=120, urls=LOG_RPC_URLS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=LOG_WORKERS) as pool:
        futures = [pool.submit(fetch_range, bounds) for bounds in ranges]
        for future in concurrent.futures.as_completed(futures):
            logs.extend(future.result())

    logs.sort(key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)))
    return logs


def word(data: str, idx: int) -> int:
    clean = data[2:] if data.startswith("0x") else data
    return int(clean[idx * 64 : (idx + 1) * 64] or "0", 16)


def word_address(data: str, idx: int) -> str:
    return "0x" + f"{word(data, idx):064x}"[-40:]


def topic_uint(topic: str) -> int:
    return int(topic, 16)


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:]


def utc_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def decimal_str(value: int, decimals: int, max_places: int = 18) -> str:
    q = Decimal(value) / (Decimal(10) ** decimals)
    s = f"{q:.{max_places}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def fetch_block_times(blocks: set[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    ordered = sorted(blocks)
    for i in range(0, len(ordered), 100):
        batch = ordered[i : i + 100]
        calls = [("eth_getBlockByNumber", [hex(block), False]) for block in batch]
        results = rpc_batch(calls)
        for block, result in zip(batch, results):
            if result:
                out[block] = utc_from_unix(int(result["timestamp"], 16))
    return out


def eth_call(to: str, data: str, block_tag: str = "latest") -> str:
    return rpc("eth_call", [{"to": to, "data": data}, block_tag])


def decode_abi_string(raw: str) -> str:
    if not raw or raw == "0x":
        return ""
    clean = raw[2:] if raw.startswith("0x") else raw
    if len(clean) < 128:
        try:
            return bytes.fromhex(clean.rstrip("0")).decode("utf-8", errors="ignore").strip("\x00")
        except Exception:
            return ""
    offset = int(clean[:64], 16) * 2
    length = int(clean[offset : offset + 64], 16)
    data = clean[offset + 64 : offset + 64 + length * 2]
    return bytes.fromhex(data).decode("utf-8", errors="ignore")


def decode_uint_call(raw: str) -> int:
    return int(raw, 16) if raw and raw != "0x" else 0


def fetch_eth_usd_price() -> tuple[Decimal, str]:
    endpoints = [
        ("coinbase", "https://api.coinbase.com/v2/prices/ETH-USD/spot", lambda data: data["data"]["amount"]),
        ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd", lambda data: data["ethereum"]["usd"]),
    ]
    for source, url, picker in endpoints:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-dogs-mission3-builder/1.0"})
            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            price = Decimal(str(picker(data)))
            if price > 0:
                return price, source
        except Exception as exc:  # noqa: BLE001
            print(f"warning: ETH/USD lookup failed via {source}: {exc}", file=sys.stderr)
    return Decimal(0), "unavailable"


def decimal_value_str(value: Decimal, max_places: int = 6) -> str:
    s = f"{value:.{max_places}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def configured_price(symbol: str) -> tuple[Decimal, str] | None:
    env_name = f"{symbol.upper()}_USD_PRICE"
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        price = Decimal(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"warning: invalid {env_name}: {exc}", file=sys.stderr)
        return None
    if price > 0:
        return price, f"env:{env_name}"
    return None


def fetch_token_usd_price(symbol: str, token_address: str) -> tuple[Decimal, str]:
    configured = configured_price(symbol)
    if configured:
        return configured

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "degen-dogs-mission3-builder/1.0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        candidates = []
        token_lower = token_address.lower()
        for pair in data.get("pairs") or []:
            if str(pair.get("chainId", "")).lower() != "base":
                continue
            base_token = pair.get("baseToken") or {}
            if str(base_token.get("address") or "").lower() != token_lower:
                continue
            try:
                price = Decimal(str(pair.get("priceUsd") or "0"))
                liquidity = Decimal(str((pair.get("liquidity") or {}).get("usd") or "0"))
            except Exception:
                continue
            if price <= 0:
                continue
            candidates.append((liquidity, price, pair))
        if candidates:
            _liquidity, price, pair = max(candidates, key=lambda item: item[0])
            source = f"dexscreener:{pair.get('dexId', 'unknown')}:{pair.get('pairAddress', '')}"
            return price, source
    except Exception as exc:  # noqa: BLE001
        print(f"warning: {symbol}/USD lookup failed via Dexscreener: {exc}", file=sys.stderr)
    return Decimal(0), "unavailable"


def reward_token_stats(woof_usd: Decimal, sup_usd: Decimal) -> dict[str, str]:
    woof_per_dog = REWARD_WOOF_FLOW_PER_DAY / REWARD_DOG_COUNT
    sup_per_dog = REWARD_SUP_FLOW_PER_DAY / REWARD_DOG_COUNT
    woof_flow_usd = REWARD_WOOF_FLOW_PER_DAY * woof_usd
    sup_flow_usd = REWARD_SUP_FLOW_PER_DAY * sup_usd
    woof_per_dog_usd = woof_per_dog * woof_usd
    sup_per_dog_usd = sup_per_dog * sup_usd
    total_flow_usd = woof_flow_usd + sup_flow_usd
    total_per_dog_usd = woof_per_dog_usd + sup_per_dog_usd
    return {
        "reward_basis_dogs": decimal_value_str(REWARD_DOG_COUNT, 0),
        "reward_excludes": "woof_vault_bonus",
        "reward_woof_received": decimal_value_str(REWARD_WOOF_RECEIVED, 2),
        "reward_woof_received_usd": decimal_value_str(REWARD_WOOF_RECEIVED * woof_usd, 2),
        "reward_woof_flow_per_day": decimal_value_str(REWARD_WOOF_FLOW_PER_DAY, 2),
        "reward_woof_flow_usd_per_day": decimal_value_str(woof_flow_usd, 2),
        "reward_woof_per_dog_per_day": decimal_value_str(woof_per_dog, 6),
        "reward_woof_per_dog_usd_per_day": decimal_value_str(woof_per_dog_usd, 6),
        "reward_sup_received": decimal_value_str(REWARD_SUP_RECEIVED, 2),
        "reward_sup_received_usd": decimal_value_str(REWARD_SUP_RECEIVED * sup_usd, 2),
        "reward_sup_flow_per_day": decimal_value_str(REWARD_SUP_FLOW_PER_DAY, 2),
        "reward_sup_flow_usd_per_day": decimal_value_str(sup_flow_usd, 2),
        "reward_sup_per_dog_per_day": decimal_value_str(sup_per_dog, 6),
        "reward_sup_per_dog_usd_per_day": decimal_value_str(sup_per_dog_usd, 6),
        "reward_total_flow_usd_per_day": decimal_value_str(total_flow_usd, 2),
        "reward_total_per_dog_usd_per_day": decimal_value_str(total_per_dog_usd, 6),
    }


def fetch_token_stats(block_tag: str) -> dict[str, str]:
    name = decode_abi_string(eth_call(WOOF, SELECTOR_NAME, block_tag))
    symbol = decode_abi_string(eth_call(WOOF, SELECTOR_SYMBOL, block_tag))
    decimals = decode_uint_call(eth_call(WOOF, SELECTOR_DECIMALS, block_tag))
    supply_raw = decode_uint_call(eth_call(WOOF, SELECTOR_TOTAL_SUPPLY, block_tag))
    eth_usd, eth_usd_source = fetch_eth_usd_price()
    sup_name = decode_abi_string(eth_call(SUP, SELECTOR_NAME, block_tag))
    sup_symbol = decode_abi_string(eth_call(SUP, SELECTOR_SYMBOL, block_tag))
    sup_decimals = decode_uint_call(eth_call(SUP, SELECTOR_DECIMALS, block_tag))
    woof_usd, woof_usd_source = fetch_token_usd_price("WOOF", WOOF)
    sup_usd, sup_usd_source = fetch_token_usd_price("SUP", SUP)
    return {
        "auction_house": AUCTION_HOUSE,
        "dog_nft": DEGEN_DOGS,
        "woof_token": WOOF,
        "woof_name": name,
        "woof_symbol": symbol,
        "woof_decimals": str(decimals),
        "woof_total_supply": decimal_str(supply_raw, decimals, 6),
        "woof_total_supply_raw": str(supply_raw),
        "woof_usd_price": decimal_value_str(woof_usd, 12),
        "woof_usd_source": woof_usd_source,
        "sup_token": SUP,
        "sup_name": sup_name,
        "sup_symbol": sup_symbol,
        "sup_decimals": str(sup_decimals),
        "sup_usd_price": decimal_value_str(sup_usd, 8),
        "sup_usd_source": sup_usd_source,
        "eth_usd_price": decimal_str(int(eth_usd * 100), 2, 2) if eth_usd else "0",
        "eth_usd_source": eth_usd_source,
        **reward_token_stats(woof_usd, sup_usd),
    }


def token_uri_data(token_id: int) -> str:
    return SELECTOR_TOKEN_URI + f"{token_id:x}".rjust(64, "0")


def fetch_dog_total_supply(block_tag: str) -> int:
    return decode_uint_call(eth_call(DEGEN_DOGS, SELECTOR_TOTAL_SUPPLY, block_tag))


def fetch_token_uri(token_id: int, block_tag: str) -> str:
    return decode_abi_string(eth_call(DEGEN_DOGS, token_uri_data(token_id), block_tag))


def normalize_metadata_url(url: str) -> str:
    if url.startswith("ipfs://"):
        return "https://ipfs.io/ipfs/" + url.removeprefix("ipfs://")
    return url


def fetch_url_json(url: str, timeout: int = 45) -> dict[str, Any]:
    req = urllib.request.Request(
        normalize_metadata_url(url),
        headers={"Accept": "application/json", "User-Agent": "degen-dogs-mission3-builder/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def simplified_dog_metadata(token_id: int, data: dict[str, Any]) -> dict[str, Any]:
    attrs = []
    for item in data.get("attributes") or []:
        if not isinstance(item, dict):
            continue
        trait_type = str(item.get("trait_type") or "").strip()
        value = str(item.get("value") or "").strip()
        if trait_type and value:
            attrs.append({"trait_type": trait_type, "value": value})
    image = str(data.get("image") or "")
    if image.startswith("ipfs://"):
        image = normalize_metadata_url(image)
    return {
        "token_id": token_id,
        "name": str(data.get("name") or f"Degen Dog #{token_id}"),
        "image_url": image,
        "external_url": str(data.get("external_url") or f"https://degendogs.club/#dog{token_id}"),
        "attributes": attrs,
    }


def load_dog_cache() -> dict[str, Any]:
    if not DOG_METADATA_CACHE.exists():
        return {}
    try:
        data = json.loads(DOG_METADATA_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_dog_cache(cache: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    DOG_METADATA_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_one_dog_metadata(token_id: int, block_tag: str) -> dict[str, Any]:
    url = f"https://degendogs.club/meta/{token_id}"
    try:
        return simplified_dog_metadata(token_id, fetch_url_json(url))
    except Exception:
        uri = fetch_token_uri(token_id, block_tag)
        return simplified_dog_metadata(token_id, fetch_url_json(uri))


def fetch_dog_metadata_rows(total_supply: int, block_tag: str) -> list[dict[str, Any]]:
    cache = load_dog_cache()
    token_ids = list(range(total_supply))
    missing = [token_id for token_id in token_ids if str(token_id) not in cache]
    if missing:
        workers = max(1, min(int(os.environ.get("DOG_METADATA_WORKERS", "16")), 24))
        print(f"fetching dog metadata: {len(missing)} missing of {total_supply}", file=sys.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one_dog_metadata, token_id, block_tag): token_id for token_id in missing}
            for future in concurrent.futures.as_completed(futures):
                token_id = futures[future]
                try:
                    cache[str(token_id)] = future.result()
                except Exception as exc:  # noqa: BLE001
                    print(f"warning: metadata failed for dog {token_id}: {exc}", file=sys.stderr)
                    cache[str(token_id)] = simplified_dog_metadata(token_id, {})
        write_dog_cache(cache)

    metadata = []
    for token_id in token_ids:
        row = cache.get(str(token_id)) or simplified_dog_metadata(token_id, {})
        row["token_id"] = int(row.get("token_id") or token_id)
        metadata.append(row)

    trait_counts: Counter[tuple[str, str]] = Counter()
    for row in metadata:
        for attr in row.get("attributes") or []:
            trait_counts[(str(attr.get("trait_type") or ""), str(attr.get("value") or ""))] += 1

    score_by_token: dict[int, float] = {}
    for row in metadata:
        token_id = int(row["token_id"])
        score = 0.0
        for attr in row.get("attributes") or []:
            key = (str(attr.get("trait_type") or ""), str(attr.get("value") or ""))
            count = max(1, trait_counts.get(key, 1))
            score += total_supply / count
        score_by_token[token_id] = score
    ranks = {token_id: rank for rank, token_id in enumerate(sorted(score_by_token, key=lambda tid: (-score_by_token[tid], tid)), start=1)}

    rows: list[dict[str, Any]] = []
    for row in metadata:
        token_id = int(row["token_id"])
        attrs = row.get("attributes") or []
        traits = []
        rarity_items = []
        for attr in attrs:
            trait_type = str(attr.get("trait_type") or "")
            value = str(attr.get("value") or "")
            if not trait_type or not value:
                continue
            count = trait_counts[(trait_type, value)]
            pct = (Decimal(count) * Decimal(100)) / Decimal(total_supply) if total_supply else Decimal(0)
            traits.append(f"{trait_type}: {value}")
            rarity_items.append(f"{trait_type}: {value} ({pct:.1f}%)")
        rows.append(
            {
                "token_id": token_id,
                "dog_name": row.get("name") or f"Degen Dog #{token_id}",
                "dog_image_url": row.get("image_url") or "",
                "dog_external_url": row.get("external_url") or f"https://degendogs.club/#dog{token_id}",
                "dog_opensea_url": dog_opensea_url(token_id),
                "traits": "; ".join(traits),
                "trait_rarity": "; ".join(rarity_items),
                "rarity": f"#{ranks.get(token_id, 0)}/{total_supply}",
                "rarity_score": round(score_by_token.get(token_id, 0.0), 6),
            }
        )
    rows.sort(key=lambda item: item["token_id"])
    return rows


def fetch_current_auction(latest_block: int, latest_time: str, block_tag: str) -> dict[str, Any]:
    raw = eth_call(AUCTION_HOUSE, SELECTOR_AUCTION, block_tag)
    token_id = word(raw, 0)
    amount = word(raw, 1)
    start_ts = word(raw, 2)
    end_ts = word(raw, 3)
    bidder = word_address(raw, 4)
    settled = word(raw, 5)
    return {
        "token_id": token_id,
        "amount_eth": float(Decimal(amount) / Decimal(10**18)),
        "amount_wei": str(amount),
        "start_time_utc": utc_from_unix(start_ts) if start_ts else "",
        "end_time_utc": utc_from_unix(end_ts) if end_ts else "",
        "bidder": bidder,
        "settled": int(settled),
        "latest_block": latest_block,
        "latest_block_time_utc": latest_time,
    }

def load_neynar_api_key() -> str | None:
    if os.environ.get("NEYNAR_API_KEY"):
        return os.environ["NEYNAR_API_KEY"]
    candidates = [
        Path.home() / ".hermes" / "skills" / "openclaw-imports" / "neynar" / "config.json",
        Path.home() / ".clawdbot" / "skills" / "neynar" / "config.json",
    ]
    for config_path in candidates:
        if not config_path.exists():
            continue
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            key = data.get("apiKey") or data.get("api_key")
            if key:
                return str(key)
        except Exception:
            continue
    return None


def normalize_address(address: str | None) -> str:
    if not address:
        return ""
    text = str(address).strip().lower()
    return text if text.startswith("0x") and len(text) == 42 else ""


def short_address(address: str) -> str:
    normalized = normalize_address(address)
    if not normalized:
        return ""
    return f"{normalized[:6]}…{normalized[-4:]}"


def basescan_address_url(address: str | None) -> str:
    normalized = normalize_address(address)
    if not normalized or normalized == ZERO:
        return ""
    return f"https://basescan.org/address/{normalized}"


def collect_identity_addresses(
    current: dict[str, Any],
    bids: list[dict[str, Any]],
    settled: list[dict[str, Any]],
    holders: list[dict[str, Any]],
) -> list[str]:
    addresses: set[str] = set()
    for value in [current.get("bidder")]:
        normalized = normalize_address(value)
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    for row in bids:
        normalized = normalize_address(row.get("bidder"))
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    for row in settled:
        normalized = normalize_address(row.get("winner"))
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    for row in holders[:100]:
        normalized = normalize_address(row.get("address"))
        if normalized and normalized != ZERO:
            addresses.add(normalized)
    return sorted(addresses)


def pick_farcaster_user(address: str, users: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not users:
        return None
    address_lc = normalize_address(address)

    def score(user: dict[str, Any]) -> tuple[int, int, int]:
        verified = [normalize_address(a) for a in user.get("verifications", [])]
        primary = normalize_address((user.get("verified_addresses") or {}).get("primary", {}).get("eth_address"))
        eth_addresses = [normalize_address(a) for a in (user.get("verified_addresses") or {}).get("eth_addresses", [])]
        is_primary = int(primary == address_lc)
        is_verified = int(address_lc in verified or address_lc in eth_addresses)
        followers = int(user.get("follower_count") or 0)
        return (is_primary, is_verified, followers)

    return max(users, key=score)


def fetch_farcaster_profiles(addresses: list[str]) -> list[dict[str, Any]]:
    api_key = load_neynar_api_key()
    rows: list[dict[str, Any]] = []
    if not api_key or not addresses:
        return rows
    chunk_size = 100
    for i in range(0, len(addresses), chunk_size):
        chunk = addresses[i : i + chunk_size]
        query = ",".join(chunk)
        url = "https://api.neynar.com/v2/farcaster/user/bulk-by-address?" + urllib.parse.urlencode({"addresses": query})
        last: Exception | None = None
        data: dict[str, Any] | None = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"accept": "application/json", "x-api-key": api_key})
                with urllib.request.urlopen(req, timeout=45) as response:
                    data = json.loads(response.read().decode("utf-8"))
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt == 3:
                    print(f"warning: Neynar wallet lookup failed for {len(chunk)} addresses: {last}", file=sys.stderr)
                    data = {}
                    break
                time.sleep(1.5 * (attempt + 1))
        if not data:
            continue
        for address in chunk:
            users = data.get(address) or data.get(address.lower()) or data.get(address.upper()) or []
            user = pick_farcaster_user(address, users)
            if not user:
                continue
            rows.append(
                {
                    "address": address.lower(),
                    "fid": int(user.get("fid") or 0),
                    "username": str(user.get("username") or "").lstrip("@"),
                    "display_name": str(user.get("display_name") or ""),
                    "pfp_url": str(user.get("pfp_url") or ""),
                }
            )
    rows.sort(key=lambda row: row["address"])
    return rows


def fetch_degendogs_auction_profiles(current: dict[str, Any]) -> list[dict[str, Any]]:
    """Fallback identity source used by the live Degen Dogs miniapp.

    Neynar only resolves addresses that are currently indexed as Farcaster custody
    or verified addresses. The official auction API also returns the username used
    by the miniapp for the current bidder, so use it to link the current high-bid
    wallet when Neynar has no match.
    """
    current_token_id = int(current.get("token_id") or 0)
    current_bidder = normalize_address(current.get("bidder"))
    if not current_token_id or not current_bidder or current_bidder == ZERO:
        return []
    url = "https://degendogs.club/api/auctionData"
    try:
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "degen-dogs-mission3-builder/1.0"})
        with urllib.request.urlopen(req, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: Degen Dogs auction identity lookup failed: {exc}", file=sys.stderr)
        return []

    try:
        api_token_id = int(data.get("nounId") or 0)
    except (TypeError, ValueError):
        return []
    if api_token_id != current_token_id:
        return []
    api_bidder = normalize_address(data.get("bidder"))
    if api_bidder != current_bidder:
        return []
    api_amount = data.get("amount")
    if api_amount is not None:
        try:
            if abs(Decimal(str(api_amount)) - Decimal(str(current.get("amount_eth") or 0))) > Decimal("0.000000000001"):
                return []
        except Exception:
            return []

    rows: list[dict[str, Any]] = []
    for bid in data.get("bids") or []:
        try:
            bid_token_id = int(bid.get("nounId") or api_token_id)
        except (TypeError, ValueError):
            continue
        if bid_token_id != current_token_id:
            continue
        bidder = normalize_address(bid.get("bidder"))
        username = str(bid.get("username") or "").strip().lstrip("@")
        if bidder != current_bidder or not username:
            continue
        rows.append(
            {
                "address": bidder.lower(),
                "fid": 0,
                "username": username,
                "display_name": username,
                "pfp_url": str(bid.get("pfp_url") or ""),
            }
        )
    rows.sort(key=lambda row: row["address"])
    return rows


def merge_farcaster_profiles(*sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for rows in sources:
        for row in rows:
            address = normalize_address(row.get("address"))
            if not address:
                continue
            key = address.lower()
            normalized = {
                "address": key,
                "fid": int(row.get("fid") or 0),
                "username": str(row.get("username") or "").strip().lstrip("@"),
                "display_name": str(row.get("display_name") or ""),
                "pfp_url": str(row.get("pfp_url") or ""),
            }
            existing = merged.get(key)
            if not existing:
                merged[key] = normalized
                continue
            for field in ["fid", "username", "display_name", "pfp_url"]:
                if not existing.get(field) and normalized.get(field):
                    existing[field] = normalized[field]
    return [merged[key] for key in sorted(merged)]


def decode_auction_logs(created_logs: list[dict[str, Any]], bid_logs: list[dict[str, Any]], settled_logs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    blocks = {int(log["blockNumber"], 16) for log in bid_logs + settled_logs}
    block_times = fetch_block_times(blocks)

    created = []
    for log in created_logs:
        token_id = topic_uint(log["topics"][1])
        created.append(
            {
                "token_id": token_id,
                "start_time_utc": utc_from_unix(word(log["data"], 0)),
                "end_time_utc": utc_from_unix(word(log["data"], 1)),
                "block_number": int(log["blockNumber"], 16),
                "tx_hash": log["transactionHash"],
            }
        )

    bids = []
    for log in bid_logs:
        block = int(log["blockNumber"], 16)
        value = word(log["data"], 1)
        bids.append(
            {
                "token_id": topic_uint(log["topics"][1]),
                "bidder": word_address(log["data"], 0),
                "bid_eth": float(Decimal(value) / Decimal(10**18)),
                "bid_wei": str(value),
                "extended": int(word(log["data"], 2)),
                "block_number": block,
                "tx_hash": log["transactionHash"],
                "log_index": int(log["logIndex"], 16),
                "block_time_utc": block_times.get(block, ""),
            }
        )

    settled = []
    for log in settled_logs:
        block = int(log["blockNumber"], 16)
        amount = word(log["data"], 1)
        settled.append(
            {
                "token_id": topic_uint(log["topics"][1]),
                "winner": word_address(log["data"], 0),
                "amount_eth": float(Decimal(amount) / Decimal(10**18)),
                "amount_wei": str(amount),
                "block_number": block,
                "tx_hash": log["transactionHash"],
                "log_index": int(log["logIndex"], 16),
                "block_time_utc": block_times.get(block, ""),
            }
        )

    return created, bids, settled


def fetch_woof_holders(transfer_logs: list[dict[str, Any]], decimals: int, block_tag: str) -> list[dict[str, Any]]:
    addresses: set[str] = set()
    for log in transfer_logs:
        if len(log.get("topics", [])) >= 3:
            a = topic_address(log["topics"][1])
            b = topic_address(log["topics"][2])
            if a.lower() != ZERO:
                addresses.add(a)
            if b.lower() != ZERO:
                addresses.add(b)

    ordered = sorted(addresses, key=str.lower)
    rows: list[dict[str, Any]] = []
    sig = SELECTOR_BALANCE_OF
    for i in range(0, len(ordered), RPC_BATCH_LIMIT):
        batch = ordered[i : i + RPC_BATCH_LIMIT]
        calls = []
        for address in batch:
            data = sig + address.lower().replace("0x", "").rjust(64, "0")
            calls.append(("eth_call", [{"to": WOOF, "data": data}, block_tag]))
        results = rpc_batch(calls)
        for address, raw in zip(batch, results):
            balance = int(raw, 16) if raw else 0
            rows.append(
                {
                    "address": address,
                    "balance_woof": float(Decimal(balance) / (Decimal(10) ** decimals)),
                    "balance_raw": str(balance),
                }
            )
    rows.sort(key=lambda r: (-r["balance_woof"], r["address"].lower()))
    return rows


def quote_ident(name: str) -> str:
    if not name or name[0].isdigit() or any(not (ch.isalnum() or ch == "_") for ch in name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def insert_rows(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]], schema: list[tuple[str, str]]) -> None:
    cols = [c for c, _ in schema]
    q_table = quote_ident(table)
    ddl_cols = []
    for col, typ in schema:
        if typ not in {"INTEGER", "REAL", "TEXT"}:
            raise ValueError(f"Invalid SQLite type: {typ!r}")
        ddl_cols.append(f"{quote_ident(col)} {typ}")
    q_cols = [quote_ident(col) for col in cols]
    drop_sql = f"DROP TABLE IF EXISTS {q_table}"
    create_sql = f"CREATE TABLE {q_table} ({', '.join(ddl_cols)})"
    conn.execute(drop_sql)
    conn.execute(create_sql)
    if not rows:
        return
    placeholders = ",".join("?" for _ in cols)
    insert_sql = f"INSERT INTO {q_table} ({', '.join(q_cols)}) VALUES ({placeholders})"
    conn.executemany(insert_sql, [[row.get(col) for col in cols] for row in rows])


def fetch_table(conn: sqlite3.Connection, table: str) -> tuple[list[str], list[tuple[Any, ...]]]:
    select_sql = f"SELECT * FROM {quote_ident(table)}"
    cur = conn.execute(select_sql)
    cols = [d[0] for d in cur.description]
    return cols, cur.fetchall()


def write_csv(path: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)


def write_json(path: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [dict(zip(cols, row)) for row in rows]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def table_dicts(cols: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(cols, row)) for row in rows]


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


def int_value(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def is_settled_status(value: Any) -> bool:
    status = text_value(value).lower()
    return status == "settled" or (status.startswith("settled") and "unsettled" not in status)


def load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def chain_address_url(mission: int, address: str | None) -> str:
    normalized = normalize_address(address)
    if not normalized or normalized == ZERO:
        return ""
    if mission == 1:
        return f"https://polygonscan.com/address/{normalized}"
    if mission == 2:
        return f"https://explorer.degen.tips/address/{normalized}"
    return basescan_address_url(normalized)


def archive_amount(row: dict[str, Any], mission: int) -> str:
    if mission == 1:
        amount = first_text(row.get("amount_display_weth"), row.get("amount_weth"))
        return f"{amount} WETH" if amount else ""
    if mission == 2:
        amount = first_text(row.get("amount_degen"), row.get("amount_display_native"))
        return f"{amount} DEGEN" if amount else ""
    amount = first_text(row.get("amount_eth"), row.get("settled_amount_eth"))
    return f"{amount} ETH" if amount else ""


def archive_status(row: dict[str, Any]) -> str:
    status = first_text(row.get("auction_status"), row.get("auction_state"), row.get("status"))
    if status:
        return status
    settled = row.get("settled")
    if settled is True or text_value(settled).lower() in {"1", "true", "yes"} or row.get("settled_block"):
        return "settled"
    if settled is False or text_value(settled).lower() in {"0", "false", "no"}:
        return "live_or_unsettled"
    if row:
        return "recovered"
    return "metadata_only"


def load_archive_lookup() -> tuple[dict[int, dict[str, Any]], int, int]:
    lookup: dict[int, dict[str, Any]] = {}
    mission1_max = 200
    mission3_min = 590
    for mission, path in HISTORICAL_ARCHIVE_INDEXES.items():
        rows = load_json_list(path)
        token_ids: list[int] = []
        for row in rows:
            token_id = int_value(row.get("token_id", row.get("dog_id")), -1)
            if token_id < 0:
                continue
            token_ids.append(token_id)
            enriched = dict(row)
            enriched["_archive_mission"] = mission
            lookup[token_id] = enriched
        if mission == 1 and token_ids:
            mission1_max = max(token_ids)
        if mission == 3 and token_ids:
            mission3_min = min(token_ids)
    return lookup, mission1_max, mission3_min


def mission_for_token(token_id: int, archive: dict[str, Any], mission1_max: int, mission3_min: int) -> int:
    archived_mission = int_value(archive.get("_archive_mission"), 0)
    if archived_mission in MISSION_CHAIN:
        return archived_mission
    if token_id <= mission1_max:
        return 1
    if token_id < mission3_min:
        return 2
    return 3


def source_text(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(text_value(item) for item in value if text_value(item))
    return text_value(value)


def build_search_text(row: dict[str, Any]) -> str:
    return " ".join(
        text_value(value)
        for value in row.values()
        if value is not None and not isinstance(value, (list, dict)) and text_value(value)
    )


def build_historical_dog_tables(
    conn: sqlite3.Connection,
    total_supply: int,
    dog_metadata: list[dict[str, Any]],
) -> None:
    metadata_by_token = {int_value(row.get("token_id"), -1): row for row in dog_metadata if int_value(row.get("token_id"), -1) >= 0}
    archive_lookup, mission1_max, mission3_min = load_archive_lookup()
    timeline_cols, timeline_rows = fetch_table(conn, "auction_timeline")
    winners_cols, winners_rows = fetch_table(conn, "auction_winners")
    current_cols, current_rows = fetch_table(conn, "current_auction")
    timeline_by_token = {int_value(row.get("token_id"), -1): row for row in table_dicts(timeline_cols, timeline_rows)}
    winners_by_token = {int_value(row.get("token_id"), -1): row for row in table_dicts(winners_cols, winners_rows)}
    current_by_token = {int_value(row.get("token_id"), -1): row for row in table_dicts(current_cols, current_rows)}

    search_rows: list[dict[str, Any]] = []
    for token_id in range(total_supply):
        metadata = metadata_by_token.get(token_id, {})
        archive = archive_lookup.get(token_id, {})
        mission = mission_for_token(token_id, archive, mission1_max, mission3_min)
        chain, chain_id = MISSION_CHAIN[mission]
        timeline = timeline_by_token.get(token_id, {}) if mission == 3 else {}
        winner = winners_by_token.get(token_id, {}) if mission == 3 else {}
        current = current_by_token.get(token_id, {}) if mission == 3 else {}

        dog_label = f"Dog #{token_id}"
        image_url = first_text(metadata.get("dog_image_url"), timeline.get("dog_image_url"), winner.get("dog_image_url"))
        external_url = first_text(metadata.get("dog_external_url"), f"https://degendogs.club/#dog{token_id}")
        opensea_url = first_text(metadata.get("dog_opensea_url"), winner.get("dog_opensea_url"), dog_opensea_url(token_id))
        traits = first_text(metadata.get("traits"), winner.get("traits"))
        trait_rarity = first_text(metadata.get("trait_rarity"), winner.get("trait_rarity"))
        rarity = first_text(metadata.get("rarity"), timeline.get("rarity"), winner.get("rarity"))

        if mission == 3 and (timeline or winner or current):
            status = first_text(current.get("auction_state"), timeline.get("auction_state"), archive_status(archive))
            amount = first_text(current.get("current_bid"), winner.get("winning_bid"))
            if not amount:
                settled_eth = first_text(timeline.get("settled_eth"), archive.get("amount_eth"))
                high_bid_eth = first_text(timeline.get("high_bid_eth"))
                amount = f"{settled_eth or high_bid_eth} ETH" if (settled_eth or high_bid_eth) else ""
            winner_label = first_text(winner.get("winner"), current.get("bidder"), timeline.get("winner"), timeline.get("latest_bidder"), archive.get("winner"))
            winner_url = first_text(winner.get("winner_url"), current.get("bidder_url"), timeline.get("winner_url"), timeline.get("latest_bidder_url"))
            winner_wallet = first_text(winner.get("winner_wallet"), current.get("bidder_wallet"))
            if not winner_url and winner_wallet:
                winner_url = chain_address_url(mission, winner_wallet)
            bid_count = int_value(first_text(timeline.get("bids"), winner.get("bid_count"), archive.get("bid_count")))
            unique_bidder_count = int_value(first_text(timeline.get("unique_bidders"), winner.get("unique_bidders"), archive.get("unique_bidder_count")))
            created_utc = first_text(timeline.get("start_time_utc"), archive.get("auction_created_time_utc"))
            settled_utc = first_text(winner.get("settled_time_utc"), timeline.get("settled_time_utc"), archive.get("settled_time_utc"))
            confidence = first_text(archive.get("confidence"), "verified_live_base_logs")
            sources = source_text(archive.get("sources")) or "base_logs,dashboard_builder"
        else:
            status = archive_status(archive)
            amount = archive_amount(archive, mission)
            raw_winner = first_text(archive.get("winner"))
            winner_wallet = normalize_address(raw_winner)
            winner_label = short_address(winner_wallet) if winner_wallet else raw_winner
            winner_url = chain_address_url(mission, winner_wallet) if winner_wallet else ""
            bid_count = int_value(archive.get("bid_count"))
            unique_bidder_count = int_value(archive.get("unique_bidder_count"))
            created_utc = first_text(archive.get("auction_created_time_utc"), archive.get("mint_time_utc"))
            settled_utc = first_text(archive.get("settled_time_utc"))
            confidence = first_text(archive.get("confidence"), "metadata_only")
            sources = source_text(archive.get("sources")) or "dog_metadata"

        raw_amount = first_text(archive.get("amount_raw"), archive.get("amount_wei"))
        row = {
            "mission": mission,
            "chain": chain,
            "chain_id": chain_id,
            "token_id": token_id,
            "dog": dog_label,
            "dog_image_url": image_url,
            "dog_external_url": external_url,
            "dog_opensea_url": opensea_url,
            "status": status,
            "winner": winner_label,
            "winner_url": winner_url,
            "winner_wallet": winner_wallet,
            "amount": amount,
            "amount_raw": raw_amount,
            "bid_count": bid_count,
            "unique_bidder_count": unique_bidder_count,
            "auction_created_time_utc": created_utc,
            "settled_time_utc": settled_utc,
            "rarity": rarity,
            "traits": traits,
            "trait_rarity": trait_rarity,
            "confidence": confidence,
            "sources": sources,
        }
        row["search_text"] = build_search_text(row)
        search_rows.append(row)

    search_schema = [
        ("mission", "INTEGER"),
        ("chain", "TEXT"),
        ("chain_id", "INTEGER"),
        ("token_id", "INTEGER"),
        ("dog", "TEXT"),
        ("dog_image_url", "TEXT"),
        ("dog_external_url", "TEXT"),
        ("dog_opensea_url", "TEXT"),
        ("status", "TEXT"),
        ("winner", "TEXT"),
        ("winner_url", "TEXT"),
        ("winner_wallet", "TEXT"),
        ("amount", "TEXT"),
        ("amount_raw", "TEXT"),
        ("bid_count", "INTEGER"),
        ("unique_bidder_count", "INTEGER"),
        ("auction_created_time_utc", "TEXT"),
        ("settled_time_utc", "TEXT"),
        ("rarity", "TEXT"),
        ("traits", "TEXT"),
        ("trait_rarity", "TEXT"),
        ("confidence", "TEXT"),
        ("sources", "TEXT"),
        ("search_text", "TEXT"),
    ]
    insert_rows(conn, "historical_dog_search", search_rows, search_schema)

    def report_row(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        statuses = [text_value(row.get("status")).lower() for row in rows]
        created_times = [text_value(row.get("auction_created_time_utc")) for row in rows if text_value(row.get("auction_created_time_utc"))]
        activity_times = [
            text_value(value)
            for row in rows
            for value in (row.get("settled_time_utc"), row.get("auction_created_time_utc"))
            if text_value(value)
        ]
        winners = {text_value(row.get("winner_wallet") or row.get("winner")) for row in rows if text_value(row.get("winner_wallet") or row.get("winner"))}
        mission_int = int_value(label, 0)
        chain_name = "All missions" if label == "all" else MISSION_CHAIN.get(mission_int, ("", 0))[0]
        return {
            "mission": label,
            "chain": chain_name,
            "dogs": len(rows),
            "auctions_or_records": sum(1 for row in rows if text_value(row.get("auction_created_time_utc")) or text_value(row.get("settled_time_utc")) or text_value(row.get("amount"))),
            "settled": sum(1 for status in statuses if is_settled_status(status)),
            "live_or_unsettled": sum(1 for status in statuses if "live" in status or "ongoing" in status or "unsettled" in status or "created" in status),
            "metadata_only": sum(1 for status in statuses if status == "metadata_only"),
            "bid_count": sum(int_value(row.get("bid_count")) for row in rows),
            "unique_winners_or_high_bidders": len(winners),
            "first_auction_utc": min(created_times) if created_times else "",
            "latest_activity_utc": max(activity_times) if activity_times else "",
            "amount_note": "Per-Dog final/high bid is in historical_dog_search.amount; currencies differ by mission.",
            "confidence": "combined archived indexes + live Base dashboard metadata",
        }

    report_rows = [report_row("all", search_rows)]
    for mission in sorted(MISSION_CHAIN):
        mission_rows = [row for row in search_rows if int_value(row.get("mission")) == mission]
        report_rows.append(report_row(str(mission), mission_rows))
    report_schema = [
        ("mission", "TEXT"),
        ("chain", "TEXT"),
        ("dogs", "INTEGER"),
        ("auctions_or_records", "INTEGER"),
        ("settled", "INTEGER"),
        ("live_or_unsettled", "INTEGER"),
        ("metadata_only", "INTEGER"),
        ("bid_count", "INTEGER"),
        ("unique_winners_or_high_bidders", "INTEGER"),
        ("first_auction_utc", "TEXT"),
        ("latest_activity_utc", "TEXT"),
        ("amount_note", "TEXT"),
        ("confidence", "TEXT"),
    ]
    insert_rows(conn, "historical_dog_report", report_rows, report_schema)


HIDDEN_UI_COLUMNS = {
    "chain_id",
    "dog_image_url",
    "dog_external_url",
    "dog_opensea_url",
    "bidder_url",
    "winner_url",
    "holder_url",
    "latest_bidder_url",
    "bidder_winner_url",
    "bidder_wallet",
    "bidder_winner_wallet",
    "winner_wallet",
    "holder_wallet",
    "unique_bidders",
    "amount_eth",
    "amount_usd",
    "latest_bid_eth",
    "latest_bid_usd",
    "winning_bid_eth",
    "winning_bid_usd",
    "current_bid_eth",
    "current_bid_usd",
    "time_remaining",
    "auction_end_utc",
    "end_time_utc",
    "last_bid_utc",
    "settled_time_utc",
    "traits",
    "trait_rarity",
    "rarity_score",
    "tx_hash",
    "created_tx_hash",
    "settled_tx_hash",
    "block_number",
    "log_index",
    "bid_wei",
    "amount_wei",
    "amount_raw",
    "sources",
    "search_text",
}


def css_class_for_col(col: str) -> str:
    lowered = col.lower()
    if lowered in {"status", "auction_state", "state"}:
        return "state"
    if lowered in {"dog", "dog_name"}:
        return "dog-col"
    if "winner" in lowered or "bidder" in lowered or "farcaster" in lowered or "wallet" in lowered or "holder" in lowered:
        return "identity"
    if "time" in lowered or lowered.endswith("utc") or "date" in lowered:
        return "time"
    numeric_markers = ("_eth", "_wei", "_pct", "_reward", "_balance", "count", "bids", "rank", "remaining", "usd")
    if any(marker in lowered for marker in numeric_markers) or lowered in {"eth", "bid", "reward", "balance", "supply_pct", "rarity"}:
        return "num"
    return ""


def display_col_name(col: str) -> str:
    overrides = {
        "token_id": "dog id",
        "bidder_winner": "high bidder / winner",
        "auction_time_utc": "last bid / settled",
        "auction_created_time_utc": "created",
        "amount": "final / high bid",
        "bid_count": "bids",
        "unique_bidder_count": "unique bidders",
        "unique_winners_or_high_bidders": "unique winners / high bidders",
        "last_bid_utc": "last bid",
        "settled_time_utc": "settled",
        "time_remaining": "time left",
    }
    return overrides.get(col, col.replace("_", " "))


def cell_url(col: str, row_data: dict[str, Any]) -> str:
    if col == "bidder_winner":
        return str(row_data.get("bidder_winner_url") or basescan_address_url(row_data.get("bidder_winner_wallet")) or "")
    if col in {"bidder", "winner", "holder", "latest_bidder"}:
        return str(row_data.get(f"{col}_url") or basescan_address_url(row_data.get(f"{col}_wallet")) or "")
    if col == "dog":
        return str(row_data.get("dog_opensea_url") or row_data.get("dog_external_url") or "")
    return ""


def render_cell(col: str, value: Any, row_data: dict[str, Any]) -> str:
    text = "" if value is None else str(value)
    escaped = html.escape(text)
    lowered = col.lower()
    if col == "dog":
        image = str(row_data.get("dog_image_url") or "")
        text_url = cell_url(col, row_data)
        image_url = str(row_data.get("dog_opensea_url") or "")
        image_html = ""
        if image:
            image_html = f'<img class="dog-thumb" src="{html.escape(image, quote=True)}" alt="{html.escape(text, quote=True)} image" loading="lazy">'
            if image_url:
                dog_label = text or "Dog"
                image_label = f"Open {dog_label} on OpenSea"
                image_html = (
                    f'<a class="dog-image-link" href="{html.escape(image_url, quote=True)}" target="_blank" '
                    f'rel="noopener noreferrer" aria-label="{html.escape(image_label, quote=True)}" '
                    f'title="{html.escape(image_label, quote=True)}">{image_html}</a>'
                )
        label_html = f'<span>{escaped}</span>'
        if text_url and text:
            label_html = f'<a class="dog-link" href="{html.escape(text_url, quote=True)}" target="_blank" rel="noopener noreferrer">{escaped}</a>'
        inner = f'<span class="dog-cell">{image_html}{label_html}</span>'
        return inner
    if lowered in {"status", "auction_state"}:
        tone = "ongoing" if "ongoing" in text or text == "live" else "settled" if is_settled_status(text) else "neutral"
        return f'<span class="status-pill {tone}">{escaped}</span>'
    if col == "auction_time_utc" and text:
        status = str(row_data.get("status") or row_data.get("auction_state") or "")
        label = "Settled" if is_settled_status(status) else "Last bid"
        return f'<span class="time-cell"><b>{html.escape(label)}</b>{escaped}</span>'
    if col == "time_remaining" and text:
        status = str(row_data.get("status") or row_data.get("auction_state") or "").lower()
        end_time = str(row_data.get("auction_end_utc") or row_data.get("end_time_utc") or "")
        if end_time and ("ongoing" in status or status == "live"):
            return f'<span class="countdown" data-countdown-end="{html.escape(end_time, quote=True)}">{escaped}</span>'
    url = cell_url(col, row_data)
    if url and text:
        return f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{escaped}</a>'
    return escaped


def table_html(name: str, cols: list[str], rows: list[tuple[Any, ...]], *, featured: bool = False) -> str:
    visible = [(idx, col) for idx, col in enumerate(cols) if col not in HIDDEN_UI_COLUMNS]
    head = "".join(
        f'<th scope="col" aria-sort="none" class="{css_class_for_col(col)}"><button type="button" data-col="{visible_idx}">{html.escape(display_col_name(col))}</button></th>'
        for visible_idx, (_, col) in enumerate(visible)
    )
    body = []
    for row in rows:
        row_data = {col: row[i] for i, col in enumerate(cols)}
        cells = []
        for _, col in visible:
            value = row_data.get(col)
            label = html.escape(display_col_name(col), quote=True)
            cells.append(f'<td class="{css_class_for_col(col)}" data-label="{label}">{render_cell(col, value, row_data)}</td>')
        search_blob = row_data.get("search_text") or " ".join(text_value(value) for value in row_data.values() if text_value(value))
        body.append(f'<tr data-search="{html.escape(str(search_blob), quote=True)}">' + "".join(cells) + "</tr>")
    row_count = len(rows)
    caption_class = "table-caption sr-only" if featured else "table-caption"
    table_label = html.escape(name.replace("_", " "))
    caption = (
        f'<caption class="{caption_class}"><span>{table_label}</span>'
        f'<span data-total="{row_count}">{row_count} rows</span></caption>'
    )
    featured_class = " featured-table" if featured else ""
    return f'<section class="table-card{featured_class}" data-name="{html.escape(name)}"><div class="table-scroll"><table data-table="{html.escape(name)}">{caption}<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div></section>'


def metric_lookup(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> dict[str, str]:
    cols, rows = tables.get("mission3_metrics", (["metric", "value"], []))
    try:
        metric_idx = cols.index("metric")
        value_idx = cols.index("value")
    except ValueError:
        return {}
    return {str(row[metric_idx]): str(row[value_idx]) for row in rows}


def current_lookup(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> dict[str, str]:
    cols, rows = tables.get("auction_feed", ([], []))
    if not rows:
        cols, rows = tables.get("current_latest_bid", ([], []))
    if not rows:
        return {}
    row = rows[0]
    return {col: "" if row[i] is None else str(row[i]) for i, col in enumerate(cols)}


def markdown_cell(value: Any) -> str:
    return str("" if value is None else value).replace("\n", " ").replace("|", "\\|")


def markdown_table(cols: list[str], rows: list[tuple[Any, ...]], limit: int | None = None) -> str:
    selected = rows if limit is None else rows[:limit]
    out = ["| " + " | ".join(markdown_cell(col) for col in cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in selected:
        out.append("| " + " | ".join(markdown_cell(v) for v in row) + " |")
    return "\n".join(out) + "\n"


def metric_value(metrics: dict[str, str], key: str, fallback: str = "") -> str:
    value = metrics.get(key, fallback)
    return str(value) if value is not None else fallback


def markdown_link(label: str, href: str) -> str:
    return f"[{markdown_cell(label)}]({href})"


def format_current_bid(metrics: dict[str, str]) -> str:
    bid_eth = metric_value(metrics, "current_bid_eth")
    bid_usd = metric_value(metrics, "current_bid_usd")
    if bid_eth and bid_usd:
        return f"{bid_eth} ETH (${bid_usd})"
    if bid_eth:
        return f"{bid_eth} ETH"
    return ""


def metric_decimal(metrics: dict[str, str], key: str) -> Decimal | None:
    raw = metric_value(metrics, key).replace(",", "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:
        return None


def format_decimal_display(value: Decimal, places: int = 2) -> str:
    return f"{value:,.{places}f}"


def reward_token_display(metrics: dict[str, str], token_key: str, usd_key: str, token: str, places: int = 2) -> str:
    amount = metric_decimal(metrics, token_key)
    usd = metric_decimal(metrics, usd_key)
    if amount is None:
        return ""
    amount_text = f"{format_decimal_display(amount, places)} {token}/day"
    if usd is not None:
        amount_text += f" (${format_decimal_display(usd, 2)}/day)"
    return amount_text


def reward_usd_display(metrics: dict[str, str], key: str) -> str:
    value = metric_decimal(metrics, key)
    if value is None:
        return ""
    return f"${format_decimal_display(value, 2)}/day"


def reward_payback_display(metrics: dict[str, str]) -> str:
    days = metric_decimal(metrics, "reward_current_bid_payback_days")
    if days is None or days <= 0:
        return ""
    if days < 1:
        return "<1 day"
    places = 1 if days < 10 else 0
    return f"≈{format_decimal_display(days, places)} days"


def render_reward_strip(metrics: dict[str, str]) -> str:
    woof = reward_token_display(metrics, "reward_woof_per_dog_per_day", "reward_woof_per_dog_usd_per_day", "WOOF", 2)
    sup = reward_token_display(metrics, "reward_sup_per_dog_per_day", "reward_sup_per_dog_usd_per_day", "SUP", 2)
    total = reward_usd_display(metrics, "reward_total_per_dog_usd_per_day")
    payback = reward_payback_display(metrics)
    tiles = [
        ("WOOF / Dog", woof, "Base WOOF flow"),
        ("SUP / Dog", sup, "SUP flow"),
        ("Total / Dog", total, "WOOF + SUP"),
        ("Bid payback", payback, "Current bid / per-Dog flow"),
    ]
    body = "".join(
        f'<span class="reward-tile"><b>{html.escape(label)}</b><strong>{html.escape(value)}</strong><em>{html.escape(note)}</em></span>'
        for label, value, note in tiles
        if value
    )
    if not body:
        return ""
    return f'<section class="reward-strip" aria-label="Per-Dog reward estimate">{body}</section>'


def format_current_auction(metrics: dict[str, str]) -> str:
    token_id = metric_value(metrics, "current_auction_token_id")
    return f"Dog #{token_id}" if token_id else ""


def format_created_settled(metrics: dict[str, str]) -> str:
    created = metric_value(metrics, "created_auctions")
    settled = metric_value(metrics, "settled_auctions")
    return f"{created} / {settled}" if created and settled else ""


def render_readme_from_template(replacements: dict[str, str]) -> str:
    # README.md is generated because `npm run data` rewrites live snapshot sections.
    # Keep stable human-written copy in README.template.md and replace only explicit placeholders here.
    template = README_TEMPLATE_PATH.read_text(encoding="utf-8")
    for token, value in replacements.items():
        template = template.replace(token, value.rstrip())
    if "{{" in template and "}}" in template:
        raise RuntimeError("README template has unresolved placeholders")
    return template.rstrip() + "\n"


def render_readme(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]], manifest_rows: list[tuple[Any, ...]]) -> str:
    metrics = metric_lookup(tables)
    site_url = metric_value(metrics, "site_url", "https://ael-dev3.github.io/Degen-Dogs-Mission-3/")

    snapshot_rows = [
        ("Network", metric_value(metrics, "network", "base")),
        ("Snapshot block", metric_value(metrics, "latest_block")),
        ("Snapshot time UTC", metric_value(metrics, "latest_block_time_utc")),
        ("Current Dog", format_current_auction(metrics)),
        ("Current bid", format_current_bid(metrics)),
        ("Current high bidder", metric_value(metrics, "current_bidder")),
        ("Created / settled auctions", format_created_settled(metrics)),
        ("WOOF holders", metric_value(metrics, "woof_holders")),
    ]
    snapshot_rows = [(label, value) for label, value in snapshot_rows if value]

    dataset_rows = []
    for table, csv_path, rows in manifest_rows:
        table_name = str(table)
        csv_link = str(csv_path)
        json_link = str(Path(csv_link).with_suffix(".json"))
        dataset_rows.append((
            f"`{table_name}`",
            f"`{csv_link}`",
            rows,
            markdown_link("CSV", csv_link),
            markdown_link("JSON", json_link),
            DATASET_DESCRIPTIONS.get(table_name, "Generated table exported by the approved query layer."),
        ))

    contract_rows = [
        ("Auction house", metric_value(metrics, "auction_house")),
        ("Degen Dogs NFT", metric_value(metrics, "dog_nft")),
        ("WOOF token", metric_value(metrics, "woof_token")),
        ("SUP token", metric_value(metrics, "sup_token")),
    ]
    contract_rows = [(label, address) for label, address in contract_rows if address]

    configuration_rows = [(f"`{name}`", description) for name, description in CONFIGURATION_ENV_VARS]

    return render_readme_from_template({
        "{{LIVE_DASHBOARD_LINK}}": markdown_link(site_url, site_url),
        "{{CURRENT_SNAPSHOT_TABLE}}": markdown_table(["Field", "Value"], snapshot_rows).rstrip(),
        "{{PUBLISHED_DATASETS_TABLE}}": markdown_table(["Table", "Path", "Rows", "CSV", "JSON", "Description"], dataset_rows).rstrip(),
        "{{CONFIGURATION_TABLE}}": markdown_table(["Variable", "Purpose"], configuration_rows).rstrip(),
        "{{VERIFIED_CONTRACTS_TABLE}}": markdown_table(["Contract", "Address"], contract_rows).rstrip(),
    })


def parse_trait_item(item: str) -> tuple[str, str, str]:
    if ":" not in item:
        return "", item.strip(), ""
    trait_type, raw_value = item.split(":", 1)
    trait_type = trait_type.strip()
    trait_value = raw_value.strip()
    rarity = ""
    if trait_value.endswith(")") and " (" in trait_value:
        value_part, rarity_part = trait_value.rsplit(" (", 1)
        if rarity_part.endswith(")") and rarity_part[:-1].strip().endswith("%"):
            trait_value = value_part.strip()
            rarity = f"({rarity_part}"
    return trait_type, trait_value, rarity


def trait_chips(current: dict[str, str]) -> str:
    source = current.get("trait_rarity") or current.get("traits") or ""
    items = [item.strip() for item in source.split(";") if item.strip()]
    chips = []
    for item in items:
        trait_type, trait_value, rarity = parse_trait_item(item)
        if not trait_type or not trait_value:
            chips.append(f'<span class="trait-pill">{html.escape(item)}</span>')
            continue
        url = opensea_trait_url(trait_type, trait_value)
        label = f"View Degen Dogs with {trait_type}: {trait_value} on OpenSea"
        rarity_html = f'<span class="trait-rarity">{html.escape(rarity)}</span>' if rarity else ""
        chips.append(
            f'<a class="trait-pill trait-pill-link" href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="{html.escape(label, quote=True)}" '
            f'title="{html.escape(label, quote=True)}">'
            f'<span class="trait-type">{html.escape(trait_type)}</span>'
            f'<span class="trait-value">{html.escape(trait_value)}</span>'
            f'{rarity_html}'
            '</a>'
        )
    return "".join(chips)


def public_png_data_uri(filename: str) -> str:
    path = ROOT / "public" / filename
    try:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
    except FileNotFoundError:
        return filename
    return f"data:image/png;base64,{payload}"


def parse_timer_seconds(value: str) -> int | None:
    value = (value or "").strip().lower()
    if not value or value == "ended":
        return 0 if value == "ended" else None
    if value.isdigit():
        return int(value)
    day_count = 0
    if "d " in value:
        days, value = value.split("d ", 1)
        try:
            day_count = int(days.strip())
        except ValueError:
            return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return None
    return day_count * 86400 + hours * 3600 + minutes * 60 + seconds


def timer_urgency_state(remaining_seconds: int | None, auction_status: str = "") -> str:
    status = (auction_status or "").lower()
    if "settled" in status or "ended" in status:
        return "ended"
    if remaining_seconds is None:
        return "calm"
    if remaining_seconds <= 0:
        return "ended"
    if remaining_seconds <= 600:
        return "critical"
    if remaining_seconds <= 3600:
        return "urgent"
    return "calm"


def write_html(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
    metrics = metric_lookup(tables)
    current = current_lookup(tables)
    primary_parts = []
    for name in PRIMARY_TABLES:
        if name not in tables:
            continue
        cols, rows = tables[name]
        default_rows = rows[:10] if name == "auction_feed" else rows
        primary_parts.append(table_html(name, cols, default_rows, featured=True))
    site_url = metric_value(metrics, "site_url", "https://ael-dev3.github.io/Degen-Dogs-Mission-3/")
    top_links = [
        ("Bid live", "https://degendogs.club/auction?cache=1779901567562", "Open the Degen Dogs auction mini app to bid", "utility-chip--bid"),
        ("Farcaster", "https://farcaster.xyz/~/channel/degendogs", "Open the main Degen Dogs Farcaster channel", ""),
        ("Docs", "https://docs.degendogs.club/", "Open the Degen Dogs docs", ""),
        ("GitHub repo", "https://github.com/ael-dev3/Degen-Dogs-Mission-3", "Open the Degen Dogs Mission 3 GitHub repository", ""),
    ]
    top_link_html = "".join(
        f'<a class="utility-chip {extra}" href="{html.escape(url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" aria-label="{html.escape(label, quote=True)}">{html.escape(text)}</a>'
        for text, url, label, extra in top_links
    )
    mark_avatar_src = html.escape(public_png_data_uri("mark-profile.png"), quote=True)
    mark_credit_html = (
        '<div class="credit-menu">'
        '<button type="button" class="credit-trigger" aria-haspopup="true" aria-label="Project credit: Mark Carey, the creator of Degen Dogs">Degen Dogs by Mark Carey</button>'
        '<div class="credit-popover" aria-label="Mark Carey profile links">'
        '<div class="credit-head">'
        f'<img src="{mark_avatar_src}" alt="Pixel Degen Dog avatar for Mark Carey">'
        '<div><span>Mark Carey, the creator of Degen Dogs</span></div>'
        '</div>'
        '<a href="https://farcaster.xyz/markcarey" target="_blank" rel="noopener noreferrer">Farcaster</a>'
        '<a href="https://x.com/mthacks" target="_blank" rel="noopener noreferrer">X</a>'
        '<a href="https://github.com/markcarey" target="_blank" rel="noopener noreferrer">GitHub</a>'
        '</div></div>'
    )
    top_actions_html = f'<div class="top-actions">{top_link_html}{mark_credit_html}</div>'
    metric_cols, metric_rows = tables.get("mission3_metrics", (["metric", "value"], [("site_url", site_url)]))
    metric_head = "".join(f'<th scope="col">{html.escape(str(col))}</th>' for col in metric_cols)
    metric_body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in metric_rows
    )
    site_metric_html = (
        '<table data-table="mission3_metrics" hidden aria-hidden="true">'
        '<caption class="sr-only">mission3 metrics</caption>'
        f'<thead><tr>{metric_head}</tr></thead>'
        f'<tbody>{metric_body}</tbody>'
        '</table>'
    )

    dog = current.get("dog", f"Dog #{metrics.get('current_auction_token_id', '')}").strip() or "Current dog"
    current_dog_url = current.get("dog_opensea_url") or current.get("dog_external_url") or "#"
    current_dog_label = f"Open {dog} on OpenSea" if current.get("dog_opensea_url") else f"Open {dog}"
    current_dog_html = html.escape(dog)
    if current_dog_url and current_dog_url != "#":
        current_dog_html = (
            f'<a class="current-dog-link" href="{html.escape(current_dog_url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer" aria-label="{html.escape(current_dog_label, quote=True)}" '
            f'title="{html.escape(current_dog_label, quote=True)}">{current_dog_html}</a>'
        )
    bid = current.get("bid") or current.get("latest_bid") or f"{metrics.get('current_bid_eth', '0')} ETH"
    participant = current.get("bidder_winner") or current.get("bidder") or metrics.get("current_bidder", "")
    participant_url = current.get("bidder_winner_url") or current.get("bidder_url", "")
    participant_html = html.escape(participant)
    if participant_url and participant:
        participant_html = f'<a href="{html.escape(participant_url, quote=True)}" target="_blank" rel="noopener noreferrer">{participant_html}</a>'
    status = current.get("status") or current.get("auction_state", "")
    time_left = current.get("time_remaining", "")
    time_left_end = current.get("auction_end_utc") or current.get("end_time_utc", "")
    time_left_seconds = parse_timer_seconds(current.get("seconds_remaining", ""))
    if time_left_seconds is None:
        time_left_seconds = parse_timer_seconds(time_left)
    timer_state = timer_urgency_state(time_left_seconds, status)
    auction_status_attr = html.escape(status.lower(), quote=True)
    is_live_auction = ("ongoing" in status.lower() or status.lower() == "live") and timer_state != "ended"
    live_dot_class = "dot dot--live" if is_live_auction else "dot dot--idle"
    live_dot_html = (
        f'<span class="{live_dot_class}" data-live-dot data-auction-status="{auction_status_attr}" '
        f'data-live-end="{html.escape(time_left_end, quote=True)}" aria-hidden="true"></span>'
    )
    time_left_html = html.escape(time_left)
    if time_left and time_left_end:
        time_left_html = (
            f'<span class="countdown timer-value countdown--{timer_state}" '
            f'data-countdown-end="{html.escape(time_left_end, quote=True)}" '
            f'data-auction-status="{auction_status_attr}">{time_left_html}</span>'
        )
    image = current.get("dog_image_url", "")
    image_html = ""
    if image:
        image_html = f'<img src="{html.escape(image, quote=True)}" alt="{html.escape(dog, quote=True)} image">'
    rarity = current.get("rarity", "")
    current_detail = "".join(
        [
            f'<span class="detail-status"><b>Status</b>{html.escape(status)}</span>' if status else "",
            f'<span class="detail-bid"><b>Bid</b>{html.escape(bid)}</span>' if bid else "",
            (
                f'<span class="detail-time timer-card timer-card--{timer_state}" '
                f'data-auction-status="{auction_status_attr}"><b class="timer-label">Time left</b>{time_left_html}</span>'
            ) if time_left else "",
            f'<span class="detail-rarity"><b>Rarity</b>{html.escape(rarity)}</span>' if rarity else "",
            f'<span class="detail-bidder"><b>High bidder</b>{participant_html}</span>' if participant else "",
        ]
    )
    reward_strip = render_reward_strip(metrics)
    chips = trait_chips(current)
    css = """
:root{color-scheme:light;--paper:#e8ded5;--paper-calm:#eff8df;--paper-warm:#fff7e6;--paper-urgent:#fff1f1;--ink:#0a0a0a;--panel:#fffaf3;--panel2:#f4ece3;--muted:#6d625b;--line:#cdbfb3;--calm:#55a653;--calm-dark:#245a32;--warning:#d97706;--warning-dark:#92400e;--urgent:#e51b32;--urgent-dark:#9f1239;--critical-bg:#111111;--critical-red:#ef233c;--accent:#e51b2f;--accent2:#b91325;--shadow:0 10px 26px rgba(10,10,10,.1);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;min-width:320px;background:var(--paper);color:var(--ink);font-size:14px}
a{color:var(--ink);text-decoration:none;transition:color .16s ease,background .16s ease,border-color .16s ease,box-shadow .16s ease,transform .16s ease}
a:hover{color:var(--accent2)}
.shell{width:min(1520px,calc(100% - 16px));margin:0 auto;padding:12px 0 24px}
.current-card,.table-card{background:var(--panel);border:2px solid var(--ink);box-shadow:var(--shadow)}
.current-card{display:grid;grid-template-columns:minmax(360px,.9fr) minmax(260px,.42fr);gap:0;margin-bottom:10px;min-height:300px;overflow:hidden}
.current-copy{padding:18px;display:flex;flex-direction:column;gap:10px;border-right:2px solid var(--ink)}
.topline{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.eyebrow{display:flex;gap:8px;align-items:center;font-size:12px;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
.dot{width:10px;height:10px;background:#8a8178;border:2px solid var(--ink);display:inline-block;box-shadow:none}
.dot--live{background:var(--calm);animation:liveDotPulse 1.7s ease-in-out infinite;box-shadow:0 0 0 0 rgba(85,166,83,.42)}
.dot--idle{background:#8a8178}
.top-actions{display:flex;align-items:center;justify-content:flex-end;gap:7px;flex-wrap:wrap;max-width:min(100%,760px)}
.utility-chip,.credit-trigger{appearance:none;font-family:inherit;display:inline-flex;align-items:center;gap:7px;width:max-content;max-width:100%;border:2px solid var(--ink);background:var(--ink);color:white;padding:6px 10px;font-size:12px;font-weight:950;letter-spacing:.08em;text-transform:uppercase;line-height:1;box-shadow:3px 3px 0 var(--accent2);white-space:nowrap}
.utility-chip::after{content:'↗';color:#ffccd2;font-size:.85em;line-height:1}
.utility-chip:hover,.credit-trigger:hover,.credit-menu:focus-within .credit-trigger{background:white;color:var(--accent2);border-color:var(--accent2);transform:translate(-1px,-1px);box-shadow:4px 4px 0 var(--accent2)}
.utility-chip:hover::after{color:var(--accent2)}
.utility-chip--bid{background:var(--calm-dark);box-shadow:3px 3px 0 var(--calm)}
.utility-chip--bid:hover{border-color:var(--calm-dark);color:var(--calm-dark);box-shadow:4px 4px 0 var(--calm)}
.credit-menu{position:relative;display:inline-flex;padding-bottom:8px;margin-bottom:-8px}
.credit-trigger{cursor:pointer;background:#fff;color:var(--ink);box-shadow:3px 3px 0 var(--ink)}
.credit-trigger:focus{outline:2px solid var(--accent2);outline-offset:2px}
.credit-popover{position:absolute;right:0;top:100%;z-index:30;display:grid;gap:6px;min-width:252px;border:2px solid var(--ink);background:var(--panel);box-shadow:5px 5px 0 var(--ink);padding:10px;opacity:0;visibility:hidden;pointer-events:none;transform:translateY(-4px);transition:opacity .14s ease,transform .14s ease,visibility .14s ease}
.credit-head{display:grid;grid-template-columns:44px minmax(0,1fr);gap:8px;align-items:center;border-bottom:1.5px solid var(--line);padding-bottom:7px;margin-bottom:2px}
.credit-head img{width:44px;height:44px;object-fit:cover;image-rendering:pixelated;border:2px solid var(--ink);background:#fff;box-shadow:2px 2px 0 var(--ink)}
.credit-menu:hover .credit-popover,.credit-menu:focus-within .credit-popover{opacity:1;visibility:visible;pointer-events:auto;transform:translateY(0)}
.credit-popover span{font-size:12px;font-weight:850;line-height:1.2;color:var(--ink)}
.credit-popover a{display:flex;align-items:center;justify-content:space-between;border:1.5px solid var(--ink);background:var(--panel2);padding:5px 7px;font-size:12px;font-weight:950;line-height:1;text-transform:uppercase;letter-spacing:.06em;box-shadow:2px 2px 0 var(--ink)}
.credit-popover a::after{content:'↗';color:var(--accent2);font-size:.78em}
.credit-popover a:hover{background:#fff;border-color:var(--accent2);box-shadow:3px 3px 0 var(--accent2)}
.current-copy h1{font-size:clamp(34px,6vw,72px);line-height:.9;margin:0;letter-spacing:-.075em;max-width:10ch}
.current-dog-link{display:inline-flex;align-items:flex-start;gap:.04em;color:inherit;max-width:100%}
.current-dog-link::after{content:'↗';font-size:.28em;line-height:1;color:var(--accent2);letter-spacing:0;margin-left:.04em;transform:translateY(.14em);transition:transform .16s ease,color .16s ease}
.current-dog-link:hover{color:var(--accent2)}
.current-dog-link:hover::after{transform:translate(.05em,.06em)}
.subtitle{margin:0;color:var(--muted);font-weight:700}
.current-detail{display:flex;flex-wrap:wrap;align-items:stretch;gap:7px;margin-top:auto}
.reward-strip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:7px;margin-top:2px}
.reward-tile{display:flex;min-width:0;flex-direction:column;gap:2px;border:1.5px solid var(--ink);background:#eff8df;padding:7px 8px;font-weight:900;line-height:1.12;box-shadow:2px 2px 0 rgba(36,84,23,.18)}
.reward-tile b{font-size:9.5px;letter-spacing:.08em;text-transform:uppercase;color:#31551f}
.reward-tile strong{font-size:clamp(13px,1.35vw,18px);font-weight:950;letter-spacing:-.025em;overflow-wrap:anywhere}
.reward-tile em{font-style:normal;color:#5d6b48;font-size:10.5px;font-weight:800}
.reward-strip p{grid-column:1/-1;margin:0;color:var(--muted);font-size:11px;font-weight:800}
.current-detail > span{display:flex;min-height:48px;flex:0 1 auto;width:max-content;max-width:100%;flex-direction:column;justify-content:center;align-items:flex-start;border:1.5px solid var(--ink);background:var(--panel2);padding:7px 9px;font-weight:900;line-height:1.18}
.current-detail .detail-status{min-width:96px}
.current-detail .detail-bid{min-width:142px}
.current-detail .detail-rarity{min-width:104px}
.current-detail .detail-bidder{min-width:0}
.current-detail .timer-card{min-width:180px;position:relative;overflow:hidden;transition:background .18s ease,color .18s ease,border-color .18s ease,box-shadow .18s ease}
.current-detail .timer-card--calm,.current-detail .timer-card--normal{background:var(--paper-calm);color:var(--ink);border-color:#9bd78d;box-shadow:3px 3px 0 rgba(85,166,83,.14)}
.current-detail .timer-card--urgent{background:var(--paper-urgent);color:var(--ink);border-color:var(--urgent);box-shadow:3px 3px 0 rgba(229,27,50,.18)}
.current-detail .timer-card--critical{background:var(--critical-bg);color:white;border-color:var(--critical-red);box-shadow:3px 3px 0 var(--critical-red)}
.current-detail .timer-card--ended{background:#eee7dd;color:#4a403a;border-color:#8a8178;box-shadow:none}
.current-detail b,.time-cell b{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.current-detail .timer-label{display:flex;align-items:center;gap:5px}
.current-detail .timer-card--calm .timer-label,.current-detail .timer-card--normal .timer-label{color:var(--calm-dark)}
.current-detail .timer-card--urgent .timer-label{color:var(--urgent-dark)}
.current-detail .timer-card--critical .timer-label{color:#ffb3bd}
.current-detail .timer-card--ended .timer-label{color:#4a403a}
.current-detail .timer-card--urgent .timer-label::before,.current-detail .timer-card--critical .timer-label::before{content:'';width:6px;height:6px;border-radius:999px;background:currentColor;box-shadow:0 0 0 1px rgba(10,10,10,.12)}
.current-detail .timer-card--critical .timer-label::before{animation:timerPulse 1.8s ease-in-out infinite}
.current-detail .timer-value{display:block;margin-top:4px;border:0;background:transparent;padding:0;min-height:0;font-family:"Arial Black",Impact,ui-sans-serif,system-ui,sans-serif;font-size:clamp(21px,2.3vw,30px);font-weight:950;line-height:.96;letter-spacing:-.015em;font-variant-numeric:tabular-nums;transform:skewX(-4deg);transform-origin:left center;text-shadow:none;color:inherit}
.current-detail .timer-card--calm .timer-value,.current-detail .timer-card--normal .timer-value{color:var(--calm-dark);text-shadow:none}
.current-detail .timer-card--urgent .timer-value{color:var(--urgent);text-shadow:none}
.current-detail .timer-card--critical .timer-value{color:white;text-shadow:2px 2px 0 var(--critical-red),0 0 12px rgba(239,35,60,.45)}
.current-detail .timer-card--ended .timer-value{color:#4a403a;text-shadow:none}
@keyframes timerPulse{0%,100%{opacity:.55;transform:scale(.92)}50%{opacity:1;transform:scale(1.08)}}
@keyframes liveDotPulse{0%,100%{transform:scale(.94);box-shadow:0 0 0 0 rgba(85,166,83,.4)}50%{transform:scale(1.08);box-shadow:0 0 0 5px rgba(85,166,83,0)}}
.current-detail a,.identity a,td.time a{display:inline-flex;align-items:center;position:relative;width:max-content;max-width:100%;border:1.5px solid var(--ink);border-radius:999px;background:var(--panel2);padding:3px calc(8px + 1.05em) 3px 8px;font-weight:900;line-height:1.1;box-shadow:2px 2px 0 var(--ink)}
.current-detail a::after,.identity a::after,td.time a::after{content:'↗';position:absolute;inset-inline-end:7px;top:50%;transform:translateY(-50%);display:grid;place-items:center;width:.95em;height:.95em;font-size:.74em;line-height:1;color:var(--accent2);pointer-events:none}
.current-detail a:hover,.identity a:hover,td.time a:hover{background:#fff;border-color:var(--accent2);transform:translate(-1px,-1px);box-shadow:3px 3px 0 var(--accent2)}
.traits{display:flex;flex-wrap:wrap;gap:5px;max-height:78px;overflow:auto;padding-right:2px}
.traits .trait-pill{display:inline-flex;align-items:baseline;gap:3px;border:1.5px solid var(--ink);background:var(--panel);color:inherit;padding:4px 6px;font-size:11px;font-weight:800;line-height:1.15}
.trait-pill-link{cursor:pointer;text-decoration:none;transition:transform .16s ease,background .16s ease,border-color .16s ease,box-shadow .16s ease}
.trait-pill-link:hover{background:#fff;border-color:var(--accent2);transform:translateY(-1px);box-shadow:2px 2px 0 rgba(185,19,37,.18)}
.trait-pill-link:focus-visible,.dog-image-link:focus-visible{outline:3px solid currentColor;outline-offset:3px}
.trait-type{font-weight:950;color:var(--muted);text-transform:uppercase;font-size:.84em;letter-spacing:.04em}.trait-type::after{content:':'}.trait-value{color:var(--ink)}.trait-rarity{color:var(--muted);font-weight:850}
.dog-stage{display:flex;align-items:center;justify-content:center;background:var(--panel2);min-height:280px;padding:10px;overflow:hidden}
.dog-stage img{width:min(100%,330px);height:min(100%,330px);object-fit:contain;filter:drop-shadow(0 10px 18px rgba(0,0,0,.16))}
.toolbar{display:grid;grid-template-columns:minmax(260px,1fr) auto auto auto;align-items:end;gap:8px;margin:0 0 10px}
.toolbar-field,.toolbar-group{min-width:0}
.toolbar-field{display:flex;flex-direction:column;gap:3px}
.toolbar-group{display:flex;align-items:end;gap:6px;flex-wrap:wrap}
.toolbar label,.toolbar-legend{font-size:10px;font-weight:950;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.toolbar input,.toolbar select{border:2px solid var(--ink);background:var(--panel);color:var(--ink);padding:8px 10px;font:inherit;font-size:12px;font-weight:850;outline:none;box-shadow:3px 3px 0 var(--ink)}
.toolbar input{width:100%}
.toolbar select{min-height:36px;cursor:pointer}
.toolbar input:focus,.toolbar select:focus{border-color:var(--accent2);box-shadow:3px 3px 0 var(--accent2)}
.mission-group{align-items:flex-end}
.mission-toggle{display:inline-flex;align-items:center;gap:4px;flex-wrap:wrap}
.mission-toggle button,.page-btn{appearance:none;border:2px solid var(--ink);background:var(--panel2);color:var(--ink);padding:8px 9px;font:inherit;font-size:11px;font-weight:950;line-height:1;text-transform:uppercase;letter-spacing:.06em;cursor:pointer;box-shadow:2px 2px 0 var(--ink)}
.mission-toggle button[aria-pressed="true"]{background:var(--ink);color:#fff;box-shadow:2px 2px 0 var(--accent2)}
.mission-toggle button:focus-visible,.page-btn:focus-visible{outline:2px solid var(--accent2);outline-offset:2px}
.page-btn:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.pagination{justify-content:flex-end}
.archive-status{font-size:11px;color:var(--muted);font-weight:850;white-space:nowrap;line-height:1.15}
.archive-caveat{font-size:10.5px;color:var(--muted);font-weight:800;line-height:1.15}
.archive-caveat:empty{display:none}
.primary-grid{display:grid;gap:10px}
.table-card{overflow:hidden}
.table-scroll{width:100%;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.24;background:var(--panel)}
.sr-only{position:absolute!important;width:1px!important;height:1px!important;padding:0!important;margin:-1px!important;overflow:hidden!important;clip:rect(0,0,0,0)!important;white-space:nowrap!important;border:0!important}
caption.table-caption:not(.sr-only){caption-side:top;padding:8px 10px;border-bottom:2px solid var(--ink);font-weight:950;text-align:left;text-transform:uppercase;letter-spacing:.07em;display:flex;align-items:center;justify-content:space-between;gap:10px;background:var(--panel2);font-size:12px}
.table-caption [data-total]{color:var(--muted);font-size:11px;white-space:nowrap}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);vertical-align:middle;white-space:nowrap}
td{text-align:left}
th{position:relative;background:#efe3d7;color:var(--muted);font-size:10.5px;text-align:center;text-transform:uppercase;letter-spacing:.08em;font-weight:950}
th button{all:unset;box-sizing:border-box;cursor:pointer;display:flex;align-items:center;justify-content:center;width:100%;min-height:22px;position:relative;text-align:center;line-height:1.05;white-space:normal;padding:0 14px}
th button::after{content:'↕';font-size:.78em;color:var(--muted);position:absolute;right:0;top:50%;transform:translateY(-50%)}
th[aria-sort='ascending'] button::after{content:'↑';color:var(--accent2)}
th[aria-sort='descending'] button::after{content:'↓';color:var(--accent2)}
tbody tr{transition:background .12s ease}
tbody tr:hover{background:#fff2e7}
tbody tr:last-child td{border-bottom:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.time{font-variant-numeric:tabular-nums;color:#2a2725}
.time-cell{display:flex;flex-direction:column;gap:2px;line-height:1.12}
.time-cell b{margin:0;color:var(--accent2)}
@media (min-width:641px){.featured-table td{text-align:center}.featured-table td.num{text-align:center}.featured-table .identity{max-width:none;text-align:center}.featured-table .status-pill,.featured-table .dog-link,.featured-table .identity a{display:flex;width:max-content;margin-inline:auto}.featured-table .dog-cell{justify-content:center}.featured-table .time-cell{align-items:center;text-align:center}}
.status-pill{display:inline-flex;align-items:center;border:1.5px solid var(--ink);padding:3px 7px;font-size:11px;font-weight:950;text-transform:uppercase;letter-spacing:.06em;background:var(--panel2)}
.status-pill.ongoing{background:var(--accent);color:white}
.status-pill.settled{background:#efe3d7;color:var(--ink)}
.dog-link{display:inline-flex;color:var(--ink)}
.dog-link:hover{color:var(--accent2)}
.dog-cell{display:flex;align-items:center;gap:7px;font-weight:950}
.dog-image-link{display:inline-flex;flex:none;border-radius:3px;color:inherit}
.dog-image-link:hover .dog-thumb{transform:translateY(-1px);box-shadow:0 3px 0 rgba(10,10,10,.16)}
.dog-thumb{width:38px;height:38px;border:1.5px solid var(--ink);background:var(--panel2);object-fit:cover;flex:none;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}
.dog-col{min-width:132px}
.identity{max-width:180px;overflow:hidden;text-overflow:ellipsis}
.identity a{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
@media (prefers-reduced-motion:reduce){.timer-card,.timer-card *,.dot--live{animation:none!important;transition:none!important}}
@media (max-width:1100px){.reward-strip{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:640px){.reward-strip{grid-template-columns:1fr;gap:5px}.reward-tile{padding:6px 7px}.reward-tile strong{font-size:13px}.reward-strip p{font-size:10px}}
@media (max-width:900px){.shell{width:min(100% - 10px,760px);padding:8px 0 18px}.current-card{grid-template-columns:1fr;min-height:0}.current-copy{border-right:0;border-bottom:2px solid var(--ink);padding:14px}.dog-stage{min-height:220px}.dog-stage img{max-height:240px}.toolbar{grid-template-columns:1fr;align-items:stretch}.toolbar input{width:100%}.toolbar-group{align-items:flex-start}.pagination{justify-content:flex-start}.current-copy h1{font-size:clamp(34px,13vw,58px)}th,td{padding:6px 7px}table{font-size:12.5px}.traits{max-height:70px}}
@media (max-width:640px){body{font-size:13px}.shell{width:calc(100% - 8px);padding:4px 0 14px}.current-card,.table-card{border-width:1.5px;box-shadow:0 6px 16px rgba(10,10,10,.1)}.current-card{margin-bottom:8px}.current-copy{padding:12px;gap:8px;border-bottom:1.5px solid var(--ink)}.eyebrow{font-size:11px;gap:6px}.dot{width:8px;height:8px}.current-copy h1{font-size:clamp(42px,17vw,62px);max-width:none;line-height:.88}.subtitle{font-size:12px}.current-detail{gap:6px}.current-detail > span{min-width:0;min-height:42px;padding:6px 7px;font-size:12.5px;overflow-wrap:anywhere}.current-detail .timer-card{flex:1 1 100%;width:100%;max-width:100%;min-width:0}.current-detail .detail-rarity,.current-detail .detail-status{min-width:84px}.current-detail .countdown{font-size:clamp(22px,9vw,36px)}.current-detail b,.time-cell b{font-size:9px}.current-detail a,.identity a,td.time a{max-width:100%;font-size:12px;box-shadow:1.5px 1.5px 0 var(--ink)}.traits{display:grid;grid-template-columns:1fr;gap:4px;max-height:none;overflow:visible}.traits .trait-pill{padding:3px 5px;font-size:9.5px;line-height:1.12;white-space:normal;overflow-wrap:anywhere}.dog-stage{min-height:166px;padding:4px}.dog-stage img{width:min(58vw,204px);height:min(58vw,204px)}.toolbar{margin:8px 0;gap:6px}.toolbar input,.toolbar select{padding:8px 10px;font-size:13px;box-shadow:2px 2px 0 var(--ink)}.mission-toggle button,.page-btn{padding:7px 8px;border-width:1.5px;box-shadow:1.5px 1.5px 0 var(--ink)}.archive-status,.archive-caveat{width:100%;white-space:normal}table{font-size:12px}.featured-table .table-scroll{overflow:visible}.featured-table table{display:block;background:transparent}.featured-table caption.table-caption:not(.sr-only){display:flex;padding:7px 8px;border-bottom:1.5px solid var(--ink)}.featured-table thead{display:none}.featured-table tbody{display:grid;gap:7px;padding:7px;background:var(--panel2)}.featured-table tr{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 8px;align-items:center;border:1.5px solid var(--ink);background:var(--panel);padding:7px;box-shadow:2px 2px 0 rgba(10,10,10,.18)}.featured-table tr:hover{background:var(--panel)}.featured-table td{display:block;min-width:0;border:0;padding:0;white-space:normal}.featured-table td::before{content:attr(data-label);display:block;margin-bottom:2px;color:var(--muted);font-size:8.5px;font-weight:950;letter-spacing:.08em;text-transform:uppercase}.featured-table td.state{align-self:start}.featured-table td.state::before{display:none}.featured-table td.dog-col{grid-column:2;grid-row:1/span 2}.featured-table td.identity{grid-column:1/-1;max-width:none}.featured-table td.num{grid-column:1/-1;text-align:left;font-size:13px;font-weight:950}.featured-table td.time{grid-column:1/-1}.featured-table td:not(.state):not(.dog-col):not(.identity):not(.num):not(.time){grid-column:1/-1}.dog-cell{gap:6px}.dog-thumb{width:34px;height:34px}.time-cell{gap:1px}.status-pill{padding:3px 6px;font-size:9px}}
@media (max-width:420px){.traits{grid-template-columns:1fr}.dog-stage img{width:min(54vw,196px);height:min(54vw,196px)}}
@media (max-width:380px){.current-detail{display:grid;grid-template-columns:1fr}.current-detail > span{width:100%;max-width:100%}.current-copy h1{font-size:clamp(38px,16vw,54px)}}
@media (max-width:900px){.topline{align-items:flex-start}.top-actions{justify-content:flex-start;max-width:100%}}
@media (max-width:640px){.utility-chip,.credit-trigger{font-size:10px;padding:5px 7px;border-width:1.5px;box-shadow:2px 2px 0 var(--accent2)}.utility-chip--bid{box-shadow:2px 2px 0 var(--calm)}.credit-trigger{box-shadow:2px 2px 0 var(--ink)}.credit-popover{left:0;right:auto;min-width:min(280px,calc(100vw - 32px))}}

""".strip()
    script = """
const filter=document.getElementById('filter');
const missionButtons=[...document.querySelectorAll('[data-mission-filter]')];
const sortSelect=document.getElementById('auction-sort');
const pageSizeSelect=document.getElementById('auction-page-size');
const pagePrev=document.getElementById('auction-prev');
const pageNext=document.getElementById('auction-next');
const pageLabel=document.getElementById('auction-page-label');
const showingLabel=document.getElementById('auction-showing');
const archiveCaveat=document.getElementById('auction-caveat');
const auctionTable=document.querySelector('table[data-table="auction_feed"]');
const auctionBody=auctionTable?.tBodies?.[0];
const auctionTotal=auctionTable?.caption?.querySelector('[data-total]');
const defaultRows=auctionBody?[...auctionBody.rows].map(row=>row.cloneNode(true)):[];
const archiveState={query:'',mission:'all',sortMode:'newest',pageSize:10,currentPage:1};
let unifiedPromise=null;
let unifiedRecords=[];
let unifiedReady=false;
const key=v=>{const s=v.trim().replaceAll(',','').replace(/[()$]/g,'');const n=Number(s.split(' ')[0]);return s!==''&&Number.isFinite(n)?n:v.trim().toLowerCase();};
const parseUtc=value=>Date.parse(String(value||'').replace(' ','T')+'Z');
const formatDuration=seconds=>{const s=Math.max(0,Math.floor(seconds));const d=Math.floor(s/86400);const h=Math.floor((s%86400)/3600);const m=Math.floor((s%3600)/60);const sec=s%60;const clock=`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;return d>0?`${d}d ${clock}`:clock;};
const TIMER_STATES=['calm','normal','urgent','critical','ended'];
const timerState=(seconds,forceEnded=false)=>forceEnded||seconds<=0?'ended':seconds<=600?'critical':seconds<=3600?'urgent':'calm';
const applyTimerState=(el,state)=>{TIMER_STATES.forEach(name=>el.classList.toggle(`countdown--${name}`,name===state));const box=el.closest('.timer-card');if(box){TIMER_STATES.forEach(name=>box.classList.toggle(`timer-card--${name}`,name===state));}};
const escapeHtml=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const attr=value=>escapeHtml(value);
const shortAddress=value=>{const s=String(value||'');return s.startsWith('0x')&&s.length>=12?`${s.slice(0,6)}…${s.slice(-4)}`:s;};
const toNumber=value=>{if(value===null||value===undefined)return null;const text=String(value).replace(/[$,]/g,'').trim();if(!text)return null;const n=Number.parseFloat(text);return Number.isFinite(n)?n:null;};
const firstNumeric=values=>{for(const value of values){const n=toNumber(value);if(n!==null)return n;}return null;};
const getUsdSortValue=record=>{const amount=record.amount||{};return firstNumeric([amount.usd_estimate,amount.estimated_usd_value,record.amount_usd_estimate,record.final_bid_usd_estimate,record.high_bid_usd_estimate,record.usd_at_time,record.usd_value,record.estimated_usd]);};
const newestRank=record=>{const status=String(record.status||'').toLowerCase();return status==='live'||status.includes('ongoing')?1:0;};
const compareNewest=(a,b)=>{const live=newestRank(b)-newestRank(a);if(live)return live;const at=Date.parse(a.activity_time_utc||'')||0;const bt=Date.parse(b.activity_time_utc||'')||0;if(bt!==at)return bt-at;return Number(b.dog_id||0)-Number(a.dog_id||0);};
const exactDogQuery=q=>{const dog=q.match(/(?:^|\s)dog\s*#?\s*(\d{1,4})(?=\s|$)/);if(dog)return Number(dog[1]);const bare=q.match(/^#?(\d{1,4})$/);return bare?Number(bare[1]):null;};
const rowSearchText=record=>{const amount=record.amount||{};const who=record.winner_or_high_bidder||{};const created=record.auction_created||{};const settled=record.settlement||{};return [record.search_text,`dog #${record.dog_id}`,`dog ${record.dog_id}`,record.dog_id,`mission ${record.mission}`,record.era_label,record.chain,record.chain_id,record.status,who.wallet,who.display,who.farcaster_handle,who.farcaster_fid,amount.native,amount.native_symbol,amount.usd_estimate,amount.usd_estimate_display,amount.usd_estimate_price_date_utc,amount.usd_estimate_source,created.tx_hash,settled.tx_hash,...(record.bid_tx_hashes||[])].filter(Boolean).join(' ').toLowerCase();};
const statusCell=status=>{const text=String(status||'unknown');const lower=text.toLowerCase();const tone=lower.includes('ongoing')||lower==='live'?'ongoing':(lower.includes('settled')?'settled':'neutral');return `<span class="status-pill ${tone}">${escapeHtml(text)}</span>`;};
const dogCell=record=>{const dog=`Dog #${record.dog_id}`;const img=record.dog_image_url?`<img class="dog-thumb" src="${attr(record.dog_image_url)}" alt="${attr(dog)} image" loading="lazy">`:'';const links=record.links||{};const item=record.dog_item_url||links.item||links.dog_page||'#';const imgHtml=img&&item!=='#'?`<a class="dog-image-link" href="${attr(item)}" target="_blank" rel="noopener noreferrer" aria-label="Open ${attr(dog)}" title="Open ${attr(dog)}">${img}</a>`:img;const label=item&&item!=='#'?`<a class="dog-link" href="${attr(item)}" target="_blank" rel="noopener noreferrer">${escapeHtml(dog)}</a>`:`<span>${escapeHtml(dog)}</span>`;return `<span class="dog-cell">${imgHtml}${label}</span>`;};
const identityCell=record=>{const who=record.winner_or_high_bidder||{};const label=who.display||shortAddress(who.wallet)||'';if(!label)return '';const url=who.profile_url||who.wallet_explorer_url||record.links?.explorer||'';return url?`<a href="${attr(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`:escapeHtml(label);};
const bidCell=record=>{const amount=record.amount||{};if(!amount.native)return archiveState.sortMode==='highest_usd'?'USD estimate unavailable':'';const native=`${amount.native} ${amount.native_symbol||''}`.trim();const usd=amount.usd_estimate_display?` (${amount.usd_estimate_display} est.)`:(archiveState.sortMode==='highest_usd'?' (USD estimate unavailable)':'');return escapeHtml(`${native}${usd}`);};
const timeCell=record=>{const status=String(record.status||'').toLowerCase();const label=status.includes('settled')?'Settled':(record.activity_time_basis==='last_bid_block_time'?'Last bid':'Activity');const value=record.activity_time_utc||'';return value?`<span class="time-cell"><b>${label}</b>${escapeHtml(value.replace('T',' ').replace('Z',''))}</span>`:'';};
const rarityCell=record=>escapeHtml(record.rarity?.display||'');
const unifiedRowHtml=record=>{const statusLabel=`${record.era_label||`Mission ${record.mission}`} · ${record.status||''}`;return `<tr data-search="${attr(rowSearchText(record))}"><td class="state" data-label="status">${statusCell(statusLabel)}</td><td class="dog-col" data-label="dog">${dogCell(record)}</td><td class="identity" data-label="high bidder / winner">${identityCell(record)}</td><td class="" data-label="bid">${bidCell(record)}</td><td class="time" data-label="last bid / settled">${timeCell(record)}</td><td class="num" data-label="rarity">${rarityCell(record)}</td></tr>`;};
const isDefaultArchiveState=()=>archiveState.query===''&&archiveState.mission==='all'&&archiveState.sortMode==='newest'&&archiveState.pageSize===10&&archiveState.currentPage===1;
const syncControls=()=>{if(filter&&filter.value!==archiveState.query)filter.value=archiveState.query;missionButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.missionFilter===archiveState.mission)));if(sortSelect)sortSelect.value=archiveState.sortMode;if(pageSizeSelect)pageSizeSelect.value=String(archiveState.pageSize);};
const loadUnified=()=>unifiedPromise||(unifiedPromise=(async()=>{for(const url of ['generated/unified_dog_search_index.json','/generated/unified_dog_search_index.json']){try{const r=await fetch(url,{cache:'no-store'});if(!r.ok)continue;const type=r.headers.get('content-type')||'';if(!type.includes('json'))continue;return await r.json();}catch(_){}}throw new Error('unified archive index unavailable');})());
const emptyArchiveMessage=()=>archiveState.mission!=='all'&&!archiveState.query?`No verified Mission ${archiveState.mission} auction rows are available yet.`:'No auctions found for this search.';
const setAuctionRows=(records,label,total=records.length)=>{if(!auctionBody)return;auctionBody.innerHTML=records.length?records.map(unifiedRowHtml).join(''):`<tr><td colspan="6">${escapeHtml(emptyArchiveMessage())}</td></tr>`;if(auctionTotal){auctionTotal.dataset.total=String(total);auctionTotal.textContent=label||`${total} rows`;}};
const filteredRows=()=>{let rows=unifiedRecords;if(archiveState.mission!=='all')rows=rows.filter(record=>String(record.mission)===archiveState.mission);const q=archiveState.query;if(q)rows=rows.filter(record=>matchesQuery(record,q));return rows;};
const sortRows=rows=>{const dogQuery=exactDogQuery(archiveState.query);rows=[...rows];if(archiveState.sortMode==='highest_usd'){return rows.sort((a,b)=>{const av=getUsdSortValue(a);const bv=getUsdSortValue(b);const aMissing=av===null||Number.isNaN(av);const bMissing=bv===null||Number.isNaN(bv);if(aMissing&&bMissing)return compareNewest(a,b);if(aMissing)return 1;if(bMissing)return -1;return bv-av||compareNewest(a,b);});}return rows.sort((a,b)=>{if(dogQuery!==null){const ae=Number(a.dog_id)===dogQuery;const be=Number(b.dog_id)===dogQuery;if(ae!==be)return ae?-1:1;}return compareNewest(a,b);});};
const renderPagination=(total,totalPages,start,count)=>{const end=count?start+count:0;if(showingLabel)showingLabel.textContent=total?`Showing ${start+1}–${end} of ${total}`:'Showing 0 of 0';if(pageLabel)pageLabel.textContent=`Page ${archiveState.currentPage} of ${totalPages}`;if(pagePrev)pagePrev.disabled=archiveState.currentPage<=1;if(pageNext)pageNext.disabled=archiveState.currentPage>=totalPages;if(archiveCaveat)archiveCaveat.textContent=archiveState.sortMode==='highest_usd'?'USD values are historical estimates where available. Missing estimates sort last.':'';};
const renderArchive=()=>{if(!auctionBody||!unifiedReady)return;syncControls();let rows=filteredRows();rows=sortRows(rows);const total=rows.length;const totalPages=Math.max(1,Math.ceil(total/archiveState.pageSize));archiveState.currentPage=Math.min(Math.max(1,archiveState.currentPage),totalPages);const start=(archiveState.currentPage-1)*archiveState.pageSize;const pageRows=rows.slice(start,start+archiveState.pageSize);const label=isDefaultArchiveState()?'Latest 10 archive records':`${total} archive ${total===1?'match':'matches'}`;setAuctionRows(pageRows,label,total);renderPagination(total,totalPages,start,pageRows.length);updateCounts();};
const restoreAuctionRows=()=>{archiveState.query='';archiveState.mission='all';archiveState.sortMode='newest';archiveState.pageSize=10;archiveState.currentPage=1;renderArchive();};
const matchesQuery=(record,q)=>{let remaining=q;const missionMatch=remaining.match(/(?:^|\s)mission\s*:?\s*([123])(?=\s|$)/);if(missionMatch&&Number(record.mission)!==Number(missionMatch[1]))return false;remaining=remaining.replace(/(?:^|\s)mission\s*:?\s*[123](?=\s|$)/g,' ');const dogMatch=remaining.match(/(?:^|\s)dog\s*#?\s*(\d{1,4})(?=\s|$)/);if(dogMatch&&Number(record.dog_id)!==Number(dogMatch[1]))return false;remaining=remaining.replace(/(?:^|\s)dog\s*#?\s*\d{1,4}(?=\s|$)/g,' ');const terms=remaining.split(/\s+/).filter(Boolean);const haystack=rowSearchText(record);return terms.every(term=>haystack.includes(term));};
const fallbackAuctionRows=()=>{if(!auctionBody)return;const rows=defaultRows.slice(0,10);auctionBody.replaceChildren(...rows.map(row=>row.cloneNode(true)));if(auctionTotal){auctionTotal.dataset.total=String(rows.length);auctionTotal.textContent='Latest 10 archive records';}renderPagination(rows.length,1,0,rows.length);updateCounts();};
filter?.addEventListener('input',()=>{archiveState.query=filter.value.trim().toLowerCase();archiveState.currentPage=1;renderArchive();});
missionButtons.forEach(button=>button.addEventListener('click',()=>{archiveState.mission=button.dataset.missionFilter||'all';archiveState.currentPage=1;renderArchive();}));
sortSelect?.addEventListener('change',()=>{archiveState.sortMode=sortSelect.value||'newest';archiveState.currentPage=1;renderArchive();});
pageSizeSelect?.addEventListener('change',()=>{archiveState.pageSize=Math.min(100,Math.max(10,Number(pageSizeSelect.value)||10));archiveState.currentPage=1;renderArchive();});
pagePrev?.addEventListener('click',()=>{archiveState.currentPage=Math.max(1,archiveState.currentPage-1);renderArchive();});
pageNext?.addEventListener('click',()=>{archiveState.currentPage+=1;renderArchive();});
const updateLiveDots=()=>{const now=Date.now();document.querySelectorAll('[data-live-dot]').forEach(el=>{const status=String(el.dataset.auctionStatus||'').toLowerCase();const end=parseUtc(el.dataset.liveEnd);const ended=status.includes('settled')||status.includes('ended')||(Number.isFinite(end)&&end<=now);const live=(status.includes('ongoing')||status.includes('live'))&&!ended;el.classList.toggle('dot--live',live);el.classList.toggle('dot--idle',!live);});};
const updateCountdowns=()=>{const now=Date.now();document.querySelectorAll('[data-countdown-end]').forEach(el=>{const end=parseUtc(el.dataset.countdownEnd);if(!Number.isFinite(end))return;const box=el.closest('.timer-card');const status=String(el.dataset.auctionStatus||box?.dataset.auctionStatus||'').toLowerCase();const forceEnded=status.includes('settled')||status.includes('ended');const seconds=forceEnded?0:Math.max(0,Math.floor((end-now)/1000));const state=timerState(seconds,forceEnded);el.textContent=state==='ended'?'ended':formatDuration(seconds);applyTimerState(el,state);});updateLiveDots();};
const updateCounts=()=>{document.querySelectorAll('table').forEach(table=>{if(!table.tBodies.length)return;const rows=[...table.tBodies[0].rows];const visible=rows.filter(row=>!row.hidden).length;const total=table.caption?.querySelector('[data-total]');if(total&&!table.matches('[data-table="auction_feed"]')){const suffix=visible===Number(total.dataset.total)?' rows':` / ${total.dataset.total} rows`;total.textContent=`${visible}${suffix}`;}});};
loadUnified().then(records=>{unifiedRecords=Array.isArray(records)?records.filter(record=>record&&typeof record==='object'):[];unifiedReady=true;renderArchive();}).catch(()=>{fallbackAuctionRows();});
document.querySelectorAll('th button').forEach(button=>{button.addEventListener('click',()=>{const table=button.closest('table');const tbody=table.tBodies[0];const col=Number(button.dataset.col);const next=button.dataset.dir==='asc'?'desc':'asc';table.querySelectorAll('th').forEach(th=>{const b=th.querySelector('button');if(b)delete b.dataset.dir;th.setAttribute('aria-sort','none');});button.dataset.dir=next;button.closest('th').setAttribute('aria-sort',next==='asc'?'ascending':'descending');const rows=[...tbody.rows].sort((a,b)=>{const av=key(a.cells[col]?.textContent||'');const bv=key(b.cells[col]?.textContent||'');const cmp=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv));return next==='asc'?cmp:-cmp;});rows.forEach(row=>tbody.appendChild(row));});});
updateCounts();
updateCountdowns();
setInterval(updateCountdowns,1000);
""".strip()
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#e8ded5">
<link rel="icon" href="data:,">
<title>Degen Dogs Mission 3 Auctions</title>
<style>{css}</style>
</head>
<body>
<div class="shell">
  <section class="current-card" aria-label="Current auction">
    <div class="current-copy">
      <div class="topline"><div class="eyebrow">{live_dot_html}Mission 3 auction feed</div>{top_actions_html}</div>
      <h1>{current_dog_html}</h1>
      <div class="current-detail">{current_detail}</div>
      {reward_strip}
      <div class="traits" aria-label="Current dog traits and rarity">{chips}</div>
    </div>
    <a class="dog-stage" href="{html.escape(current_dog_url, quote=True)}" target="_blank" rel="noopener noreferrer" aria-label="{html.escape(current_dog_label, quote=True)}">{image_html}</a>
  </section>
  <div class="toolbar" aria-label="Auction archive controls">
    <div class="toolbar-field search-field"><label for="filter">Search auctions</label><input id="filter" type="search" aria-label="search unified Mission 1, 2, and 3 archive" placeholder="Search all missions: Dog #, wallet, handle, tx, chain, status" autocomplete="off"></div>
    <div class="toolbar-group mission-group" role="group" aria-label="Filter by mission"><span class="toolbar-legend">Mission</span><span class="mission-toggle"><button type="button" data-mission-filter="all" aria-pressed="true">All</button><button type="button" data-mission-filter="1" aria-pressed="false">Mission 1</button><button type="button" data-mission-filter="2" aria-pressed="false">Mission 2</button><button type="button" data-mission-filter="3" aria-pressed="false">Mission 3</button></span></div>
    <div class="toolbar-field"><label for="auction-sort">Sort by</label><select id="auction-sort" aria-label="Sort auctions"><option value="newest" selected>Newest first</option><option value="highest_usd">Highest USD bid</option></select></div>
    <div class="toolbar-field"><label for="auction-page-size">Rows</label><select id="auction-page-size" aria-label="Rows per page"><option value="10" selected>10</option><option value="25">25</option><option value="50">50</option><option value="100">100</option></select></div>
    <div class="toolbar-group pagination" aria-label="Auction pagination"><span id="auction-showing" class="archive-status" aria-live="polite">Showing 1–10</span><button id="auction-prev" class="page-btn" type="button" disabled>Previous</button><span id="auction-page-label" class="archive-status">Page 1 of 1</span><button id="auction-next" class="page-btn" type="button">Next</button><span id="auction-caveat" class="archive-caveat" aria-live="polite"></span></div>
  </div>
  <main class="primary-grid">{''.join(primary_parts)}</main>
  {site_metric_html}
</div>
<script>{script}</script>
</body>
</html>
"""
    (ROOT / "index.html").write_text(html_doc, encoding="utf-8")

def main() -> None:
    GENERATED.mkdir(exist_ok=True)
    PUBLIC_GENERATED.mkdir(parents=True, exist_ok=True)
    latest_block = int(rpc("eth_blockNumber", []), 16)
    snapshot_tag = hex(latest_block)
    latest_block_data = rpc("eth_getBlockByNumber", [snapshot_tag, False])
    latest_time = utc_from_unix(int(latest_block_data["timestamp"], 16))

    auction_logs = fetch_logs(
        AUCTION_HOUSE,
        [TOPIC_AUCTION_CREATED, TOPIC_AUCTION_BID, TOPIC_AUCTION_SETTLED],
        FROM_BLOCK,
        latest_block,
    )
    created_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_CREATED]
    bid_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_BID]
    settled_logs = [log for log in auction_logs if log["topics"][0].lower() == TOPIC_AUCTION_SETTLED]
    transfer_logs = fetch_logs(WOOF, TOPIC_TRANSFER, FROM_BLOCK, latest_block)

    token_stats = fetch_token_stats(snapshot_tag)
    decimals = int(token_stats["woof_decimals"])
    dog_total_supply = fetch_dog_total_supply(snapshot_tag)
    token_stats["dog_total_supply"] = str(dog_total_supply)
    dog_metadata = fetch_dog_metadata_rows(dog_total_supply, snapshot_tag)
    current = fetch_current_auction(latest_block, latest_time, snapshot_tag)
    created, bids, settled = decode_auction_logs(created_logs, bid_logs, settled_logs)
    holders = fetch_woof_holders(transfer_logs, decimals, snapshot_tag)
    identity_addresses = collect_identity_addresses(current, bids, settled, holders)
    neynar_profiles = fetch_farcaster_profiles(identity_addresses)
    current_bidder = normalize_address(current.get("bidder"))
    current_has_neynar_profile = any(
        normalize_address(profile.get("address")) == current_bidder and profile.get("username")
        for profile in neynar_profiles
    )
    auction_profiles = [] if current_has_neynar_profile else fetch_degendogs_auction_profiles(current)
    farcaster_profiles = merge_farcaster_profiles(neynar_profiles, auction_profiles)

    conn = sqlite3.connect(":memory:")
    insert_rows(conn, "auction_created", created, [("token_id", "INTEGER"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT")])
    insert_rows(conn, "auction_bids", bids, [("token_id", "INTEGER"), ("bidder", "TEXT"), ("bid_eth", "REAL"), ("bid_wei", "TEXT"), ("extended", "INTEGER"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "auction_settled", settled, [("token_id", "INTEGER"), ("winner", "TEXT"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "woof_holders", holders, [("address", "TEXT"), ("balance_woof", "REAL"), ("balance_raw", "TEXT")])
    insert_rows(conn, "farcaster_profiles", farcaster_profiles, [("address", "TEXT"), ("fid", "INTEGER"), ("username", "TEXT"), ("display_name", "TEXT"), ("pfp_url", "TEXT")])
    insert_rows(conn, "dog_metadata", dog_metadata, [("token_id", "INTEGER"), ("dog_name", "TEXT"), ("dog_image_url", "TEXT"), ("dog_external_url", "TEXT"), ("dog_opensea_url", "TEXT"), ("traits", "TEXT"), ("trait_rarity", "TEXT"), ("rarity", "TEXT"), ("rarity_score", "REAL")])
    insert_rows(conn, "token_stats", [{"metric": k, "value": v} for k, v in token_stats.items()], [("metric", "TEXT"), ("value", "TEXT")])
    insert_rows(conn, "current_auction_source", [current], [("token_id", "INTEGER"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("bidder", "TEXT"), ("settled", "INTEGER"), ("latest_block", "INTEGER"), ("latest_block_time_utc", "TEXT")])

    conn.executescript(SQL_PATH.read_text(encoding="utf-8"))
    build_historical_dog_tables(conn, dog_total_supply, dog_metadata)

    tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    manifest_rows = []
    for table in OUTPUT_TABLES:
        cols, rows = fetch_table(conn, table)
        tables[table] = (cols, rows)
        out_path = GENERATED / f"{table}.csv"
        write_csv(out_path, cols, rows)
        write_json(GENERATED / f"{table}.json", cols, rows)
        write_csv(PUBLIC_GENERATED / f"{table}.csv", cols, rows)
        write_json(PUBLIC_GENERATED / f"{table}.json", cols, rows)
        manifest_rows.append((table, f"generated/{table}.csv", len(rows)))

    write_csv(GENERATED / "manifest.csv", ["table", "file", "rows"], manifest_rows)
    write_json(GENERATED / "manifest.json", ["table", "file", "rows"], manifest_rows)
    write_csv(PUBLIC_GENERATED / "manifest.csv", ["table", "file", "rows"], manifest_rows)
    write_json(PUBLIC_GENERATED / "manifest.json", ["table", "file", "rows"], manifest_rows)
    expected_public_files = {"manifest.csv", "manifest.json"}
    for table in OUTPUT_TABLES:
        expected_public_files.add(f"{table}.csv")
        expected_public_files.add(f"{table}.json")
    for stale in PUBLIC_GENERATED.glob("*"):
        if stale.is_file() and stale.name not in expected_public_files:
            stale.unlink()
    write_html(tables)

    (ROOT / "README.md").write_text(render_readme(tables, manifest_rows), encoding="utf-8")

    print(json.dumps({"latest_block": latest_block, "tables": {k: len(v[1]) for k, v in tables.items()}}, indent=2))


if __name__ == "__main__":
    main()
