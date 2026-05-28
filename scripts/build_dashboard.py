#!/usr/bin/env python3
from __future__ import annotations

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
ZERO = "0x0000000000000000000000000000000000000000"

OUTPUT_TABLES = [
    "mission3_metrics",
    "auction_feed",
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


def fetch_token_stats(block_tag: str) -> dict[str, str]:
    name = decode_abi_string(eth_call(WOOF, SELECTOR_NAME, block_tag))
    symbol = decode_abi_string(eth_call(WOOF, SELECTOR_SYMBOL, block_tag))
    decimals = decode_uint_call(eth_call(WOOF, SELECTOR_DECIMALS, block_tag))
    supply_raw = decode_uint_call(eth_call(WOOF, SELECTOR_TOTAL_SUPPLY, block_tag))
    eth_usd, eth_usd_source = fetch_eth_usd_price()
    return {
        "auction_house": AUCTION_HOUSE,
        "dog_nft": DEGEN_DOGS,
        "woof_token": WOOF,
        "woof_name": name,
        "woof_symbol": symbol,
        "woof_decimals": str(decimals),
        "woof_total_supply": decimal_str(supply_raw, decimals, 6),
        "woof_total_supply_raw": str(supply_raw),
        "eth_usd_price": decimal_str(int(eth_usd * 100), 2, 2) if eth_usd else "0",
        "eth_usd_source": eth_usd_source,
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



HIDDEN_UI_COLUMNS = {
    "dog_image_url",
    "dog_external_url",
    "bidder_url",
    "winner_url",
    "holder_url",
    "latest_bidder_url",
    "bidder_winner_url",
    "bidder_wallet",
    "bidder_winner_wallet",
    "winner_wallet",
    "holder_wallet",
    "bid_count",
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
        "bidder_winner": "high bidder / winner",
        "auction_time_utc": "last bid / settled",
        "last_bid_utc": "last bid",
        "settled_time_utc": "settled",
        "time_remaining": "time left",
    }
    return overrides.get(col, col.replace("_", " "))


def cell_url(col: str, row_data: dict[str, Any]) -> str:
    if col == "bidder_winner":
        return str(row_data.get("bidder_winner_url") or "")
    if col in {"bidder", "winner", "holder", "latest_bidder"}:
        return str(row_data.get(f"{col}_url") or "")
    if col == "dog":
        return str(row_data.get("dog_external_url") or "")
    return ""


def render_cell(col: str, value: Any, row_data: dict[str, Any]) -> str:
    text = "" if value is None else str(value)
    escaped = html.escape(text)
    lowered = col.lower()
    if col == "dog":
        image = str(row_data.get("dog_image_url") or "")
        url = cell_url(col, row_data)
        image_html = ""
        if image:
            image_html = f'<img class="dog-thumb" src="{html.escape(image, quote=True)}" alt="{html.escape(text, quote=True)} image" loading="lazy">'
        label_html = f'<span>{escaped}</span>'
        inner = f'<span class="dog-cell">{image_html}{label_html}</span>'
        if url:
            inner = f'<a class="dog-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{inner}</a>'
        return inner
    if lowered in {"status", "auction_state"}:
        tone = "ongoing" if "ongoing" in text or text == "live" else "settled" if "settled" in text else "neutral"
        return f'<span class="status-pill {tone}">{escaped}</span>'
    if col == "auction_time_utc" and text:
        status = str(row_data.get("status") or row_data.get("auction_state") or "").lower()
        label = "Settled" if "settled" in status else "Last bid"
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
        body.append("<tr>" + "".join(cells) + "</tr>")
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


def export_links(name: str) -> str:
    safe = html.escape(name)
    return f'<a href="generated/{safe}.csv" download>CSV</a><a href="generated/{safe}.json" download>JSON</a>'


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


def readme_table_links(csv_path: str) -> str:
    json_path = str(Path(csv_path).with_suffix(".json"))
    return f"[CSV]({csv_path}) / [JSON]({json_path})"


def render_readme(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]], manifest_rows: list[tuple[Any, ...]]) -> str:
    metrics = metric_lookup(tables)
    site_url = metric_value(metrics, "site_url", "https://ael-dev3.github.io/Degen-Dogs-Mission-3/")

    snapshot_rows = [
        ("site_url", site_url),
        ("Network", metric_value(metrics, "network", "base")),
        ("Snapshot block", metric_value(metrics, "latest_block")),
        ("Snapshot time UTC", metric_value(metrics, "latest_block_time_utc")),
        ("Current auction", f"Dog #{metric_value(metrics, 'current_auction_token_id')}"),
        ("Current bid", f"{metric_value(metrics, 'current_bid_eth')} ETH (${metric_value(metrics, 'current_bid_usd')})"),
        ("Current high bidder", metric_value(metrics, "current_bidder")),
        ("Auction ends UTC", metric_value(metrics, "current_auction_end_utc")),
        ("Created / settled auctions", f"{metric_value(metrics, 'created_auctions')} / {metric_value(metrics, 'settled_auctions')}"),
        ("WOOF holders", metric_value(metrics, "woof_holders")),
        ("Farcaster profiles resolved", metric_value(metrics, "farcaster_profiles_resolved")),
    ]
    snapshot_rows = [(label, value) for label, value in snapshot_rows if value and not value.endswith("#")]

    dataset_rows = [
        (str(table), f"`{csv_path}`", rows, readme_table_links(str(csv_path)))
        for table, csv_path, rows in manifest_rows
    ]

    contract_rows = [
        ("Auction house", metric_value(metrics, "auction_house")),
        ("Degen Dogs NFT", metric_value(metrics, "dog_nft")),
        ("WOOF token", metric_value(metrics, "woof_token")),
    ]
    contract_rows = [(label, address) for label, address in contract_rows if address]

    parts = [
        "# Degen Dogs Mission 3 Analytics",
        "Static, cached analytics for Degen Dogs Mission 3 on Base. The public site serves approved, precomputed result tables and downloadable CSV/JSON exports; it does not expose arbitrary visitor-run SQL.",
        "## Links\n\n- Live dashboard: [{0}]({0})\n- Query layer: [`sql/mission3_dashboard.sql`](sql/mission3_dashboard.sql)\n- Generated exports: [`generated/`](generated/)".format(site_url),
        "## Current snapshot\n\n" + markdown_table(["Field", "Value"], snapshot_rows).rstrip(),
        "## Published datasets\n\n" + markdown_table(["Table", "CSV path", "Rows", "Downloads"], dataset_rows).rstrip(),
        "## Data pipeline\n\n1. Fetch Base RPC logs and contract calls from the private Mac mini runner.\n2. Load decoded auction, WOOF, NFT metadata, and Farcaster identity rows into SQLite.\n3. Execute the approved SQL query layer and publish cached CSV/JSON/table artifacts to GitHub Pages.\n4. Refresh automatically from the private runner; the Mac mini is not the public host.",
        "## Verified contracts\n\n" + markdown_table(["Contract", "Address"], contract_rows).rstrip(),
        "## Caveats\n\n- The public site is a cached snapshot, not a live SQL database.\n- Current-auction state and high bidder are taken from the on-chain `auction()` snapshot.\n- Historical auction rows are reconstructed from verified Base auction-house events.\n- Archived SQL bundles may contain reconstructed auction SQL, SUP reward stubs, and patched contract references; the active dashboard is generated from this repository's query layer and Base RPC data.",
        "## Local development\n\n```bash\nnpm ci\nnpm run data\nnpm run build\n```\n\nInstall or refresh the hourly private-runner LaunchAgent:\n\n```bash\nnpm run refresh:install\n```",
    ]
    return "\n\n".join(parts).rstrip() + "\n"


