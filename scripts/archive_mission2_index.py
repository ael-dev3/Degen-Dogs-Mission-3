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
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    return {
        "chain_id": chain_id,
        "contract_address": address,
        "block_number": hex_int(log.get("blockNumber")),
        "block_hash": log.get("blockHash"),
        "tx_hash": log.get("transactionHash"),
        "tx_index": hex_int(log.get("transactionIndex")),
        "log_index": hex_int(log.get("logIndex")),
        "block_time_utc": None,
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
                    "value_display_native": None,
                    "display_decimals_confidence": None,
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
                    "amount_display_native": None,
                    "display_decimals_confidence": None,
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
    contracts = load_json(CONFIG / "mission2_contracts.unverified.json")
    rows = []
    for name, meta in contracts.items():
        rows.append({
            "name": name,
            "chain_id": chain_id,
            "address": meta.get("address"),
            "confidence": meta.get("confidence", "unverified"),
            "source": "archive/mission2/config/mission2_contracts.unverified.json",
            "how_to_verify": meta.get("how_to_verify"),
            "notes": None,
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


def export_outputs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        export_query(conn, "mission2_auction_created", "SELECT * FROM mission2_auction_created ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_bids", "SELECT * FROM mission2_auction_bids ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_extended", "SELECT * FROM mission2_auction_extended ORDER BY block_number, log_index"),
        export_query(conn, "mission2_auction_settled", "SELECT * FROM mission2_auction_settled ORDER BY block_number, log_index"),
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
    config_auction = (contracts.get("auction_house") or {}).get("address")
    auction = args.auction_house or os.environ.get("MISSION2_AUCTION_HOUSE") or config_auction
    from_block_raw = args.from_block if args.from_block is not None else os.environ.get("MISSION2_FROM_BLOCK")
    to_block_raw = args.to_block if args.to_block is not None else os.environ.get("MISSION2_TO_BLOCK")
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
    chain = load_json(CONFIG / "mission2_chain.json")
    contracts = load_json(CONFIG / "mission2_contracts.unverified.json")
    event_config = load_json(CONFIG / "mission2_event_abis.json")
    topics = event_topics(event_config)
    missing_runtime = []
    auction, from_block, _to_block, rpc_url = resolve_runtime(argparse.Namespace(auction_house=None, from_block=None, to_block=None, rpc_url=None), chain, contracts)
    if not auction:
        missing_runtime.append("MISSION2_AUCTION_HOUSE")
    if from_block is None:
        missing_runtime.append("MISSION2_FROM_BLOCK")
    result = {
        "status": "config_ok_indexer_not_ready" if missing_runtime else "config_ok_runtime_ready",
        "chain_id": chain["chain_id"],
        "rpc_url": redact_url(rpc_url),
        "event_topics_computed": topics,
        "missing_runtime": missing_runtime,
        "contracts_confidence": {name: meta.get("confidence") for name, meta in contracts.items()},
        "note": "Normal indexing refuses to run until auction house and from block are supplied. Contract placeholders remain unverified until recovered.",
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

    chain = load_json(CONFIG / "mission2_chain.json")
    contracts = load_json(CONFIG / "mission2_contracts.unverified.json")
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
    source_confidence = "unverified"
    warning = "Runtime address/range supplied by user or environment; contracts config remains unverified until source is recorded."

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

    manifest_path = GENERATED_DIR / "mission2_archive_manifest.json"
    manifest = {
        "run_id": run_id,
        "run_timestamp_utc": run_timestamp,
        "chain_id": int(chain["chain_id"]),
        "network": chain.get("network"),
        "rpc_used": redact_url(rpc_url),
        "from_block": from_block,
        "to_block": to_block,
        "auction_house_address": auction,
        "config_confidence": source_confidence,
        "warnings": [warning, "Display/native amount conversion is intentionally omitted until decimals and currency path are verified."],
        "event_topics": topics_by_event,
        "row_counts": counts,
        "source_files_used": [
            "archive/mission2/config/mission2_chain.json",
            "archive/mission2/config/mission2_contracts.unverified.json",
            "archive/mission2/config/mission2_event_abis.json",
            "archive/mission2/config/woof_vault_allocations.json",
            "archive/mission2/sql/schema.sql",
            "archive/mission2/sql/marts.sql",
        ],
        "raw_log_file": {
            "path": str(raw_path.relative_to(ROOT)),
            "sha256": sha256_file(raw_path),
            "rows": len(logs),
        },
        "sqlite_file": {
            "path": str(sqlite_path.relative_to(ROOT)),
            "sha256": sha256_file(sqlite_path),
        },
        "generated_files": exports,
        "known_gaps": [],
    }
    write_json(manifest_path, manifest)
    print(json.dumps({"run_id": run_id, "row_counts": counts, "manifest": str(manifest_path.relative_to(ROOT))}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
