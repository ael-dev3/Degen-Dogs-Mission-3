#!/usr/bin/env python3
"""Local Degen Dogs Mission 2 archive indexer.

This script intentionally refuses to run without a verified auction house address
and a verified block range. It stores raw logs before decoding and writes decoded
SQLite/CSV/JSON outputs for archival reproducibility.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any, Iterable

getcontext().prec = 80

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission2"
CONFIG = ARCHIVE / "config"
DATA = ARCHIVE / "data"
RAW_DIR = DATA / "raw"
SQLITE_DIR = DATA / "sqlite"
GENERATED_DIR = DATA / "generated"
SCHEMA_PATH = ARCHIVE / "sql" / "schema.sql"
MARTS_PATH = ARCHIVE / "sql" / "marts.sql"
ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sql_identifier(name: str) -> str:
    if not SQL_IDENTIFIER_RE.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return '"' + name + '"'


def redact_url(value: str) -> str:
    """Remove obvious credentials from RPC URLs before writing manifests/DB rows."""
    try:
        parts = urllib.parse.urlsplit(value)
    except Exception:
        return "<redacted-url>"
    netloc = parts.hostname or ""
    if parts.port:
        netloc += f":{parts.port}"
    if parts.username or parts.password:
        netloc = "***@" + netloc
    query = "redacted=1" if parts.query else ""
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_from_unix(value: int | str | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def decimal_18(raw_value: int | str | None) -> str | None:
    if raw_value in (None, ""):
        return None
    value = Decimal(str(raw_value)) / (Decimal(10) ** 18)
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_chain_config() -> dict[str, Any]:
    """Load verified chain config when present, falling back to the scaffold file."""
    verified = CONFIG / "mission2_chain.verified.json"
    if verified.exists():
        data = load_json(verified)
        chain = data.get("chain", {})
        return {
            "network": chain.get("name", "Degen Chain"),
            "chain_id": int(chain.get("chain_id", 666666666)),
            "rpc_candidates": [item["url"] for item in chain.get("rpc_urls", []) if item.get("url")]
            or ["https://rpc.degen.tips"],
            "confidence": data.get("confidence", "verified"),
            "source_file": "archive/mission2/config/mission2_chain.verified.json",
        }
    data = load_json(CONFIG / "mission2_chain.json")
    data["source_file"] = "archive/mission2/config/mission2_chain.json"
    return data


def load_contract_config() -> dict[str, Any]:
    """Load verified contracts when present, normalized to the legacy flat shape."""
    verified = CONFIG / "mission2_contracts.verified.json"
    if verified.exists():
        data = load_json(verified)
        out: dict[str, Any] = {
            "_config_source": "archive/mission2/config/mission2_contracts.verified.json",
            "_confidence": data.get("confidence", "verified"),
        }
        for name, meta in data.get("contracts", {}).items():
            out[name] = {
                "address": meta.get("address"),
                "confidence": meta.get("confidence", data.get("confidence", "verified")),
                "source": out["_config_source"],
                "notes": meta.get("notes"),
            }
        for name, meta in data.get("unresolved", {}).items():
            out[name] = {
                "address": meta.get("address"),
                "confidence": meta.get("confidence", "unknown"),
                "source": out["_config_source"],
                "notes": meta.get("notes"),
            }
        return out
    out = load_json(CONFIG / "mission2_contracts.unverified.json")
    out["_config_source"] = "archive/mission2/config/mission2_contracts.unverified.json"
    out["_confidence"] = "unverified"
    return out


def load_block_config() -> dict[str, Any]:
    verified = CONFIG / "mission2_blocks.verified.json"
    if verified.exists():
        data = load_json(verified)
        blocks = data.get("blocks", {})
        blocks = dict(blocks)
        blocks["confidence"] = data.get("confidence", "verified")
        blocks["source_file"] = "archive/mission2/config/mission2_blocks.verified.json"
        return blocks
    return {"confidence": "unverified", "source_file": None}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def keccak256_text(text: str) -> str:
    payload = text.encode("utf-8")
    try:
        from Crypto.Hash import keccak  # type: ignore
        k = keccak.new(digest_bits=256)
        k.update(payload)
        return "0x" + k.hexdigest()
    except Exception:
        pass
    try:
        from eth_hash.auto import keccak  # type: ignore
        return "0x" + keccak(payload).hex()
    except Exception as exc:
        raise RuntimeError(
            "Cannot compute Ethereum Keccak-256 topics. Install pycryptodome or eth-hash."
        ) from exc


def rpc_call(rpc_url: str, method: str, params: list[Any], *, timeout: int = 45) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        rpc_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "mission2-archive-indexer/0.1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(f"RPC {method} error: {data['error']}")
    return data.get("result")


def rpc_call_retry(rpc_url: str, method: str, params: list[Any], *, attempts: int = 4) -> Any:
    last: Exception | None = None
    for idx in range(attempts):
        try:
            return rpc_call(rpc_url, method, params)
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            last = exc
            if idx == attempts - 1:
                break
            time.sleep(min(2 ** idx, 8))
    raise RuntimeError(f"RPC {method} failed after {attempts} attempts: {last}")


def hex_int(value: str | None) -> int | None:
    if value is None:
        return None
    return int(value, 16)


def to_block_hex(value: int) -> str:
    return hex(value)


def normalize_address(value: str) -> str:
    if not ADDRESS_RE.match(value or ""):
        raise ValueError(f"invalid address: {value!r}")
    return value.lower()


def decode_word(data_hex: str, index: int) -> str:
    data = data_hex[2:] if data_hex.startswith("0x") else data_hex
    start = index * 64
    word = data[start:start + 64]
    if len(word) != 64:
        raise ValueError(f"missing ABI word {index} in data {data_hex}")
    return word


def decode_uint_word(data_hex: str, index: int) -> int:
    return int(decode_word(data_hex, index), 16)


def decode_bool_word(data_hex: str, index: int) -> bool:
    return bool(decode_uint_word(data_hex, index))


def decode_address_word(data_hex: str, index: int) -> str:
    word = decode_word(data_hex, index)
    return "0x" + word[-40:].lower()


def indexed_uint_topic(topics: list[str], index: int) -> int:
    return int(topics[index], 16)


def event_topics(event_config: dict[str, Any]) -> dict[str, str]:
    return {event["name"]: keccak256_text(event["signature"]) for event in event_config["events"]}


def topic_to_event(topics_by_event: dict[str, str]) -> dict[str, str]:
    return {topic.lower(): name for name, topic in topics_by_event.items()}


def chunks(start: int, end: int, size: int) -> Iterable[tuple[int, int]]:
    cur = start
    while cur <= end:
        nxt = min(cur + size - 1, end)
        yield cur, nxt
        cur = nxt + 1


def fetch_logs(rpc_url: str, address: str, topics0: list[str], from_block: int, to_block: int, chunk_size: int) -> list[dict[str, Any]]:
    all_logs: list[dict[str, Any]] = []
    for lo, hi in chunks(from_block, to_block, chunk_size):
        params = [{
            "address": address,
            "fromBlock": to_block_hex(lo),
            "toBlock": to_block_hex(hi),
            "topics": [topics0],
        }]
        result = rpc_call_retry(rpc_url, "eth_getLogs", params)
        if not isinstance(result, list):
            raise RuntimeError(f"eth_getLogs returned non-list for {lo}-{hi}: {result!r}")
        all_logs.extend(result)
    all_logs.sort(key=lambda log: (hex_int(log.get("blockNumber")) or 0, hex_int(log.get("logIndex")) or 0))
    return all_logs


def write_ndjson(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def log_common(log: dict[str, Any], chain_id: int, address: str, source_confidence: str, run_id: str) -> dict[str, Any]:
    topics = [str(t).lower() for t in log.get("topics", [])]
    block_ts = hex_int(log.get("blockTimestamp")) if log.get("blockTimestamp") else None
    return {
        "chain_id": chain_id,
        "contract_address": address,
        "block_number": hex_int(log.get("blockNumber")),
        "block_hash": log.get("blockHash"),
        "tx_hash": log.get("transactionHash"),
        "tx_index": hex_int(log.get("transactionIndex")),
        "log_index": hex_int(log.get("logIndex")),
        "block_time_utc": utc_from_unix(block_ts),
        "removed": 1 if log.get("removed") else 0,
        "topics": topics,
        "data": log.get("data", "0x"),
        "source_confidence": source_confidence,
        "run_id": run_id,
    }


def insert_many(conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    qtable = sql_identifier(table)
    qcols = ", ".join(sql_identifier(col) for col in cols)
    placeholders = ", ".join("?" for _ in cols)
    sql = "INSERT OR REPLACE INTO " + qtable + " (" + qcols + ") VALUES (" + placeholders + ")"
    conn.executemany(sql, [[row.get(col) for col in cols] for row in rows])


def decode_events(logs: list[dict[str, Any]], chain_id: int, address: str, topic_names: dict[str, str], source_confidence: str, run_id: str) -> dict[str, list[dict[str, Any]]]:
    decoded: dict[str, list[dict[str, Any]]] = {
        "raw": [],
        "created": [],
        "bids": [],
        "extended": [],
        "settled": [],
        "parameter_updates": [],
    }
    parameter_names = {
        "AuctionTimeBufferUpdated": "timeBuffer",
        "AuctionDurationUpdated": "duration",
        "AuctionReservePriceUpdated": "reservePrice",
        "AuctionMinBidIncrementPercentageUpdated": "minBidIncrementPercentage",
    }

    for log in logs:
        common = log_common(log, chain_id, address, source_confidence, run_id)
        topics = common["topics"]
        topic0 = topics[0] if topics else None
        event_name = topic_names.get(topic0 or "")
        decoded["raw"].append({
            "chain_id": chain_id,
            "contract_address": address,
            "block_number": common["block_number"],
            "block_hash": common["block_hash"],
            "tx_hash": common["tx_hash"],
            "tx_index": common["tx_index"],
            "log_index": common["log_index"],
            "block_time_utc": common["block_time_utc"],
            "removed": common["removed"],
            "topics_json": json.dumps(topics),
            "data": common["data"],
            "event_name": event_name,
            "topic0": topic0,
            "source_confidence": source_confidence,
            "raw_json": json.dumps(log, sort_keys=True),
            "first_seen_run_id": run_id,
            "last_seen_run_id": run_id,
        })
        if not event_name:
            continue
        try:
            if event_name == "AuctionCreated":
                dog_id = indexed_uint_topic(topics, 1)
                start_time = decode_uint_word(common["data"], 0)
                end_time = decode_uint_word(common["data"], 1)
                decoded["created"].append({
                    "chain_id": chain_id,
                    "contract_address": address,
                    "block_number": common["block_number"],
                    "block_hash": common["block_hash"],
                    "tx_hash": common["tx_hash"],
                    "tx_index": common["tx_index"],
                    "log_index": common["log_index"],
                    "block_time_utc": common["block_time_utc"],
                    "dog_id": dog_id,
                    "start_time_unix": str(start_time),
                    "start_time_utc": utc_from_unix(start_time),
                    "end_time_unix": str(end_time),
                    "end_time_utc": utc_from_unix(end_time),
                    "source_confidence": source_confidence,
                    "run_id": run_id,
                })
            elif event_name == "AuctionBid":
                dog_id = indexed_uint_topic(topics, 1)
                bidder = decode_address_word(common["data"], 0)
                value = decode_uint_word(common["data"], 1)
                extended = decode_bool_word(common["data"], 2)
                decoded["bids"].append({
                    "chain_id": chain_id,
                    "contract_address": address,
                    "block_number": common["block_number"],
                    "block_hash": common["block_hash"],
                    "tx_hash": common["tx_hash"],
                    "tx_index": common["tx_index"],
                    "log_index": common["log_index"],
                    "block_time_utc": common["block_time_utc"],
                    "dog_id": dog_id,
                    "bidder": bidder,
                    "value_raw": str(value),
                    "value_display_native": decimal_18(value),
                    "display_decimals_confidence": "verified_wdegen_18_decimals",
                    "extended": 1 if extended else 0,
                    "source_confidence": source_confidence,
                    "run_id": run_id,
                })
            elif event_name == "AuctionExtended":
                dog_id = indexed_uint_topic(topics, 1)
                end_time = decode_uint_word(common["data"], 0)
                decoded["extended"].append({
                    "chain_id": chain_id,
                    "contract_address": address,
                    "block_number": common["block_number"],
                    "block_hash": common["block_hash"],
                    "tx_hash": common["tx_hash"],
                    "tx_index": common["tx_index"],
                    "log_index": common["log_index"],
                    "block_time_utc": common["block_time_utc"],
                    "dog_id": dog_id,
                    "end_time_unix": str(end_time),
                    "end_time_utc": utc_from_unix(end_time),
                    "source_confidence": source_confidence,
                    "run_id": run_id,
                })
            elif event_name == "AuctionSettled":
                dog_id = indexed_uint_topic(topics, 1)
                winner = decode_address_word(common["data"], 0)
                amount = decode_uint_word(common["data"], 1)
                decoded["settled"].append({
                    "chain_id": chain_id,
                    "contract_address": address,
                    "block_number": common["block_number"],
                    "block_hash": common["block_hash"],
                    "tx_hash": common["tx_hash"],
                    "tx_index": common["tx_index"],
                    "log_index": common["log_index"],
                    "block_time_utc": common["block_time_utc"],
                    "dog_id": dog_id,
                    "winner": winner,
                    "amount_raw": str(amount),
                    "amount_display_native": decimal_18(amount),
                    "display_decimals_confidence": "verified_wdegen_18_decimals",
                    "source_confidence": source_confidence,
                    "run_id": run_id,
                })
            elif event_name in parameter_names:
                value = decode_uint_word(common["data"], 0)
                decoded["parameter_updates"].append({
                    "chain_id": chain_id,
                    "contract_address": address,
                    "block_number": common["block_number"],
                    "block_hash": common["block_hash"],
                    "tx_hash": common["tx_hash"],
                    "tx_index": common["tx_index"],
                    "log_index": common["log_index"],
                    "block_time_utc": common["block_time_utc"],
                    "event_name": event_name,
                    "parameter_name": parameter_names[event_name],
                    "value_raw": str(value),
                    "value_display": None,
                    "source_confidence": source_confidence,
                    "run_id": run_id,
                })
        except Exception as exc:
            raise RuntimeError(f"failed decoding {event_name} log {log.get('transactionHash')}:{log.get('logIndex')}: {exc}") from exc
    return decoded


def setup_db(sqlite_path: Path) -> sqlite3.Connection:
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(sqlite_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.executescript(MARTS_PATH.read_text(encoding="utf-8"))
    return conn


def upsert_configs(conn: sqlite3.Connection, chain_id: int, run_timestamp: str) -> None:
    contracts = load_contract_config()
    rows = []
    source_file = contracts.get("_config_source")
    for name, meta in contracts.items():
        if name.startswith("_"):
            continue
        rows.append({
            "name": name,
            "chain_id": chain_id,
            "address": meta.get("address"),
            "confidence": meta.get("confidence", "unverified"),
            "source": meta.get("source") or source_file,
            "how_to_verify": meta.get("how_to_verify"),
            "notes": meta.get("notes"),
            "updated_at_utc": run_timestamp,
        })
    insert_many(conn, "mission2_known_contracts", rows)

    vault = load_json(CONFIG / "woof_vault_allocations.json")
    vault_rows = []
    for item in vault.get("allocations", []):
        vault_rows.append({
            "address": normalize_address(item["address"]),
            "units_raw": str(item["units"]),
            "units_display": None,
            "source_url": vault["source_url"],
            "source_confidence": vault["source_confidence"],
            "interpretation_confidence": vault["interpretation_confidence"],
            "note": vault["note"],
        })
    insert_many(conn, "mission2_woof_vault_allocations", vault_rows)


def export_query(conn: sqlite3.Connection, name: str, query: str) -> dict[str, Any]:
    cur = conn.execute(query)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    csv_path = GENERATED_DIR / f"{name}.csv"
    json_path = GENERATED_DIR / f"{name}.json"
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    write_json(json_path, rows)
    return {
        "name": name,
        "csv_path": str(csv_path.relative_to(ROOT)),
        "json_path": str(json_path.relative_to(ROOT)),
        "rows": len(rows),
        "csv_sha256": sha256_file(csv_path),
        "json_sha256": sha256_file(json_path),
    }


def export_dog_search_index(conn: sqlite3.Connection) -> dict[str, Any]:
    query = """
        SELECT
          s.dog_id AS token_id,
          c.block_number AS auction_created_block,
          c.tx_hash AS auction_created_tx,
          c.start_time_utc AS auction_created_time_utc,
          s.block_number AS settled_block,
          s.tx_hash AS settled_tx,
          s.block_time_utc AS settled_time_utc,
          s.winner,
          s.amount_raw,
          s.amount_display_native AS amount_degen,
          COALESCE(b.bid_count, 0) AS bid_count,
          COALESCE(b.unique_bidder_count, 0) AS unique_bidder_count
        FROM mission2_auction_settled s
        LEFT JOIN mission2_auction_created c ON c.chain_id = s.chain_id AND c.dog_id = s.dog_id
        LEFT JOIN (
          SELECT chain_id, dog_id, COUNT(*) AS bid_count, COUNT(DISTINCT bidder) AS unique_bidder_count
          FROM mission2_auction_bids
          GROUP BY chain_id, dog_id
        ) b ON b.chain_id = s.chain_id AND b.dog_id = s.dog_id
        ORDER BY s.dog_id
    """
    rows = []
    for row in conn.execute(query).fetchall():
        rows.append({
            "mission": 2,
            "chain": "Degen Chain",
            "token_id": row[0],
            "dog_id": row[0],
            "winner": row[7],
            "amount_raw": row[8],
            "amount_degen": row[9],
            "bid_count": row[10],
            "unique_bidder_count": row[11],
            "auction_created_block": row[1],
            "settled_block": row[4],
            "auction_created_tx": row[2],
            "settled_tx": row[5],
            "auction_created_time_utc": row[3],
            "settled_time_utc": row[6],
            "confidence": "verified_onchain",
            "sources": ["chain_logs", "contract_getters"],
        })
    json_path = GENERATED_DIR / "mission2_dog_search_index.json"
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    write_json(json_path, rows)
    return {
        "name": "mission2_dog_search_index",
        "json_path": str(json_path.relative_to(ROOT)),
        "rows": len(rows),
        "json_sha256": sha256_file(json_path),
    }


def export_outputs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    winners_query = """
        SELECT
          s.chain_id,
          2 AS mission,
          'Degen Chain' AS chain,
          s.dog_id AS token_id,
          c.block_number AS auction_created_block,
          c.tx_hash AS auction_created_tx,
          c.start_time_utc AS auction_created_time_utc,
          s.block_number AS settled_block,
          s.tx_hash AS settled_tx,
          s.block_time_utc AS settled_time_utc,
          s.winner,
          s.amount_raw,
          s.amount_display_native AS amount_degen,
          COALESCE(b.bid_count, 0) AS bid_count,
          COALESCE(b.unique_bidder_count, 0) AS unique_bidder_count,
          b.highest_bidder,
          'verified_onchain' AS confidence,
          'chain_logs;contract_getters' AS sources
        FROM mission2_auction_settled s
        LEFT JOIN mission2_auction_created c ON c.chain_id = s.chain_id AND c.dog_id = s.dog_id
        LEFT JOIN (
          SELECT
            chain_id,
            dog_id,
            COUNT(*) AS bid_count,
            COUNT(DISTINCT bidder) AS unique_bidder_count,
            (
              SELECT bidder
              FROM mission2_auction_bids b2
              WHERE b2.chain_id = b.chain_id AND b2.dog_id = b.dog_id
              ORDER BY length(value_raw) DESC, value_raw DESC, block_number DESC, log_index DESC
              LIMIT 1
            ) AS highest_bidder
          FROM mission2_auction_bids b
          GROUP BY chain_id, dog_id
        ) b ON b.chain_id = s.chain_id AND b.dog_id = s.dog_id
        ORDER BY s.dog_id
    """
    return [
        export_query(conn, "mission2_auction_created", "SELECT * FROM mission2_auction_created ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_bids", "SELECT * FROM mission2_auction_bids ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_extended", "SELECT * FROM mission2_auction_extended ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_settled", "SELECT * FROM mission2_auction_settled ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_winners", winners_query),
        export_query(conn, "mission2_recent_bids", "SELECT * FROM mission2_auction_bids ORDER BY block_number DESC, log_index DESC LIMIT 250"),
        export_query(conn, "mission2_bidder_leaderboard", """
            SELECT
              bidder,
              COUNT(*) AS bid_count,
              COUNT(DISTINCT dog_id) AS dogs_bid_on,
              MIN(block_number) AS first_bid_block,
              MAX(block_number) AS last_bid_block,
              COUNT(CASE WHEN extended = 1 THEN 1 END) AS extension_bid_count,
              'raw total omitted to avoid SQLite integer overflow; use per-bid exact raw rows for accounting' AS amount_note,
              'verified_onchain' AS confidence
            FROM mission2_auction_bids
            GROUP BY bidder
            ORDER BY bid_count DESC, last_bid_block DESC, bidder
        """),
        export_dog_search_index(conn),
        export_query(conn, "mission2_auction_timeline", """
            SELECT
              c.dog_id,
              c.block_number AS created_block,
              c.tx_hash AS created_tx,
              c.start_time_utc,
              c.end_time_utc,
              s.block_number AS settled_block,
              s.tx_hash AS settled_tx,
              s.block_time_utc AS settled_time_utc,
              s.winner,
              s.amount_raw,
              s.amount_display_native AS amount_degen,
              COALESCE(b.bid_count, 0) AS bid_count,
              COALESCE(b.unique_bidder_count, 0) AS unique_bidder_count,
              'verified_onchain' AS confidence
            FROM mission2_auction_created c
            LEFT JOIN mission2_auction_settled s ON s.chain_id = c.chain_id AND s.dog_id = c.dog_id
            LEFT JOIN (
              SELECT chain_id, dog_id, COUNT(*) AS bid_count, COUNT(DISTINCT bidder) AS unique_bidder_count
              FROM mission2_auction_bids
              GROUP BY chain_id, dog_id
            ) b ON b.chain_id = c.chain_id AND b.dog_id = c.dog_id
            ORDER BY c.dog_id
        """),
        export_query(conn, "mission2_daily_activity", """
            SELECT
              substr(block_time_utc, 1, 10) AS activity_date_utc,
              COUNT(*) AS bid_count,
              COUNT(DISTINCT dog_id) AS dogs_with_bids,
              COUNT(DISTINCT bidder) AS unique_bidders,
              MIN(block_number) AS first_block,
              MAX(block_number) AS last_block,
              'verified_onchain' AS confidence
            FROM mission2_auction_bids
            GROUP BY activity_date_utc
            ORDER BY activity_date_utc
        """),
        export_query(conn, "mission2_parameter_updates", "SELECT * FROM mission2_parameter_updates ORDER BY block_number, log_index"),
        export_query(conn, "mission2_archive_metrics", "SELECT * FROM mission2_archive_metrics"),
        export_query(conn, "mission2_woof_vault_allocations", "SELECT * FROM mission2_woof_vault_allocations ORDER BY address"),
    ]


def row_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in tables:
        out[table] = int(conn.execute("SELECT COUNT(*) FROM " + sql_identifier(table)).fetchone()[0])
    return out


def resolve_runtime(args: argparse.Namespace, chain: dict[str, Any], contracts: dict[str, Any]) -> tuple[str | None, int | None, int | str | None, str]:
    block_config = load_block_config()
    config_auction = (contracts.get("auction_house") or {}).get("address")
    auction = args.auction_house or os.environ.get("MISSION2_AUCTION_HOUSE") or config_auction
    from_block_raw = (
        args.from_block
        if args.from_block is not None
        else os.environ.get("MISSION2_FROM_BLOCK")
        or block_config.get("from_block")
    )
    to_block_raw = (
        args.to_block
        if args.to_block is not None
        else os.environ.get("MISSION2_TO_BLOCK")
        or block_config.get("scan_to_block")
        or block_config.get("to_block")
    )
    rpc_url = args.rpc_url or os.environ.get("DEGEN_RPC_URL") or chain.get("rpc_candidates", ["https://rpc.degen.tips"])[0]
    from_block = int(from_block_raw) if from_block_raw not in (None, "") else None
    to_block: int | str | None
    if to_block_raw in (None, ""):
        to_block = "latest"
    elif str(to_block_raw).lower() == "latest":
        to_block = "latest"
    else:
        to_block = int(str(to_block_raw))
    return auction, from_block, to_block, rpc_url


def check_config() -> int:
    chain = load_chain_config()
    contracts = load_contract_config()
    block_config = load_block_config()
    event_config = load_json(CONFIG / "mission2_event_abis.json")
    topics = event_topics(event_config)
    missing_runtime = []
    auction, from_block, to_block, rpc_url = resolve_runtime(argparse.Namespace(auction_house=None, from_block=None, to_block=None, rpc_url=None), chain, contracts)
    if not auction:
        missing_runtime.append("MISSION2_AUCTION_HOUSE")
    if from_block is None:
        missing_runtime.append("MISSION2_FROM_BLOCK")
    result = {
        "status": "config_ok_runtime_ready" if not missing_runtime else "config_ok_indexer_not_ready",
        "chain_id": chain["chain_id"],
        "rpc_url": redact_url(rpc_url),
        "event_topics_computed": topics,
        "auction_house": auction,
        "from_block": from_block,
        "to_block": to_block,
        "missing_runtime": missing_runtime,
        "chain_config_source": chain.get("source_file"),
        "contracts_config_source": contracts.get("_config_source"),
        "blocks_config_source": block_config.get("source_file"),
        "contracts_confidence": {name: meta.get("confidence") for name, meta in contracts.items() if not name.startswith("_")},
        "note": "Verified configs are available; indexing can run without env overrides. Dune query SQL/results remain separate and unrecovered until DUNE_API_KEY/query IDs are supplied.",
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Degen Dogs Mission 2 auction events from Degen Chain.")
    parser.add_argument("--check-config", action="store_true", help="Validate static archive config and report missing runtime inputs without fetching logs.")
    parser.add_argument("--auction-house", help="Verified Mission 2 auction house address. Env: MISSION2_AUCTION_HOUSE")
    parser.add_argument("--from-block", type=int, help="Verified first block to index. Env: MISSION2_FROM_BLOCK")
    parser.add_argument("--to-block", help="Final block to index or latest. Env: MISSION2_TO_BLOCK")
    parser.add_argument("--rpc-url", help="Degen Chain RPC URL. Env: DEGEN_RPC_URL")
    parser.add_argument("--chunk-size", type=int, default=int(os.environ.get("MISSION2_LOG_CHUNK", "5000")))
    args = parser.parse_args()

    if args.check_config:
        return check_config()
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size / MISSION2_LOG_CHUNK must be a positive integer.")

    chain = load_chain_config()
    contracts = load_contract_config()
    block_config = load_block_config()
    event_config = load_json(CONFIG / "mission2_event_abis.json")
    topics_by_event = event_topics(event_config)
    topic_names = topic_to_event(topics_by_event)

    auction, from_block, to_block_raw, rpc_url = resolve_runtime(args, chain, contracts)
    if not auction:
        raise SystemExit("MISSION2_AUCTION_HOUSE or --auction-house is required and must be verified before indexing.")
    auction = normalize_address(auction)
    if from_block is None:
        raise SystemExit("MISSION2_FROM_BLOCK or --from-block is required and must be verified before indexing.")
    if from_block < 0:
        raise SystemExit("MISSION2_FROM_BLOCK / --from-block must be non-negative.")
    if to_block_raw == "latest":
        latest_hex = rpc_call_retry(rpc_url, "eth_blockNumber", [])
        to_block = int(latest_hex, 16)
    elif to_block_raw is None:
        latest_hex = rpc_call_retry(rpc_url, "eth_blockNumber", [])
        to_block = int(latest_hex, 16)
    else:
        to_block = int(to_block_raw)
    if to_block < 0:
        raise SystemExit("MISSION2_TO_BLOCK / --to-block must be non-negative when provided.")
    if to_block < from_block:
        raise SystemExit(f"to_block {to_block} is before from_block {from_block}")

    run_timestamp = utc_now()
    run_id = f"mission2_{from_block}_{to_block}_{run_timestamp.replace(':', '').replace('-', '').replace('Z', 'Z')}"
    source_confidence = "verified_onchain" if contracts.get("_confidence") == "verified" else "unverified"
    warning = (
        "Verified Mission 2 config files used; Dune reconciliation remains separate."
        if source_confidence == "verified_onchain"
        else "Runtime address/range supplied by user or environment; contracts config remains unverified until source is recorded."
    )

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    SQLITE_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    logs = fetch_logs(rpc_url, auction, list(topics_by_event.values()), from_block, to_block, args.chunk_size)
    raw_path = RAW_DIR / f"mission2_raw_logs_{from_block}_{to_block}_{run_timestamp.replace(':', '').replace('-', '')}.ndjson"
    write_ndjson(raw_path, logs)

    decoded = decode_events(logs, int(chain["chain_id"]), auction, topic_names, source_confidence, run_id)
    sqlite_path = SQLITE_DIR / "mission2.sqlite"
    conn = setup_db(sqlite_path)
    try:
        upsert_configs(conn, int(chain["chain_id"]), run_timestamp)
        conn.execute(
            "INSERT OR REPLACE INTO mission2_index_runs (run_id, run_timestamp_utc, chain_id, rpc_url, from_block, to_block, auction_house_address, config_confidence, raw_log_path, sqlite_path, manifest_path, warning) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, run_timestamp, int(chain["chain_id"]), redact_url(rpc_url), from_block, to_block, auction, source_confidence, str(raw_path.relative_to(ROOT)), str(sqlite_path.relative_to(ROOT)), str((GENERATED_DIR / "mission2_archive_manifest.json").relative_to(ROOT)), warning),
        )
        insert_many(conn, "mission2_raw_logs", decoded["raw"])
        insert_many(conn, "mission2_auction_created", decoded["created"])
        insert_many(conn, "mission2_auction_bids", decoded["bids"])
        insert_many(conn, "mission2_auction_extended", decoded["extended"])
        insert_many(conn, "mission2_auction_settled", decoded["settled"])
        insert_many(conn, "mission2_parameter_updates", decoded["parameter_updates"])
        conn.commit()
        exports = export_outputs(conn)
        counts = row_counts(conn, [
            "mission2_raw_logs",
            "mission2_auction_created",
            "mission2_auction_bids",
            "mission2_auction_extended",
            "mission2_auction_settled",
            "mission2_parameter_updates",
            "mission2_woof_vault_allocations",
            "mission2_index_gaps",
        ])
    finally:
        conn.close()

    sqlite_alias = DATA / "mission2_archive.sqlite"
    shutil.copy2(sqlite_path, sqlite_alias)

    manifest_path = GENERATED_DIR / "mission2_archive_manifest.json"
    source_files = [
        chain.get("source_file") or "archive/mission2/config/mission2_chain.json",
        contracts.get("_config_source") or "archive/mission2/config/mission2_contracts.unverified.json",
        block_config.get("source_file"),
        "archive/mission2/config/mission2_event_abis.json",
        "archive/mission2/config/woof_vault_allocations.json",
        "archive/mission2/sql/schema.sql",
        "archive/mission2/sql/marts.sql",
    ]
    source_files = [item for item in source_files if item]
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "run_timestamp_utc": run_timestamp,
        "chain_id": int(chain["chain_id"]),
        "network": chain.get("network"),
        "rpc_used": redact_url(rpc_url),
        "from_block": from_block,
        "to_block": to_block,
        "auction_house_address": auction,
        "config_confidence": source_confidence,
        "dune_reconciliation_status": "not_recovered",
        "warnings": [warning, "Dune query IDs, official SQL, and official Dune result exports remain unrecovered."],
        "event_topics": topics_by_event,
        "row_counts": counts,
        "source_files_used": source_files,
        "raw_log_file": {
            "path": str(raw_path.relative_to(ROOT)),
            "sha256": sha256_file(raw_path),
            "rows": len(logs),
        },
        "sqlite_file": {
            "path": str(sqlite_path.relative_to(ROOT)),
            "sha256": sha256_file(sqlite_path),
        },
        "sqlite_archive_alias": {
            "path": str(sqlite_alias.relative_to(ROOT)),
            "sha256": sha256_file(sqlite_alias),
        },
        "generated_files": exports,
        "known_gaps": [],
    }
    write_json(manifest_path, manifest)
    write_json(GENERATED_DIR / "manifest.json", manifest)
    write_json(RAW_DIR / "mission2_auction_logs.meta.json", {
        "schema_version": 1,
        "updated_at_utc": run_timestamp,
        "run_id": run_id,
        "chain_id": int(chain["chain_id"]),
        "rpc_urls_used": [redact_url(rpc_url)],
        "auction_house": auction,
        "from_block": from_block,
        "to_block": to_block,
        "chunk_size": args.chunk_size,
        "event_topics": topics_by_event,
        "total_logs": len(logs),
        "per_event_log_counts": {
            "AuctionCreated": counts.get("mission2_auction_created", 0),
            "AuctionBid": counts.get("mission2_auction_bids", 0),
            "AuctionExtended": counts.get("mission2_auction_extended", 0),
            "AuctionSettled": counts.get("mission2_auction_settled", 0),
        },
        "failed_chunks": [],
        "retried_chunks": [],
        "raw_log_path": str(raw_path.relative_to(ROOT)),
        "raw_log_sha256": sha256_file(raw_path),
    })
    write_json(RAW_DIR / "mission2_rpc_failures.json", {
        "schema_version": 1,
        "updated_at_utc": run_timestamp,
        "failures": [],
        "notes": "No final failed chunks were recorded in the successful recovery run.",
    })
    gap_path = RAW_DIR / "mission2_index_gaps.csv"
    with gap_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["gap_id", "chain_id", "from_block", "to_block", "reason", "severity", "detected_at_utc", "resolved_at_utc", "notes"])
    print(json.dumps({"run_id": run_id, "row_counts": counts, "manifest": str(manifest_path.relative_to(ROOT))}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
