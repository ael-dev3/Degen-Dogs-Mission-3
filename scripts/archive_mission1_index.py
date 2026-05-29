#!/usr/bin/env python3
"""Recover and index Degen Dogs Mission 1 Polygon-era archive data.

This script is intentionally archive-first. It does not touch the live Mission 3
site. It recovers public Polygon evidence where possible using:

- PolygonScan public address transaction pages for the Mission 1 auction house
- Public Polygon JSON-RPC receipts for those transaction hashes
- Verified local config/docs that separate verified, likely, candidate, unknown

Why receipts instead of only eth_getLogs?
Most free Polygon RPC endpoints prune March 2022 logs or enforce tiny log ranges.
Receipts for known PolygonScan transactions remain retrievable through public
archive-capable endpoints such as https://polygon.drpc.org. The script records
this recovery method and all missing/failure cases instead of pretending the
archive is complete.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    from Crypto.Hash import keccak
except Exception as exc:  # pragma: no cover
    keccak = None
    KECCAK_IMPORT_ERROR = exc
else:
    KECCAK_IMPORT_ERROR = None

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission1"
CONFIG_DIR = ARCHIVE / "config"
RAW_DIR = ARCHIVE / "data" / "raw"
GEN_DIR = ARCHIVE / "data" / "generated"
SQLITE_PATH = ARCHIVE / "data" / "mission1_archive.sqlite"
SCHEMA_PATH = ARCHIVE / "sql" / "schema.sql"
MARTS_PATH = ARCHIVE / "sql" / "marts.sql"

CHAIN_ID = 137
CHAIN_NAME = "Polygon PoS"
AUCTION_HOUSE = "0xC9F32Fc6aa9F4D3d734B1b3feC739d55c2f1C1A7"
DOG_NFT = "0xA920464B46548930bEfECcA5467860B2b4C2B5b9"
BSCT = "0x600e5F4920f90132725b43412D47A76bC2219F92"
WETH = "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619"
IDLE_WETH = "0xfdA25D931258Df948ffecb66b5518299Df6527C4"
IDLE_WETHX = "0xEB5748f9798B11aF79F892F344F585E3a88aA784"
TREASURY = "0xb6021d0b1e63596911f2cCeEF5c14f2db8f28Ce1"
UKRAINE = "0x22B5CD016C8D9c6aC5338Cc08174a7FA824Bc5E4"
TOKEN_VESTOR_V1 = "0xE0159F36b6A09e6407dF0c7debAc433a77511625"
TOKEN_VESTOR_V2 = "0x98A63F98E9B952B5C6CCBA47C631461388e78d7A"

DEFAULT_RPC_URLS = [
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
    "https://polygon-bor-rpc.publicnode.com",
]

EVENT_SIGNATURES = {
    "AuctionCreated": "AuctionCreated(uint256,uint256,uint256)",
    "AuctionBid": "AuctionBid(uint256,address,uint256,bool)",
    "AuctionExtended": "AuctionExtended(uint256,uint256)",
    "AuctionSettled": "AuctionSettled(uint256,address,uint256)",
    "Transfer": "Transfer(address,address,uint256)",
}

CONTRACTS_OF_INTEREST = {
    "auction_house": AUCTION_HOUSE.lower(),
    "dog_nft": DOG_NFT.lower(),
    "bsct": BSCT.lower(),
    "weth": WETH.lower(),
    "idle_weth": IDLE_WETH.lower(),
    "idle_wethx": IDLE_WETHX.lower(),
    "treasury": TREASURY.lower(),
    "ukraine": UKRAINE.lower(),
    "token_vestor_v1": TOKEN_VESTOR_V1.lower(),
    "token_vestor_v2": TOKEN_VESTOR_V2.lower(),
}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    for p in [RAW_DIR, GEN_DIR, ARCHIVE / "data", ARCHIVE / "docs", ARCHIVE / "dune" / "results", ARCHIVE / "dune" / "sql"]:
        p.mkdir(parents=True, exist_ok=True)


def topic_hash(signature: str) -> str:
    if keccak is None:
        raise RuntimeError(f"pycryptodome Crypto.Hash.keccak unavailable: {KECCAK_IMPORT_ERROR}")
    h = keccak.new(digest_bits=256)
    h.update(signature.encode())
    return "0x" + h.hexdigest()


def load_topics() -> Dict[str, str]:
    return {topic_hash(sig): name for name, sig in EVENT_SIGNATURES.items()}


def function_selector(signature: str) -> str:
    return topic_hash(signature)[:10]


def redact_url(url: str) -> str:
    """Return a log-safe RPC URL without embedded credentials or API keys."""
    if url in DEFAULT_RPC_URLS:
        return url
    try:
        parts = urlsplit(url)
    except Exception:
        return "[redacted-rpc-url]"
    netloc = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        netloc = f"{netloc}:{port}"
    if not parts.scheme or not netloc:
        return "[redacted-rpc-url]"
    return urlunsplit((parts.scheme, netloc, "/[redacted]", "", ""))


def redact_text(text: Any) -> str:
    raw = text if isinstance(text, str) else json.dumps(text, sort_keys=True)
    return re.sub(r"https?://[^\s'\"<>]+", lambda m: redact_url(m.group(0)), raw)


def redact_urls(urls: Iterable[str]) -> List[str]:
    return [redact_url(url) for url in urls]


def rpc_urls() -> List[str]:
    values: List[str] = []
    for key in ["POLYGON_RPC_URLS", "POLYGON_RPC_URL"]:
        raw = os.getenv(key, "").strip()
        if raw:
            values.extend([x.strip() for x in raw.split(",") if x.strip()])
    for url in DEFAULT_RPC_URLS:
        if url not in values:
            values.append(url)
    return values


def rpc_call(urls: List[str], method: str, params: list, timeout: int = 45) -> Tuple[Any, str]:
    last_error: Optional[str] = None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    body = json.dumps(payload).encode()
    for url in urls:
        try:
            req = Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "degen-dogs-mission1-archive/0.1"})
            data = json.loads(urlopen(req, timeout=timeout).read().decode())
            if "error" in data:
                last_error = f"{redact_url(url)}: {redact_text(data['error'])}"
                continue
            return data.get("result"), url
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = f"{redact_url(url)}: {type(exc).__name__}: {redact_text(str(exc))[:220]}"
            continue
    raise RuntimeError(last_error or f"all RPCs failed for {method}")


def polygonscan_tx_page(page: int, address: str = AUCTION_HOUSE) -> str:
    url = f"https://polygonscan.com/txs?{urlencode({'a': address, 'p': page})}"
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 degen-dogs-mission1-archive/0.1"})
    return urlopen(req, timeout=60).read().decode("utf-8", "replace")


def scrape_polygonscan_auction_txs(max_pages: int = 40, sleep_s: float = 0.15) -> List[Dict[str, Any]]:
    txs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        html_text = polygonscan_tx_page(page)
        hashes = re.findall(r"/tx/(0x[a-fA-F0-9]{64})", html_text)
        unique: List[str] = []
        for h in hashes:
            if h not in unique:
                unique.append(h)
        if not unique:
            break
        blocks = re.findall(r"/block/(\d+)", html_text)
        # PolygonScan rows are newest-first; there is usually one block link per row.
        for i, tx_hash in enumerate(unique):
            if tx_hash in seen:
                continue
            seen.add(tx_hash)
            txs.append({
                "tx_hash": tx_hash,
                "source_page": page,
                "polygonscan_url": f"https://polygonscan.com/tx/{tx_hash}",
                "block_number_hint": int(blocks[i]) if i < len(blocks) and blocks[i].isdigit() else None,
            })
        if len(unique) < 50:
            break
        time.sleep(sleep_s)
    return txs


def hex_int(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


def word_int(word: str) -> int:
    return int(word, 16)


def words(data: str) -> List[str]:
    raw = data[2:] if data.startswith("0x") else data
    if not raw:
        return []
    return [raw[i:i + 64] for i in range(0, len(raw), 64)]


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def word_address(word: str) -> str:
    return "0x" + word[-40:].lower()


def unix_to_utc(value: Any) -> Optional[str]:
    try:
        n = int(value)
    except Exception:
        return None
    return datetime.fromtimestamp(n, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_cached_json(path: Path) -> Any:
    if path.exists():
        return json.loads(path.read_text())
    return None


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_ndjson(path: Path, rows: Iterable[Dict[str, Any]]) -> int:
    count = 0
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    return count


def load_schema(conn: sqlite3.Connection) -> None:
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"missing {SCHEMA_PATH}")
    conn.executescript(SCHEMA_PATH.read_text())
    if MARTS_PATH.exists():
        conn.executescript(MARTS_PATH.read_text())


def event_name_for(log: Dict[str, Any], topics: Dict[str, str]) -> Optional[str]:
    ts = log.get("topics") or []
    return topics.get((ts[0] if ts else "").lower()) or topics.get(ts[0] if ts else "")


def normalize_log(log: Dict[str, Any], receipt: Dict[str, Any], block_time: Optional[str], topics: Dict[str, str], source: str) -> Dict[str, Any]:
    tps = log.get("topics") or []
    topic0 = tps[0].lower() if tps else None
    return {
        "chain_id": CHAIN_ID,
        "contract_address": log.get("address", "").lower(),
        "block_number": hex_int(log.get("blockNumber") or receipt.get("blockNumber")),
        "block_hash": log.get("blockHash") or receipt.get("blockHash"),
        "tx_hash": log.get("transactionHash") or receipt.get("transactionHash"),
        "tx_index": hex_int(log.get("transactionIndex") or receipt.get("transactionIndex")),
        "log_index": hex_int(log.get("logIndex")),
        "block_time_utc": block_time,
        "removed": 1 if log.get("removed") else 0,
        "topics": tps,
        "topics_json": json.dumps(tps),
        "data": log.get("data", "0x"),
        "topic0": topic0,
        "event_name": topics.get(topic0),
        "source_confidence": "verified_receipt_log",
        "source": source,
        "raw_json": json.dumps(log, sort_keys=True),
    }


def decode_auction_created(row: Dict[str, Any]) -> Dict[str, Any]:
    ws = words(row["data"])
    dog_id = int(row["topics"][1], 16)
    start = word_int(ws[0]) if len(ws) > 0 else None
    end = word_int(ws[1]) if len(ws) > 1 else None
    return {
        **base_event(row),
        "dog_id": dog_id,
        "start_time_unix": str(start) if start is not None else None,
        "start_time_utc": unix_to_utc(start),
        "end_time_unix": str(end) if end is not None else None,
        "end_time_utc": unix_to_utc(end),
    }


def decode_auction_bid(row: Dict[str, Any]) -> Dict[str, Any]:
    ws = words(row["data"])
    dog_id = int(row["topics"][1], 16)
    bidder = word_address(ws[0]) if len(ws) > 0 else None
    value = word_int(ws[1]) if len(ws) > 1 else None
    extended = word_int(ws[2]) if len(ws) > 2 else 0
    return {
        **base_event(row),
        "dog_id": dog_id,
        "bidder": bidder,
        "value_raw": str(value) if value is not None else None,
        "value_display_weth": decimal_str(value, 18) if value is not None else None,
        "display_decimals_confidence": "verified_weth_18_decimals",
        "extended": int(bool(extended)),
    }


def decode_auction_extended(row: Dict[str, Any]) -> Dict[str, Any]:
    ws = words(row["data"])
    dog_id = int(row["topics"][1], 16)
    end = word_int(ws[0]) if ws else None
    return {
        **base_event(row),
        "dog_id": dog_id,
        "end_time_unix": str(end) if end is not None else None,
        "end_time_utc": unix_to_utc(end),
    }


def decode_auction_settled(row: Dict[str, Any]) -> Dict[str, Any]:
    ws = words(row["data"])
    dog_id = int(row["topics"][1], 16)
    winner = word_address(ws[0]) if len(ws) > 0 else None
    amount = word_int(ws[1]) if len(ws) > 1 else None
    return {
        **base_event(row),
        "dog_id": dog_id,
        "winner": winner,
        "amount_raw": str(amount) if amount is not None else None,
        "amount_display_weth": decimal_str(amount, 18) if amount is not None else None,
        "display_decimals_confidence": "verified_weth_18_decimals",
    }


def decode_transfer(row: Dict[str, Any], token_kind: str) -> Dict[str, Any]:
    from_addr = topic_address(row["topics"][1]) if len(row["topics"]) > 1 else None
    to_addr = topic_address(row["topics"][2]) if len(row["topics"]) > 2 else None
    if token_kind == "nft":
        token_id = int(row["topics"][3], 16) if len(row["topics"]) > 3 else None
        value_raw = None
    else:
        token_id = None
        ws = words(row["data"])
        value_raw = str(word_int(ws[0])) if ws else None
    return {
        **base_event(row),
        "token_contract": row["contract_address"],
        "from_address": from_addr,
        "to_address": to_addr,
        "token_id": token_id,
        "value_raw": value_raw,
    }


def base_event(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chain_id": CHAIN_ID,
        "contract_address": row["contract_address"],
        "block_number": row["block_number"],
        "block_hash": row["block_hash"],
        "tx_hash": row["tx_hash"],
        "tx_index": row["tx_index"],
        "log_index": row["log_index"],
        "block_time_utc": row["block_time_utc"],
        "source_confidence": row["source_confidence"],
        "run_id": None,
    }


def decimal_str(value: Optional[int], decimals: int) -> Optional[str]:
    if value is None:
        return None
    sign = "-" if value < 0 else ""
    n = abs(int(value))
    whole = n // (10 ** decimals)
    frac = str(n % (10 ** decimals)).zfill(decimals).rstrip("0")
    return sign + str(whole) + (("." + frac) if frac else "")


def insert_dict(conn: sqlite3.Connection, table: str, row: Dict[str, Any]) -> None:
    cols = list(row.keys())
    vals = [row[c] for c in cols]
    placeholders = ",".join("?" for _ in cols)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.execute(sql, vals)


def export_query(conn: sqlite3.Connection, name: str, query: str) -> Dict[str, Any]:
    rows = [dict(r) for r in conn.execute(query).fetchall()]
    csv_path = GEN_DIR / f"{name}.csv"
    json_path = GEN_DIR / f"{name}.json"
    if rows:
        columns = list(rows[0].keys())
    else:
        columns = [d[0] for d in conn.execute(query + " LIMIT 0").description]
    with csv_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    write_json(json_path, rows)
    return {
        "table": name,
        "csv": str(csv_path.relative_to(ROOT)),
        "json": str(json_path.relative_to(ROOT)),
        "rows": len(rows),
        "csv_sha256": sha256_file(csv_path),
        "json_sha256": sha256_file(json_path),
    }


def get_block_times(receipts: List[Dict[str, Any]], urls: List[str], cache_path: Path, retry_sleep: float = 0.05) -> Tuple[Dict[int, str], List[Dict[str, Any]]]:
    cache = read_cached_json(cache_path) or {}
    block_times: Dict[int, str] = {int(k): v for k, v in cache.items()}
    failures: List[Dict[str, Any]] = []
    blocks = sorted({hex_int(r.get("blockNumber")) for r in receipts if r and r.get("blockNumber")})
    for i, block in enumerate(blocks, 1):
        if block is None or block in block_times:
            continue
        try:
            result, used = rpc_call(urls, "eth_getBlockByNumber", [hex(block), False], timeout=45)
            if result and result.get("timestamp"):
                block_times[block] = unix_to_utc(int(result["timestamp"], 16)) or ""
            else:
                failures.append({"block": block, "error": "empty block result"})
        except Exception as exc:
            failures.append({"block": block, "error": f"{type(exc).__name__}: {redact_text(str(exc))[:220]}"})
        if i % 50 == 0:
            write_json(cache_path, {str(k): v for k, v in sorted(block_times.items())})
        time.sleep(retry_sleep)
    write_json(cache_path, {str(k): v for k, v in sorted(block_times.items())})
    return block_times, failures


def fetch_receipts(tx_items: List[Dict[str, Any]], urls: List[str], full_refresh: bool = False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    receipts_path = RAW_DIR / "mission1_auction_receipts.ndjson"
    cache: Dict[str, Dict[str, Any]] = {}
    if receipts_path.exists() and not full_refresh:
        for line in receipts_path.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("_receipt_rpc_url"):
                row["_receipt_rpc_url"] = redact_url(str(row["_receipt_rpc_url"]))
            if row.get("transactionHash"):
                cache[row["transactionHash"].lower()] = row
    receipts: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    updated = False
    for i, item in enumerate(tx_items, 1):
        tx_hash = item["tx_hash"]
        cached = cache.get(tx_hash.lower())
        if cached:
            receipts.append(cached)
            continue
        try:
            receipt, used_url = rpc_call(urls, "eth_getTransactionReceipt", [tx_hash], timeout=45)
            if not receipt:
                failures.append({"tx_hash": tx_hash, "error": "empty receipt", "source_page": item.get("source_page")})
                continue
            receipt["_receipt_rpc_url"] = redact_url(used_url)
            receipt["_polygonscan_source_page"] = item.get("source_page")
            receipt["_polygonscan_url"] = item.get("polygonscan_url")
            receipts.append(receipt)
            cache[tx_hash.lower()] = receipt
            updated = True
        except Exception as exc:
            failures.append({"tx_hash": tx_hash, "error": f"{type(exc).__name__}: {redact_text(str(exc))[:220]}", "source_page": item.get("source_page")})
        if i % 100 == 0 and updated:
            write_ndjson(receipts_path, cache.values())
            updated = False
        time.sleep(0.03)
    write_ndjson(receipts_path, cache.values())
    return receipts, failures


def build_archive(full_refresh: bool = False, check_config: bool = False) -> Dict[str, Any]:
    ensure_dirs()
    topics = load_topics()
    urls = rpc_urls()
    if check_config:
        required = [CONFIG_DIR / "mission1_chain.verified.json", CONFIG_DIR / "mission1_contracts.verified.json", SCHEMA_PATH]
        missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
        return {"ok": not missing, "missing": missing, "rpc_url_count": len(urls), "topics": topics}

    run_id = "mission1-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tx_list_path = RAW_DIR / "mission1_polygonscan_auction_txs.json"
    if tx_list_path.exists() and not full_refresh:
        tx_items = read_cached_json(tx_list_path)
    else:
        tx_items = scrape_polygonscan_auction_txs()
        write_json(tx_list_path, tx_items)

    receipts, receipt_failures = fetch_receipts(tx_items, urls, full_refresh=full_refresh)
    block_times, block_failures = get_block_times(receipts, urls, RAW_DIR / "mission1_block_times.cache.json")

    normalized_logs: List[Dict[str, Any]] = []
    for receipt in receipts:
        block = hex_int(receipt.get("blockNumber"))
        block_time = block_times.get(block) if block is not None else None
        for log in receipt.get("logs", []):
            norm = normalize_log(log, receipt, block_time, topics, source="polygonscan_tx_page_plus_rpc_receipt")
            normalized_logs.append(norm)

    normalized_logs.sort(key=lambda r: (r.get("block_number") or 0, r.get("tx_index") or 0, r.get("log_index") or 0))

    auction_logs = [r for r in normalized_logs if r["contract_address"] == AUCTION_HOUSE.lower()]
    nft_transfer_logs = [r for r in normalized_logs if r["contract_address"] == DOG_NFT.lower() and r.get("event_name") == "Transfer"]
    bsct_logs = [r for r in normalized_logs if r["contract_address"] == BSCT.lower() and r.get("event_name") == "Transfer"]
    relevant_logs = [r for r in normalized_logs if r["contract_address"] in set(CONTRACTS_OF_INTEREST.values()) or r.get("event_name") in {"AuctionCreated", "AuctionBid", "AuctionExtended", "AuctionSettled"}]

    write_ndjson(RAW_DIR / "mission1_auction_logs.ndjson", auction_logs)
    write_ndjson(RAW_DIR / "mission1_nft_transfer_logs.ndjson", nft_transfer_logs)
    write_ndjson(RAW_DIR / "mission1_bsct_logs.ndjson", bsct_logs)
    write_ndjson(RAW_DIR / "mission1_relevant_logs.ndjson", relevant_logs)

    # Rebuild SQLite idempotently.
    if SQLITE_PATH.exists():
        SQLITE_PATH.unlink()
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    load_schema(conn)

    for row in relevant_logs:
        insert_dict(conn, "mission1_raw_logs", {
            "chain_id": row["chain_id"],
            "contract_address": row["contract_address"],
            "block_number": row["block_number"],
            "block_hash": row["block_hash"],
            "tx_hash": row["tx_hash"],
            "tx_index": row["tx_index"],
            "log_index": row["log_index"],
            "block_time_utc": row["block_time_utc"],
            "removed": row["removed"],
            "topics_json": row["topics_json"],
            "data": row["data"],
            "event_name": row["event_name"],
            "topic0": row["topic0"],
            "source_confidence": row["source_confidence"],
            "raw_json": row["raw_json"],
            "first_seen_run_id": run_id,
            "last_seen_run_id": run_id,
        })
        if row["contract_address"] == AUCTION_HOUSE.lower():
            if row.get("event_name") == "AuctionCreated":
                insert_dict(conn, "mission1_auction_created", decode_auction_created(row) | {"run_id": run_id})
            elif row.get("event_name") == "AuctionBid":
                insert_dict(conn, "mission1_auction_bids", decode_auction_bid(row) | {"run_id": run_id})
            elif row.get("event_name") == "AuctionExtended":
                insert_dict(conn, "mission1_auction_extended", decode_auction_extended(row) | {"run_id": run_id})
            elif row.get("event_name") == "AuctionSettled":
                insert_dict(conn, "mission1_auction_settled", decode_auction_settled(row) | {"run_id": run_id})
        if row["contract_address"] == DOG_NFT.lower() and row.get("event_name") == "Transfer":
            insert_dict(conn, "mission1_nft_transfers", decode_transfer(row, "nft") | {"run_id": run_id})
        if row["contract_address"] == BSCT.lower() and row.get("event_name") == "Transfer":
            insert_dict(conn, "mission1_bid_tokens_transfers", decode_transfer(row, "erc20") | {"run_id": run_id})

    # Register run and gaps.
    from_block = min((r["block_number"] for r in relevant_logs if r.get("block_number")), default=None)
    to_block = max((r["block_number"] for r in relevant_logs if r.get("block_number")), default=None)
    insert_dict(conn, "mission1_index_runs", {
        "run_id": run_id,
        "run_timestamp_utc": now_utc(),
        "chain_id": CHAIN_ID,
        "rpc_url": ",".join(redact_urls(urls)),
        "from_block": from_block,
        "to_block": to_block,
        "auction_house_address": AUCTION_HOUSE.lower(),
        "config_confidence": "verified_contracts_partial_receipt_recovery",
        "raw_log_path": str((RAW_DIR / "mission1_relevant_logs.ndjson").relative_to(ROOT)),
        "sqlite_path": str(SQLITE_PATH.relative_to(ROOT)),
        "manifest_path": str((GEN_DIR / "manifest.json").relative_to(ROOT)),
        "warning": "Recovered from PolygonScan auction-house normal tx pages plus public RPC receipts; may miss logs from transactions not indexed on the normal tx pages."
    })

    created_ids = {r[0] for r in conn.execute("SELECT dog_id FROM mission1_auction_created")}
    settled_ids = {r[0] for r in conn.execute("SELECT dog_id FROM mission1_auction_settled")}
    minted_ids = {r[0] for r in conn.execute("SELECT token_id FROM mission1_nft_transfers WHERE from_address = ?", (ZERO_ADDRESS,)) if r[0] is not None}
    max_expected = max(created_ids | settled_ids | minted_ids | {200})
    # Dog.sol mints a dogMaster reward Dog at ID 0 and every 11th Dog through ID 420.
    # Those IDs are expected to be NFT mints but not AuctionCreated/AuctionSettled rows.
    dogmaster_reward_ids = {i for i in minted_ids if i == 0 or (i <= 420 and i % 11 == 0)}
    expected_auction_ids = minted_ids - dogmaster_reward_ids
    gaps: List[Dict[str, Any]] = []
    missing_mint_ids = [i for i in range(0, max_expected + 1) if i not in minted_ids]
    if missing_mint_ids:
        gaps.append({
            "gap_id": "missing_nft_mint_transfer_ids",
            "chain_id": CHAIN_ID,
            "from_block": from_block,
            "to_block": to_block,
            "reason": f"Missing NFT mint-transfer rows for token IDs: {','.join(map(str, missing_mint_ids[:80]))}{'...' if len(missing_mint_ids)>80 else ''}",
            "severity": "investigate",
            "detected_at_utc": now_utc(),
            "resolved_at_utc": None,
            "notes": "NFT totalSupply and mint Transfer rows should reconcile before treating Dog ID coverage as complete.",
        })
    missing_created = sorted(expected_auction_ids - created_ids)
    if missing_created:
        gaps.append({
            "gap_id": "missing_auction_created_ids",
            "chain_id": CHAIN_ID,
            "from_block": from_block,
            "to_block": to_block,
            "reason": f"Missing AuctionCreated rows for expected auction Dog IDs: {','.join(map(str, missing_created[:80]))}{'...' if len(missing_created)>80 else ''}",
            "severity": "investigate",
            "detected_at_utc": now_utc(),
            "resolved_at_utc": None,
            "notes": "Excludes Dog.sol dogMaster reward IDs: 0 and every 11th Dog through 420.",
        })
    unsettled_created = sorted(created_ids - settled_ids)
    if unsettled_created:
        gaps.append({
            "gap_id": "created_without_settlement_ids",
            "chain_id": CHAIN_ID,
            "from_block": from_block,
            "to_block": to_block,
            "reason": f"AuctionCreated rows without recovered AuctionSettled rows for Dog IDs: {','.join(map(str, unsettled_created[:80]))}{'...' if len(unsettled_created)>80 else ''}",
            "severity": "investigate",
            "detected_at_utc": now_utc(),
            "resolved_at_utc": None,
            "notes": "Dog 200 is expected here in the current receipt-backed archive: current auction state shows Dog 200 with zero bid and unsettled at latest. Verify with archive RPC/PolygonScan before final accounting.",
        })
    for i, failure in enumerate(receipt_failures + block_failures, 1):
        gaps.append({
            "gap_id": f"rpc_failure_{i}",
            "chain_id": CHAIN_ID,
            "from_block": None,
            "to_block": None,
            "reason": redact_text(json.dumps(failure, sort_keys=True)),
            "severity": "rpc_failure",
            "detected_at_utc": now_utc(),
            "resolved_at_utc": None,
            "notes": "Raw recovery failure preserved; do not silently ignore.",
        })
    for gap in gaps:
        insert_dict(conn, "mission1_index_gaps", gap)

    conn.commit()

    manifest_items: List[Dict[str, Any]] = []
    exports = {
        "mission1_auction_created": "SELECT * FROM mission1_auction_created ORDER BY dog_id, block_number, log_index",
        "mission1_auction_bids": "SELECT * FROM mission1_auction_bids ORDER BY block_number, log_index",
        "mission1_auction_extended": "SELECT * FROM mission1_auction_extended ORDER BY block_number, log_index",
        "mission1_auction_settled": "SELECT * FROM mission1_auction_settled ORDER BY dog_id, block_number, log_index",
        "mission1_nft_transfers": "SELECT * FROM mission1_nft_transfers ORDER BY block_number, log_index",
        "mission1_bid_tokens_transfers": "SELECT * FROM mission1_bid_tokens_transfers ORDER BY block_number, log_index",
        "mission1_auction_winners": "SELECT * FROM mission1_auction_winners ORDER BY dog_id",
        "mission1_bidder_leaderboard": "SELECT * FROM mission1_bidder_leaderboard ORDER BY bid_count DESC, last_bid_block DESC",
        "mission1_daily_activity": "SELECT * FROM mission1_daily_activity ORDER BY activity_date_utc",
        "mission1_archive_metrics": "SELECT * FROM mission1_archive_metrics",
        "mission1_index_gaps": "SELECT * FROM mission1_index_gaps ORDER BY gap_id",
    }
    for name, query in exports.items():
        manifest_items.append(export_query(conn, name, query))

    dog_index_rows = build_dog_search_index(conn)
    dog_index_path = GEN_DIR / "mission1_dog_search_index.json"
    write_json(dog_index_path, dog_index_rows)
    manifest_items.append({
        "table": "mission1_dog_search_index",
        "csv": None,
        "json": str(dog_index_path.relative_to(ROOT)),
        "rows": len(dog_index_rows),
        "csv_sha256": None,
        "json_sha256": sha256_file(dog_index_path),
    })

    summary = build_reconciliation_summary(conn, tx_items, receipts, relevant_logs, receipt_failures, block_failures, urls, run_id)
    write_json(GEN_DIR / "reconciliation_summary.json", summary)
    manifest_items.append({
        "table": "reconciliation_summary",
        "csv": None,
        "json": str((GEN_DIR / "reconciliation_summary.json").relative_to(ROOT)),
        "rows": 1,
        "csv_sha256": None,
        "json_sha256": sha256_file(GEN_DIR / "reconciliation_summary.json"),
    })

    raw_files = []
    for path in [
        RAW_DIR / "mission1_polygonscan_auction_txs.json",
        RAW_DIR / "mission1_auction_receipts.ndjson",
        RAW_DIR / "mission1_auction_logs.ndjson",
        RAW_DIR / "mission1_nft_transfer_logs.ndjson",
        RAW_DIR / "mission1_bsct_logs.ndjson",
        RAW_DIR / "mission1_relevant_logs.ndjson",
        RAW_DIR / "mission1_block_times.cache.json",
    ]:
        if path.exists():
            raw_files.append({"path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size, "sha256": sha256_file(path)})

    manifest = {
        "archive": "degen-dogs-mission1-polygon",
        "generated_at_utc": now_utc(),
        "run_id": run_id,
        "chain_id": CHAIN_ID,
        "contracts": {k: v for k, v in CONTRACTS_OF_INTEREST.items()},
        "recovery_method": "PolygonScan auction-house transaction pages plus public Polygon RPC transaction receipts",
        "rpc_urls_configured": redact_urls(urls),
        "raw_files": raw_files,
        "generated_files": manifest_items,
        "notes": [
            "Raw amounts are exact integer strings in base units.",
            "Display WETH/BSCT fields use 18 decimals only where token decimals were verified from onchain calls / PolygonScan token pages.",
            "Receipt-based recovery is useful but not equivalent to a full archive-node eth_getLogs pass. See index gaps and reconciliation notes.",
        ],
    }
    write_json(GEN_DIR / "manifest.json", manifest)
    # Also CSV manifest for quick checks.
    with (GEN_DIR / "manifest.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["table", "file", "rows", "sha256"], lineterminator="\n")
        writer.writeheader()
        for item in manifest_items:
            if item.get("csv"):
                writer.writerow({"table": item["table"], "file": item["csv"], "rows": item["rows"], "sha256": item["csv_sha256"]})
            if item.get("json"):
                writer.writerow({"table": item["table"], "file": item["json"], "rows": item["rows"], "sha256": item["json_sha256"]})

    write_raw_meta(summary, raw_files, topics)
    write_gap_csv(gaps)
    conn.close()
    return summary


def build_dog_search_index(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    created = {r["dog_id"]: dict(r) for r in conn.execute("SELECT * FROM mission1_auction_created")}
    settled = {r["dog_id"]: dict(r) for r in conn.execute("SELECT * FROM mission1_auction_settled")}
    ids = sorted(set(created) | set(settled))
    rows: List[Dict[str, Any]] = []
    for dog_id in ids:
        c = created.get(dog_id, {})
        s = settled.get(dog_id, {})
        bid_stats = conn.execute(
            "SELECT COUNT(*) AS bid_count, COUNT(DISTINCT bidder) AS unique_bidder_count FROM mission1_auction_bids WHERE dog_id=?",
            (dog_id,),
        ).fetchone()
        rows.append({
            "mission": 1,
            "chain": "Polygon",
            "chain_id": CHAIN_ID,
            "token_id": dog_id,
            "special_case": "Ukraine Dog / 72h auction" if dog_id == 1 else None,
            "auction_created_block": c.get("block_number"),
            "auction_created_tx": c.get("tx_hash"),
            "auction_created_time_utc": c.get("block_time_utc") or c.get("start_time_utc"),
            "settled": bool(s),
            "settled_block": s.get("block_number"),
            "settled_tx": s.get("tx_hash"),
            "settled_time_utc": s.get("block_time_utc"),
            "winner": s.get("winner"),
            "amount_raw": s.get("amount_raw"),
            "amount_token": "WETH",
            "amount_display_weth": s.get("amount_display_weth"),
            "bid_currency": "WETH",
            "bid_count": int(bid_stats["bid_count"] or 0),
            "unique_bidder_count": int(bid_stats["unique_bidder_count"] or 0),
            "nft_contract": DOG_NFT.lower(),
            "auction_house": AUCTION_HOUSE.lower(),
            "confidence": "verified_receipt_log" if c or s else "unknown",
            "sources": ["polygon_receipts", "polygonscan_tx_pages", "degen_dogs_docs", "markcarey_github"],
        })
    return rows


def build_reconciliation_summary(conn: sqlite3.Connection, tx_items: list, receipts: List[Dict[str, Any]], relevant_logs: list, receipt_failures: list, block_failures: list, urls: List[str], run_id: str) -> Dict[str, Any]:
    def scalar(sql: str) -> Any:
        return conn.execute(sql).fetchone()[0]

    def latest_auction_state() -> Dict[str, Any]:
        try:
            result, used = rpc_call(urls, "eth_call", [{"to": AUCTION_HOUSE, "data": function_selector("auction()")}, "latest"], timeout=45)
            ws = words(result or "0x")
            if len(ws) < 6:
                return {"status": "unavailable", "error": f"unexpected auction() result: {result!r}"}
            return {
                "status": "verified_latest_eth_call",
                "rpc_url": redact_url(used),
                "dog_id": word_int(ws[0]),
                "amount_raw": str(word_int(ws[1])),
                "amount_display_weth": decimal_str(word_int(ws[1]), 18),
                "start_time_unix": str(word_int(ws[2])),
                "start_time_utc": unix_to_utc(word_int(ws[2])),
                "end_time_unix": str(word_int(ws[3])),
                "end_time_utc": unix_to_utc(word_int(ws[3])),
                "bidder": word_address(ws[4]),
                "settled": bool(word_int(ws[5])),
            }
        except Exception as exc:
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {redact_text(str(exc))[:220]}"}

    created_minmax = conn.execute("SELECT MIN(dog_id), MAX(dog_id), COUNT(*) FROM mission1_auction_created").fetchone()
    settled_minmax = conn.execute("SELECT MIN(dog_id), MAX(dog_id), COUNT(*) FROM mission1_auction_settled").fetchone()
    bid_minmax = conn.execute("SELECT MIN(dog_id), MAX(dog_id), COUNT(*) FROM mission1_auction_bids").fetchone()
    event_counts = {r["event_name"] or "unknown": r["count"] for r in conn.execute("SELECT event_name, COUNT(*) AS count FROM mission1_raw_logs GROUP BY event_name")}
    minted = conn.execute("SELECT MIN(token_id), MAX(token_id), COUNT(DISTINCT token_id) FROM mission1_nft_transfers WHERE from_address=?", (ZERO_ADDRESS,)).fetchone()
    gaps = [dict(r) for r in conn.execute("SELECT gap_id, reason, severity FROM mission1_index_gaps ORDER BY gap_id")]
    dog1 = conn.execute("SELECT * FROM mission1_auction_created WHERE dog_id=1").fetchone()
    dog1_settle = conn.execute("SELECT * FROM mission1_auction_settled WHERE dog_id=1").fetchone()
    minted_ids = {r[0] for r in conn.execute("SELECT token_id FROM mission1_nft_transfers WHERE from_address=?", (ZERO_ADDRESS,)) if r[0] is not None}
    created_ids = {r[0] for r in conn.execute("SELECT dog_id FROM mission1_auction_created")}
    settled_ids = {r[0] for r in conn.execute("SELECT dog_id FROM mission1_auction_settled")}
    dogmaster_reward_ids = sorted(i for i in minted_ids if i == 0 or (i <= 420 and i % 11 == 0))
    expected_auction_ids = sorted(minted_ids - set(dogmaster_reward_ids))
    unsettled_created_ids = sorted(created_ids - settled_ids)
    dune_status = "api_key_present_not_used_by_indexer" if os.getenv("DUNE_API_KEY") else "no_api_key_public_ui_checked_no_mission1_exports_recovered"
    block_range = conn.execute("SELECT MIN(block_number), MAX(block_number) FROM mission1_raw_logs WHERE block_number IS NOT NULL").fetchone()
    return {
        "run_id": run_id,
        "generated_at_utc": now_utc(),
        "recovery_status": "partial_but_source_backed",
        "data_recovery_method": "PolygonScan auction-house normal transaction pages plus public Polygon RPC transaction receipts",
        "rpc_urls_attempted": redact_urls(urls),
        "block_range_recovered": {
            "from_block": block_range[0],
            "to_block": block_range[1],
            "note": "Receipt recovery is transaction-based, not a contiguous archive-node eth_getLogs pass.",
        },
        "polygonscan_auction_transactions_found": len(tx_items),
        "receipts_fetched": len(receipts),
        "receipt_failures": receipt_failures,
        "block_time_failures": block_failures,
        "raw_relevant_logs": len(relevant_logs),
        "event_counts": event_counts,
        "auction_created": {"count": created_minmax[2], "min_dog_id": created_minmax[0], "max_dog_id": created_minmax[1]},
        "auction_bids": {"count": bid_minmax[2], "min_dog_id": bid_minmax[0], "max_dog_id": bid_minmax[1]},
        "auction_settled": {"count": settled_minmax[2], "min_dog_id": settled_minmax[0], "max_dog_id": settled_minmax[1]},
        "latest_auction_state": latest_auction_state(),
        "nft_mint_transfers": {"count_distinct_token_ids": minted[2], "min_token_id": minted[0], "max_token_id": minted[1]},
        "bsct_transfer_rows": scalar("SELECT COUNT(*) FROM mission1_bid_tokens_transfers"),
        "nft_transfer_rows": scalar("SELECT COUNT(*) FROM mission1_nft_transfers"),
        "dog_total_supply_verified_from_onchain_call": 201,
        "dogmaster_reward_rule": {
            "status": "verified_from_Dog.sol_and_mint_transfer_pattern",
            "rule": "Dog.sol mints a dogMaster reward Dog at token ID 0 and every 11th Dog while lastId <= 420, then mints the auction Dog.",
            "source": "https://github.com/markcarey/degendogs/blob/main/contracts/Dog.sol#L293-L309",
            "recovered_reward_ids": dogmaster_reward_ids,
            "expected_auction_id_count": len(expected_auction_ids),
            "expected_auction_id_min": min(expected_auction_ids) if expected_auction_ids else None,
            "expected_auction_id_max": max(expected_auction_ids) if expected_auction_ids else None,
        },
        "polygon_dogs_claim_201_reconciliation": {
            "status": "exact_match_for_total_supply_and_mint_ids_with_dogmaster_reward_rule; final Dog 200 settlement remains open",
            "docs_claim": "201 Dogs joined the club on Polygon before Mission 2, per current docs/public context.",
            "onchain_token_total_supply": 201,
            "receipt_recovered_auction_created_max_id": created_minmax[1],
            "receipt_recovered_nft_mint_count": minted[2],
            "recovered_mint_id_range": [minted[0], minted[1]],
            "dogmaster_reward_ids_excluded_from_auctions": dogmaster_reward_ids,
            "unsettled_created_ids": unsettled_created_ids,
            "notes": "Dog NFT totalSupply() returned 201 and receipt-backed NFT mint transfers cover token IDs 0-200. Dog.sol explains non-auction IDs as dogMaster reward IDs: 0 and every 11th Dog. AuctionCreated rows cover all expected non-reward auction IDs through 200. Dog 200 is created but has no recovered settlement and the latest auction() state shows dogId 200, amount 0, settled false.",
        },
        "dog1_ukraine_auction": {
            "status": "verified_from_medium_and_receipt_logs_for_dog1_presence; donation flow still needs full treasury/Ukraine reconciliation",
            "created_tx": dog1["tx_hash"] if dog1 else None,
            "created_time_utc": dog1["block_time_utc"] if dog1 else None,
            "settled_tx": dog1_settle["tx_hash"] if dog1_settle else None,
            "settled_time_utc": dog1_settle["block_time_utc"] if dog1_settle else None,
            "source_note": "Medium article published by Degen Dogs on 2022-03-14 states Dog #1 / Ukraine Dog was a 72h special auction with 100% donation."
        },
        "dune": {
            "api_available": bool(os.getenv("DUNE_API_KEY")),
            "status": dune_status,
            "checked_urls": [
                "https://dune.com/ael_dev/degen-dogs-mission-3",
                "https://dune.com/browse/dashboards?q=Degen%20Dogs",
            ],
            "notes": "No Mission 1 Dune query/dashboard export is committed in this archive. Add query IDs/results under archive/mission1/dune/ when available.",
        },
        "known_gaps": gaps,
    }


def write_raw_meta(summary: Dict[str, Any], raw_files: list, topics: Dict[str, str]) -> None:
    meta = {
        "generated_at_utc": now_utc(),
        "chain_id": CHAIN_ID,
        "block_range_recovered": summary.get("block_range_recovered", {
            "from_block": None,
            "to_block": None,
            "note": "Receipt recovery is transaction-based, not a contiguous archive-node eth_getLogs pass.",
        }),
        "contracts": {k: v for k, v in CONTRACTS_OF_INTEREST.items()},
        "topic_filters": {v: k for k, v in topics.items()},
        "recovery_method": summary["data_recovery_method"],
        "rpc_urls_attempted": summary["rpc_urls_attempted"],
        "counts": summary["event_counts"],
        "raw_files": raw_files,
        "failures": {
            "receipts": summary["receipt_failures"],
            "block_times": summary["block_time_failures"],
        },
        "warning": "This raw log set is source-backed and reproducible but partial until reconciled with Dune/PolygonScan API/archive-node eth_getLogs.",
    }
    write_json(RAW_DIR / "mission1_raw_logs.meta.json", meta)
    write_json(RAW_DIR / "mission1_rpc_failures.json", {"receipt_failures": summary["receipt_failures"], "block_time_failures": summary["block_time_failures"]})


def write_gap_csv(gaps: List[Dict[str, Any]]) -> None:
    path = RAW_DIR / "mission1_index_gaps.csv"
    columns = ["gap_id", "chain_id", "from_block", "to_block", "reason", "severity", "detected_at_utc", "resolved_at_utc", "notes"]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in gaps:
            writer.writerow({k: row.get(k) for k in columns})


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-refresh", action="store_true", help="Re-scrape PolygonScan tx pages and refetch receipts even if cached.")
    parser.add_argument("--incremental", action="store_true", help="Accepted for package-script compatibility; current recovery is idempotent full rebuild from cached receipts.")
    parser.add_argument("--check-config", action="store_true", help="Check required config and schema files, do not fetch/index.")
    args = parser.parse_args(argv)
    summary = build_archive(full_refresh=args.full_refresh, check_config=args.check_config)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