def trait_chips(current: dict[str, str]) -> str:
    source = current.get("trait_rarity") or current.get("traits") or ""
    items = [item.strip() for item in source.split(";") if item.strip()]
    return "".join(f'<span>{html.escape(item)}</span>' for item in items)


def write_html(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
    metrics = metric_lookup(tables)
    current = current_lookup(tables)
    primary_parts = [table_html(name, *tables[name], featured=True) for name in PRIMARY_TABLES if name in tables]
    raw_parts = [
        table_html(name, cols, rows)
        for name, (cols, rows) in tables.items()
        if name not in PRIMARY_TABLES
    ]
    export_rows = []
    for name, (cols, rows) in tables.items():
        export_rows.append(
            f'<tr><td>{html.escape(name.replace("_", " "))}</td><td>{len(rows)}</td><td>{export_links(name)}</td></tr>'
        )

    dog = current.get("dog", f"Dog #{metrics.get('current_auction_token_id', '')}").strip() or "Current dog"
    bid = current.get("bid") or current.get("latest_bid") or f"{metrics.get('current_bid_eth', '0')} ETH"
    participant = current.get("bidder_winner") or current.get("bidder") or metrics.get("current_bidder", "")
    participant_url = current.get("bidder_winner_url") or current.get("bidder_url", "")
    participant_html = html.escape(participant)
    if participant_url and participant:
        participant_html = f'<a href="{html.escape(participant_url, quote=True)}" target="_blank" rel="noopener noreferrer">{participant_html}</a>'
    status = current.get("status") or current.get("auction_state", "")
    time_left = current.get("time_remaining", "")
    time_left_end = current.get("auction_end_utc") or current.get("end_time_utc", "")
    time_left_html = html.escape(time_left)
    if time_left and time_left_end:
        time_left_html = f'<span class="countdown" data-countdown-end="{html.escape(time_left_end, quote=True)}">{time_left_html}</span>'
    image = current.get("dog_image_url", "")
    image_html = ""
    if image:
        image_html = f'<img src="{html.escape(image, quote=True)}" alt="{html.escape(dog, quote=True)} image">'
    rarity = current.get("rarity", "")
    current_detail = "".join(
        [
            f'<span class="detail-status"><b>Status</b>{html.escape(status)}</span>' if status else "",
            f'<span class="detail-bid"><b>Bid</b>{html.escape(bid)}</span>' if bid else "",
            f'<span class="detail-time"><b>Time left</b>{time_left_html}</span>' if time_left else "",
            f'<span class="detail-rarity"><b>Rarity</b>{html.escape(rarity)}</span>' if rarity else "",
            f'<span class="detail-bidder"><b>High bidder</b>{participant_html}</span>' if participant else "",
        ]
    )
    chips = trait_chips(current)
    css = """
:root{color-scheme:light;--paper:#e8ded5;--ink:#0a0a0a;--panel:#fffaf3;--panel2:#f4ece3;--muted:#6d625b;--line:#cdbfb3;--accent:#e51b2f;--accent2:#b91325;--shadow:0 10px 26px rgba(10,10,10,.1);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}
html{background:var(--paper)}
body{margin:0;min-width:320px;background:var(--paper);color:var(--ink);font-size:14px}
a{color:var(--ink);text-decoration:none;transition:color .16s ease,background .16s ease,border-color .16s ease,box-shadow .16s ease,transform .16s ease}
a:hover{color:var(--accent2)}
.shell{width:min(1520px,calc(100% - 16px));margin:0 auto;padding:12px 0 24px}
.current-card,.table-card,.exports,details{background:var(--panel);border:2px solid var(--ink);box-shadow:var(--shadow)}
.current-card{display:grid;grid-template-columns:minmax(360px,.9fr) minmax(260px,.42fr);gap:0;margin-bottom:10px;min-height:300px;overflow:hidden}
.current-copy{padding:18px;display:flex;flex-direction:column;gap:10px;border-right:2px solid var(--ink)}
.eyebrow{display:flex;gap:8px;align-items:center;font-size:12px;font-weight:900;letter-spacing:.08em;text-transform:uppercase}
.dot{width:10px;height:10px;background:var(--accent);border:2px solid var(--ink);display:inline-block}
.current-copy h1{font-size:clamp(34px,6vw,72px);line-height:.9;margin:0;letter-spacing:-.075em;max-width:10ch}
.subtitle{margin:0;color:var(--muted);font-weight:700}
.current-detail{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:7px;margin-top:auto}
.current-detail span{display:flex;min-height:50px;flex-direction:column;justify-content:center;border:1.5px solid var(--ink);background:var(--panel2);padding:7px 9px;font-weight:900;line-height:1.18}
.current-detail .detail-bidder{grid-column:span 2}
.current-detail b,.time-cell b{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-bottom:3px}
.current-detail a,.identity a,td.time a{display:inline-flex;align-items:center;position:relative;width:max-content;max-width:100%;border:1.5px solid var(--ink);border-radius:999px;background:var(--panel2);padding:3px calc(8px + 1.05em) 3px 8px;font-weight:900;line-height:1.1;box-shadow:2px 2px 0 var(--ink)}
.current-detail a::after,.identity a::after,td.time a::after{content:'↗';position:absolute;inset-inline-end:7px;top:50%;transform:translateY(-50%);display:grid;place-items:center;width:.95em;height:.95em;font-size:.74em;line-height:1;color:var(--accent2);pointer-events:none}
.current-detail a:hover,.identity a:hover,td.time a:hover{background:#fff;border-color:var(--accent2);transform:translate(-1px,-1px);box-shadow:3px 3px 0 var(--accent2)}
.traits{display:flex;flex-wrap:wrap;gap:5px;max-height:78px;overflow:auto;padding-right:2px}
.traits span{border:1.5px solid var(--ink);background:var(--panel);padding:4px 6px;font-size:11px;font-weight:800;line-height:1.15}
.dog-stage{display:flex;align-items:center;justify-content:center;background:var(--panel2);min-height:280px;padding:10px;overflow:hidden}
.dog-stage img{width:min(100%,330px);height:min(100%,330px);object-fit:contain;filter:drop-shadow(0 10px 18px rgba(0,0,0,.16))}
.toolbar{display:flex;justify-content:flex-end;margin:0 0 10px}
.toolbar input{width:min(100%,390px);border:2px solid var(--ink);background:var(--panel);color:var(--ink);padding:9px 11px;font:inherit;font-weight:800;outline:none;box-shadow:4px 4px 0 var(--ink)}
.toolbar input:focus{border-color:var(--accent2);box-shadow:4px 4px 0 var(--accent2)}
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
.dog-cell{display:flex;align-items:center;gap:7px;font-weight:950}
.dog-thumb{width:38px;height:38px;border:1.5px solid var(--ink);background:var(--panel2);object-fit:cover;flex:none}
.dog-col{min-width:132px}
.identity{max-width:180px;overflow:hidden;text-overflow:ellipsis}
.identity a{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.exports{margin-top:10px;padding:12px}
.exports h2{margin:0 0 8px;font-size:15px;letter-spacing:-.02em}
.exports table{font-size:12.5px}
.exports a{display:inline-flex;margin-right:6px;border:1.5px solid var(--ink);border-radius:999px;background:var(--panel2);padding:3px 8px;font-weight:900;box-shadow:2px 2px 0 var(--ink)}
.exports a:hover{background:#fff;border-color:var(--accent2);box-shadow:2px 2px 0 var(--accent2)}
details{margin-top:10px;padding:0;overflow:hidden}
summary{cursor:pointer;padding:10px 12px;font-weight:950;text-transform:uppercase;letter-spacing:.07em;background:var(--panel2);border-bottom:2px solid var(--ink)}
.raw-grid{display:grid;gap:10px;padding:10px}
.raw-grid .table-scroll{max-height:420px;overflow:auto}
.countdown.ending{color:var(--accent2)}
.countdown.ended{color:var(--muted)}
@media (max-width:900px){.shell{width:min(100% - 10px,760px);padding:8px 0 18px}.current-card{grid-template-columns:1fr;min-height:0}.current-copy{border-right:0;border-bottom:2px solid var(--ink);padding:14px}.dog-stage{min-height:220px}.dog-stage img{max-height:240px}.toolbar{justify-content:stretch}.toolbar input{width:100%}.current-copy h1{font-size:clamp(34px,13vw,58px)}th,td{padding:6px 7px}table{font-size:12.5px}.traits{max-height:70px}}
@media (max-width:640px){body{font-size:13px}.shell{width:calc(100% - 8px);padding:4px 0 14px}.current-card,.table-card,.exports,details{border-width:1.5px;box-shadow:0 6px 16px rgba(10,10,10,.1)}.current-card{margin-bottom:8px}.current-copy{padding:12px;gap:8px;border-bottom:1.5px solid var(--ink)}.eyebrow{font-size:11px;gap:6px}.dot{width:8px;height:8px}.current-copy h1{font-size:clamp(42px,17vw,62px);max-width:none;line-height:.88}.subtitle{font-size:12px}.current-detail{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}.current-detail span{min-width:0;min-height:42px;padding:6px 7px;font-size:12.5px;overflow-wrap:anywhere}.current-detail span:nth-child(2),.current-detail span:nth-child(5){grid-column:1/-1}.current-detail b,.time-cell b{font-size:9px}.current-detail a,.identity a,td.time a{max-width:100%;font-size:12px;box-shadow:1.5px 1.5px 0 var(--ink)}.traits{display:grid;grid-template-columns:1fr;gap:4px;max-height:none;overflow:visible}.traits span{padding:3px 5px;font-size:9.5px;line-height:1.12;white-space:normal;overflow-wrap:anywhere}.dog-stage{min-height:166px;padding:4px}.dog-stage img{width:min(58vw,204px);height:min(58vw,204px)}.toolbar{margin:8px 0}.toolbar input{padding:8px 10px;font-size:13px;box-shadow:2px 2px 0 var(--ink)}table{font-size:12px}.featured-table .table-scroll{overflow:visible}.featured-table table{display:block;background:transparent}.featured-table caption.table-caption:not(.sr-only){display:flex;padding:7px 8px;border-bottom:1.5px solid var(--ink)}.featured-table thead{display:none}.featured-table tbody{display:grid;gap:7px;padding:7px;background:var(--panel2)}.featured-table tr{display:grid;grid-template-columns:auto minmax(0,1fr);gap:6px 8px;align-items:center;border:1.5px solid var(--ink);background:var(--panel);padding:7px;box-shadow:2px 2px 0 rgba(10,10,10,.18)}.featured-table tr:hover{background:var(--panel)}.featured-table td{display:block;min-width:0;border:0;padding:0;white-space:normal}.featured-table td::before{content:attr(data-label);display:block;margin-bottom:2px;color:var(--muted);font-size:8.5px;font-weight:950;letter-spacing:.08em;text-transform:uppercase}.featured-table td.state{align-self:start}.featured-table td.state::before{display:none}.featured-table td.dog-col{grid-column:2;grid-row:1/span 2}.featured-table td.identity{grid-column:1/-1;max-width:none}.featured-table td.num{grid-column:1/-1;text-align:left;font-size:13px;font-weight:950}.featured-table td.time{grid-column:1/-1}.featured-table td:not(.state):not(.dog-col):not(.identity):not(.num):not(.time){grid-column:1/-1}.dog-cell{gap:6px}.dog-thumb{width:34px;height:34px}.time-cell{gap:1px}.status-pill{padding:3px 6px;font-size:9px}.exports{padding:9px;margin-top:8px;overflow:hidden}.exports h2{font-size:14px}.exports table{font-size:11.5px}.exports th,.exports td{padding:6px 5px}.exports a{margin-right:3px;padding:2px 6px;box-shadow:1px 1px 0 var(--ink)}summary{padding:8px 10px;font-size:12px}.raw-grid{padding:8px;gap:8px}}
@media (max-width:420px){.traits{grid-template-columns:1fr}.dog-stage img{width:min(54vw,196px);height:min(54vw,196px)}}
@media (max-width:380px){.current-detail{grid-template-columns:1fr}.current-detail span:nth-child(2),.current-detail span:nth-child(5),.current-detail .detail-bidder{grid-column:1}.current-copy h1{font-size:clamp(38px,16vw,54px)}}

""".strip()
    script = """
const filter=document.getElementById('filter');
const key=v=>{const s=v.trim().replaceAll(',','').replace(/[()$]/g,'');const n=Number(s.split(' ')[0]);return s!==''&&Number.isFinite(n)?n:v.trim().toLowerCase();};
const parseUtc=value=>Date.parse(String(value||'').replace(' ','T')+'Z');
const formatDuration=seconds=>{const s=Math.max(0,Math.floor(seconds));const d=Math.floor(s/86400);const h=Math.floor((s%86400)/3600);const m=Math.floor((s%3600)/60);const sec=s%60;const clock=`${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;return d>0?`${d}d ${clock}`:clock;};
const updateCountdowns=()=>{const now=Date.now();document.querySelectorAll('[data-countdown-end]').forEach(el=>{const end=parseUtc(el.dataset.countdownEnd);if(!Number.isFinite(end))return;const seconds=Math.max(0,Math.floor((end-now)/1000));el.textContent=seconds===0?'ended':formatDuration(seconds);el.classList.toggle('ending',seconds>0&&seconds<3600);el.classList.toggle('ended',seconds===0);});};
const updateCounts=()=>{document.querySelectorAll('table').forEach(table=>{const rows=[...table.tBodies[0].rows];const visible=rows.filter(row=>!row.hidden).length;const total=table.caption?.querySelector('[data-total]');if(total){const suffix=visible===Number(total.dataset.total)?' rows':` / ${total.dataset.total} rows`;total.textContent=`${visible}${suffix}`;}});};
filter.addEventListener('input',()=>{const q=filter.value.trim().toLowerCase();document.querySelectorAll('tbody tr').forEach(tr=>{const table=tr.closest('table');const searchable=table?.closest('.primary-grid,details');tr.hidden=q!==''&&searchable&&!tr.textContent.toLowerCase().includes(q);});updateCounts();});
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
<title>Degen Dogs Mission 3 Auctions</title>
<style>{css}</style>
</head>
<body>
<div class="shell">
  <section class="current-card" aria-label="Current auction">
    <div class="current-copy">
      <div class="eyebrow"><span class="dot"></span>Mission 3 auction feed</div>
      <h1>{html.escape(dog)}</h1>
      <div class="current-detail">{current_detail}</div>
      <div class="traits" aria-label="Current dog traits and rarity">{chips}</div>
    </div>
    <a class="dog-stage" href="{html.escape(current.get('dog_external_url', '#'), quote=True)}" target="_blank" rel="noopener noreferrer">{image_html}</a>
  </section>
  <div class="toolbar"><input id="filter" type="search" aria-label="filter visible tables" placeholder="Search auctions, usernames, dogs" autocomplete="off"></div>
  <main class="primary-grid">{''.join(primary_parts)}</main>
  <section class="exports"><h2>Cached data exports</h2><table><thead><tr><th>table</th><th>rows</th><th>download</th></tr></thead><tbody>{''.join(export_rows)}</tbody></table></section>
  <details><summary>Advanced cached tables</summary><div class="raw-grid">{''.join(raw_parts)}</div></details>
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
    insert_rows(conn, "dog_metadata", dog_metadata, [("token_id", "INTEGER"), ("dog_name", "TEXT"), ("dog_image_url", "TEXT"), ("dog_external_url", "TEXT"), ("traits", "TEXT"), ("trait_rarity", "TEXT"), ("rarity", "TEXT"), ("rarity_score", "REAL")])
    insert_rows(conn, "token_stats", [{"metric": k, "value": v} for k, v in token_stats.items()], [("metric", "TEXT"), ("value", "TEXT")])
    insert_rows(conn, "current_auction_source", [current], [("token_id", "INTEGER"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("bidder", "TEXT"), ("settled", "INTEGER"), ("latest_block", "INTEGER"), ("latest_block_time_utc", "TEXT")])

    conn.executescript(SQL_PATH.read_text(encoding="utf-8"))

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
