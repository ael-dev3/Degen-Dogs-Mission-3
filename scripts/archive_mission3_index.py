#!/usr/bin/env python3
"""Mission 3 Base archive indexer.

Fetches Degen Dogs Mission 3 auction-house logs from Base, stores raw logs and
decoded events in an append-only SQLite archive, then exports generated CSV/JSON
files for long-term preservation and future dashboard integration.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
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
ARCHIVE = ROOT / "archive" / "mission3"
CONFIG_DIR = ARCHIVE / "config"
SQL_DIR = ARCHIVE / "sql"
DATA_DIR = ARCHIVE / "data"
DEFAULT_DB = DATA_DIR / "mission3_archive.sqlite"
DEFAULT_OUTPUT_DIR = DATA_DIR / "generated"
DEFAULT_RAW_DIR = DATA_DIR / "raw"
PUBLIC_OUTPUT_DIR = ROOT / "public" / "generated" / "mission3"

DEFAULT_RPC_URLS = [
    "https://mainnet.base.org",
    "https://developer-access-mainnet.base.org",
    "https://base-rpc.publicnode.com",
]
DEFAULT_LOG_RPC_URLS = ["https://mainnet.base.org"]
STATE_ID = "mission3"
SELECTOR_AUCTION = "0x7d9f6db5"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
OPENSEA_ITEM_BASE = "https://opensea.io/item/base"

CSV_EXPORTS = [
    "mission3_auction_created",
    "mission3_auction_bids",
    "mission3_auction_extended",
    "mission3_auction_settled",
    "mission3_auction_winners",
    "mission3_recent_bids",
    "mission3_bidder_leaderboard",
    "mission3_auction_timeline",
    "mission3_daily_activity",
]
JSON_LIST_EXPORTS = [
    "mission3_dog_search_index",
]
JSON_OBJECT_EXPORTS = [
    "mission3_archive_metrics",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def utc_from_unix(value: int | str | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def parse_url_list(env_name: str, default_urls: list[str]) -> list[str]:
    if env_name == "BASE_RPC_URL" and os.environ.get("BASE_RPC_URL"):
        return [os.environ["BASE_RPC_URL"]]
    raw = os.environ.get(env_name)
    if not raw:
        return list(default_urls)
    urls = [item.strip() for item in raw.split(",") if item.strip()]
    return urls or list(default_urls)


def rpc_urls() -> list[str]:
    if os.environ.get("BASE_RPC_URL"):
        return [os.environ["BASE_RPC_URL"]]
    return parse_url_list("BASE_RPC_URLS", DEFAULT_RPC_URLS)


def log_rpc_urls() -> list[str]:
    if os.environ.get("BASE_RPC_URL"):
        return [os.environ["BASE_RPC_URL"]]
    return parse_url_list("BASE_LOG_RPC_URLS", DEFAULT_LOG_RPC_URLS)


def redact_url(value: str) -> str:
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


def post_json(url: str, payload: Any, *, timeout: int = 60) -> Any:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "degen-dogs-mission3-archive/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            text = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    return json.loads(text)


def rpc_call(method: str, params: list[Any], *, urls: list[str] | None = None, timeout: int = 60) -> tuple[Any, str]:
    active_urls = urls or rpc_urls()
    errors: list[str] = []
    for url in active_urls:
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
            data = post_json(url, payload, timeout=timeout)
            if "error" in data:
                raise RuntimeError(data["error"])
            return data.get("result"), url
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{redact_url(url)}: {exc}")
    raise RuntimeError(f"RPC {method} failed: {'; '.join(errors)}")


def rpc_batch(calls: list[tuple[str, list[Any]]], *, urls: list[str] | None = None, timeout: int = 120) -> list[Any]:
    if not calls:
        return []
    active_urls = urls or rpc_urls()
    payload = [
        {"jsonrpc": "2.0", "id": idx, "method": method, "params": params}
        for idx, (method, params) in enumerate(calls)
    ]
    errors: list[str] = []
    for url in active_urls:
        try:
            data = post_json(url, payload, timeout=timeout)
            if not isinstance(data, list):
                raise RuntimeError(f"batch returned non-list: {data!r}")
            by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
            results: list[Any] = []
            for idx, (method, params) in enumerate(calls):
                item = by_id.get(idx)
                if not item or "error" in item:
                    result, _ = rpc_call(method, params, urls=active_urls, timeout=timeout)
                    results.append(result)
                else:
                    results.append(item.get("result"))
            return results
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{redact_url(url)}: {exc}")
    raise RuntimeError(f"RPC batch failed: {'; '.join(errors)}")


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
        raise RuntimeError("Cannot compute Ethereum Keccak-256 topics. Install pycryptodome or eth-hash.") from exc


def word(data_hex: str, index: int) -> int:
    data = data_hex[2:] if data_hex.startswith("0x") else data_hex
    start = index * 64
    chunk = data[start : start + 64]
    if len(chunk) != 64:
        raise ValueError(f"missing ABI word {index} in data {data_hex}")
    return int(chunk, 16)


def word_address(data_hex: str, index: int) -> str:
    return "0x" + f"{word(data_hex, index):064x}"[-40:]


def topic_uint(topic: str | None) -> int:
    if not topic:
        raise ValueError("missing indexed uint topic")
    return int(topic, 16)


def hex_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    return int(value, 16)


def wei_to_eth_string(amount_raw: int | str) -> str:
    amount = Decimal(str(amount_raw)) / Decimal(10**18)
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def load_configs() -> dict[str, Any]:
    return {
        "chain": load_json(CONFIG_DIR / "mission3_chain.verified.json"),
        "contracts": load_json(CONFIG_DIR / "mission3_contracts.verified.json"),
        "blocks": load_json(CONFIG_DIR / "mission3_blocks.verified.json"),
        "events": load_json(CONFIG_DIR / "mission3_events.verified.json"),
    }


def event_topics(events_config: dict[str, Any]) -> dict[str, str]:
    topics: dict[str, str] = {}
    for event in events_config["events"]:
        computed = keccak256_text(event["signature"])
        expected = str(event["topic0"]).lower()
        if computed.lower() != expected:
            raise RuntimeError(f"topic mismatch for {event['name']}: computed {computed}, config {expected}")
        topics[event["name"]] = expected
    return topics


def verify_config(*, check_rpc: bool = True) -> dict[str, Any]:
    configs = load_configs()
    chain_id = int(configs["chain"]["chain"]["chain_id"])
    if chain_id != 8453:
        raise RuntimeError(f"unexpected chain id in config: {chain_id}")
    topics = event_topics(configs["events"])

    db = sqlite3.connect(":memory:")
    db.executescript((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    db.executescript((SQL_DIR / "marts.sql").read_text(encoding="utf-8"))
    db.close()

    rpc_report: dict[str, Any] = {"checked": False}
    if check_rpc:
        chain_hex, used_url = rpc_call("eth_chainId", [], urls=rpc_urls())
        live_chain_id = int(chain_hex, 16)
        if live_chain_id != chain_id:
            raise RuntimeError(f"RPC chain mismatch: config={chain_id} rpc={live_chain_id}")
        contract_report: dict[str, int] = {}
        for name, item in configs["contracts"]["contracts"].items():
            code, _ = rpc_call("eth_getCode", [item["address"], "latest"], urls=rpc_urls())
            code_bytes = max((len(code or "0x") - 2) // 2, 0)
            if code_bytes <= 0:
                raise RuntimeError(f"contract has no code: {name} {item['address']}")
            contract_report[name] = code_bytes
        rpc_report = {
            "checked": True,
            "chain_id": live_chain_id,
            "rpc": redact_url(used_url),
            "contract_code_bytes": contract_report,
        }

    return {"status": "ok", "topics": topics, "rpc": rpc_report}


def block_ranges(start: int, end: int, size: int) -> Iterable[tuple[int, int]]:
    cursor = start
    while cursor <= end:
        hi = min(cursor + size - 1, end)
        yield cursor, hi
        cursor = hi + 1


def log_filter(address: str, topics0: list[str], start: int, end: int) -> dict[str, Any]:
    return {
        "address": address,
        "fromBlock": hex(start),
        "toBlock": hex(end),
        "topics": [topics0],
    }


def fetch_log_range(address: str, topics0: list[str], start: int, end: int, urls: list[str]) -> tuple[tuple[int, int], list[dict[str, Any]], str]:
    last: Exception | None = None
    for attempt in range(5):
        try:
            logs, used_url = rpc_call("eth_getLogs", [log_filter(address, topics0, start, end)], urls=urls, timeout=120)
            if not isinstance(logs, list):
                raise RuntimeError(f"eth_getLogs returned non-list: {logs!r}")
            redacted = redact_url(used_url)
            for log in logs:
                log["__source_rpc"] = redacted
            return (start, end), logs, redacted
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(2**attempt, 10))
    raise RuntimeError(f"log range {start}-{end} failed after retries: {last}")


def fetch_logs(address: str, topics0: list[str], from_block: int, to_block: int, *, chunk_size: int, workers: int) -> list[dict[str, Any]]:
    if from_block > to_block:
        return []
    ranges = list(block_ranges(from_block, to_block, chunk_size))
    urls = log_rpc_urls()
    logs: list[dict[str, Any]] = []
    completed = 0
    print(f"fetching {len(ranges)} log chunks from {from_block} to {to_block} (chunk={chunk_size}, workers={workers})", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(fetch_log_range, address, topics0, lo, hi, urls): (lo, hi)
            for lo, hi in ranges
        }
        for future in concurrent.futures.as_completed(future_map):
            lo, hi = future_map[future]
            bounds, rows, source = future.result()
            completed += 1
            logs.extend(rows)
            if completed == 1 or completed == len(ranges) or completed % 25 == 0:
                print(f"  log chunks {completed}/{len(ranges)} latest={bounds[0]}-{bounds[1]} rows={len(rows)} rpc={source}", file=sys.stderr)
    logs.sort(key=lambda item: (hex_int(item.get("blockNumber")), hex_int(item.get("logIndex"))))
    return logs


def fetch_block_times(blocks: Iterable[int]) -> dict[int, str]:
    ordered = sorted(set(int(block) for block in blocks))
    out: dict[int, str] = {}
    batch_size = 10
    for idx in range(0, len(ordered), batch_size):
        batch = ordered[idx : idx + batch_size]
        calls = [("eth_getBlockByNumber", [hex(block), False]) for block in batch]
        results = rpc_batch(calls, timeout=120)
        for block, result in zip(batch, results):
            if result and result.get("timestamp"):
                out[block] = utc_from_unix(int(result["timestamp"], 16)) or ""
    return out


def init_db(path: Path, *, full_refresh: bool) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if full_refresh and path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


def record_state(
    conn: sqlite3.Connection,
    *,
    chain_id: int,
    auction_house: str,
    from_block: int,
    latest_indexed_block: int | None,
    latest_indexed_block_time_utc: str | None,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mission3_index_state (
          id, chain_id, auction_house, from_block, latest_indexed_block,
          latest_indexed_block_time_utc, latest_run_at_utc, status, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            STATE_ID,
            chain_id,
            auction_house.lower(),
            from_block,
            latest_indexed_block,
            latest_indexed_block_time_utc,
            utc_now(),
            status,
            error,
        ),
    )
    conn.commit()


def get_latest_indexed_block(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT latest_indexed_block FROM mission3_index_state WHERE id = ?",
        (STATE_ID,),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return int(row[0])


def record_gap(conn: sqlite3.Connection, start: int, end: int, reason: str, status: str = "open") -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO mission3_index_gaps
        (from_block, to_block, reason, status, created_at_utc, resolved_at_utc)
        VALUES (?, ?, ?, ?, COALESCE((SELECT created_at_utc FROM mission3_index_gaps WHERE from_block=? AND to_block=? AND reason=?), ?), NULL)
        """,
        (start, end, reason[:500], status, start, end, reason[:500], utc_now()),
    )
    conn.commit()


def resolve_covered_gaps(conn: sqlite3.Connection, start: int, end: int) -> None:
    if start > end:
        return
    conn.execute(
        """
        UPDATE mission3_index_gaps
        SET status = 'resolved', resolved_at_utc = ?
        WHERE status = 'open' AND from_block >= ? AND to_block <= ?
        """,
        (utc_now(), start, end),
    )
    conn.commit()


def insert_raw_logs(conn: sqlite3.Connection, logs: list[dict[str, Any]], chain_id: int, fetched_at: str) -> None:
    rows: list[tuple[Any, ...]] = []
    for log in logs:
        topics = [str(topic).lower() for topic in log.get("topics", [])]
        padded = topics + [None] * (4 - len(topics))
        rows.append(
            (
                chain_id,
                str(log.get("address") or "").lower(),
                hex_int(log.get("blockNumber")),
                str(log.get("blockHash") or "").lower(),
                str(log.get("transactionHash") or "").lower(),
                hex_int(log.get("transactionIndex")),
                hex_int(log.get("logIndex")),
                int(bool(log.get("removed", False))),
                padded[0],
                padded[1],
                padded[2],
                padded[3],
                str(log.get("data") or "0x").lower(),
                fetched_at,
                str(log.get("__source_rpc") or "<unknown>"),
            )
        )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_raw_logs (
          chain_id, address, block_number, block_hash, transaction_hash,
          transaction_index, log_index, removed, topic0, topic1, topic2, topic3,
          data, fetched_at_utc, source_rpc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()


def decode_and_insert(conn: sqlite3.Connection, logs: list[dict[str, Any]], topics_by_name: dict[str, str]) -> dict[str, int]:
    block_times = fetch_block_times(hex_int(log.get("blockNumber")) for log in logs)
    created: list[tuple[Any, ...]] = []
    bids: list[tuple[Any, ...]] = []
    extended: list[tuple[Any, ...]] = []
    settled: list[tuple[Any, ...]] = []
    topic_to_name = {topic.lower(): name for name, topic in topics_by_name.items()}

    for log in logs:
        topics = [str(topic).lower() for topic in log.get("topics", [])]
        if not topics:
            continue
        name = topic_to_name.get(topics[0])
        if not name:
            continue
        block_number = hex_int(log.get("blockNumber"))
        tx_hash = str(log.get("transactionHash") or "").lower()
        log_index = hex_int(log.get("logIndex"))
        block_time = block_times.get(block_number)
        data = str(log.get("data") or "0x")

        if name == "AuctionCreated":
            created.append((topic_uint(topics[1] if len(topics) > 1 else None), word(data, 0), word(data, 1), block_number, tx_hash, log_index, block_time))
        elif name == "AuctionBid":
            amount = word(data, 1)
            bids.append((topic_uint(topics[1] if len(topics) > 1 else None), word_address(data, 0), str(amount), wei_to_eth_string(amount), int(bool(word(data, 2))), block_number, tx_hash, log_index, block_time))
        elif name == "AuctionExtended":
            extended.append((topic_uint(topics[1] if len(topics) > 1 else None), word(data, 0), block_number, tx_hash, log_index, block_time))
        elif name == "AuctionSettled":
            amount = word(data, 1)
            settled.append((topic_uint(topics[1] if len(topics) > 1 else None), word_address(data, 0), str(amount), wei_to_eth_string(amount), block_number, tx_hash, log_index, block_time))

    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_created
        (token_id, start_time, end_time, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        created,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_bids
        (token_id, bidder, amount_raw, amount_eth, extended, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        bids,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_extended
        (token_id, end_time, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        extended,
    )
    conn.executemany(
        """
        INSERT OR REPLACE INTO mission3_auction_settled
        (token_id, winner, amount_raw, amount_eth, block_number, transaction_hash, log_index, block_time_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        settled,
    )
    conn.commit()
    return {
        "created": len(created),
        "bids": len(bids),
        "extended": len(extended),
        "settled": len(settled),
    }


def fetch_current_auction(conn: sqlite3.Connection, auction_house: str, latest_block: int) -> None:
    try:
        raw, used_url = rpc_call("eth_call", [{"to": auction_house, "data": SELECTOR_AUCTION}, hex(latest_block)], urls=rpc_urls())
        block_time = fetch_block_times([latest_block]).get(latest_block)
        token_id = word(raw, 0)
        amount_raw = word(raw, 1)
        start_time = word(raw, 2)
        end_time = word(raw, 3)
        highest_bidder = word_address(raw, 4)
        settled = int(word(raw, 5))
        conn.execute(
            """
            INSERT OR REPLACE INTO mission3_current_auction_snapshots
            (snapshot_at_utc, latest_block, token_id, start_time, end_time, highest_bidder, amount_raw, amount_eth, settled, source, confidence)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                latest_block,
                token_id,
                start_time,
                end_time,
                highest_bidder.lower(),
                str(amount_raw),
                wei_to_eth_string(amount_raw),
                settled,
                redact_url(used_url),
                "verified_contract_call",
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"warning: current auction snapshot failed: {exc}", file=sys.stderr)


def apply_marts(conn: sqlite3.Connection) -> None:
    conn.executescript((SQL_DIR / "marts.sql").read_text(encoding="utf-8"))
    conn.commit()


def table_rows(conn: sqlite3.Connection, table: str, *, limit: int | None = None) -> tuple[list[str], list[tuple[Any, ...]]]:
    sql = f'SELECT * FROM "{table}"'
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cur = conn.execute(sql)
    cols = [item[0] for item in cur.description]
    rows = [tuple(row) for row in cur.fetchall()]
    return cols, rows


def rows_to_dicts(cols: list[str], rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [dict(zip(cols, row)) for row in rows]


def write_csv_file(path: Path, cols: list[str], rows: list[tuple[Any, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_raw_ndjson(conn: sqlite3.Connection, raw_dir: Path) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / "mission3_raw_logs.ndjson"
    cur = conn.execute("SELECT * FROM mission3_raw_logs ORDER BY block_number, log_index")
    cols = [item[0] for item in cur.description]
    with path.open("w", encoding="utf-8") as handle:
        for row in cur.fetchall():
            handle.write(json.dumps(dict(zip(cols, tuple(row))), sort_keys=True) + "\n")
    return path


def export_outputs(conn: sqlite3.Connection, output_dir: Path, *, db_path: Path, write_public: bool) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = write_raw_ndjson(conn, DEFAULT_RAW_DIR)
    files: list[dict[str, Any]] = []

    for table in CSV_EXPORTS:
        cols, rows = table_rows(conn, table)
        csv_path = output_dir / f"{table}.csv"
        json_path = output_dir / f"{table}.json"
        write_csv_file(csv_path, cols, rows)
        write_json(json_path, rows_to_dicts(cols, rows))
        files.append({"name": table, "type": "csv", "path": str(csv_path.relative_to(ROOT)), "rows": len(rows), "sha256": sha256_file(csv_path)})
        files.append({"name": table, "type": "json", "path": str(json_path.relative_to(ROOT)), "rows": len(rows), "sha256": sha256_file(json_path)})

    for table in JSON_LIST_EXPORTS:
        cols, rows = table_rows(conn, table)
        records = rows_to_dicts(cols, rows)
        for record in records:
            if isinstance(record.get("sources"), str):
                record["sources"] = [item for item in str(record["sources"]).split(",") if item]
            if record.get("settled") in (0, 1):
                record["settled"] = bool(record["settled"])
        json_path = output_dir / f"{table}.json"
        write_json(json_path, records)
        files.append({"name": table, "type": "json", "path": str(json_path.relative_to(ROOT)), "rows": len(records), "sha256": sha256_file(json_path)})

    for table in JSON_OBJECT_EXPORTS:
        cols, rows = table_rows(conn, table)
        metrics = {str(row[0]): row[1] for row in rows}
        metrics["generated_at_utc"] = utc_now()
        json_path = output_dir / f"{table}.json"
        write_json(json_path, metrics)
        files.append({"name": table, "type": "json", "path": str(json_path.relative_to(ROOT)), "rows": len(metrics), "sha256": sha256_file(json_path)})

    state = conn.execute("SELECT * FROM mission3_index_state WHERE id = ?", (STATE_ID,)).fetchone()
    counts = {row["metric"]: row["value"] for row in conn.execute("SELECT metric, value FROM mission3_archive_metrics")}
    manifest = {
        "schema_version": 1,
        "mission": 3,
        "generated_at_utc": utc_now(),
        "database": str(db_path.relative_to(ROOT)) if db_path.is_relative_to(ROOT) else db_path.name,
        "raw_logs_ndjson": str(raw_path.relative_to(ROOT)),
        "raw_logs_sha256": sha256_file(raw_path),
        "index_state": dict(state) if state else None,
        "counts": counts,
        "files": files,
    }
    manifest_path = output_dir / "manifest.json"
    write_json(manifest_path, manifest)

    if write_public:
        PUBLIC_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        public_files = {
            "mission3_dog_search_index.json": output_dir / "mission3_dog_search_index.json",
            "mission3_archive_metrics.json": output_dir / "mission3_archive_metrics.json",
        }
        public_manifest_files: list[dict[str, Any]] = []
        file_meta_by_path = {item["path"]: item for item in files}
        for target_name, source_path in public_files.items():
            target_path = PUBLIC_OUTPUT_DIR / target_name
            shutil.copyfile(source_path, target_path)
            source_rel = str(source_path.relative_to(ROOT))
            source_meta = file_meta_by_path.get(source_rel, {})
            public_manifest_files.append({
                "name": source_meta.get("name", target_path.stem),
                "type": "json",
                "path": f"generated/mission3/{target_name}",
                "rows": source_meta.get("rows"),
                "sha256": sha256_file(target_path),
            })
        public_manifest = {
            "schema_version": 1,
            "mission": 3,
            "public": True,
            "generated_at_utc": manifest["generated_at_utc"],
            "index_state": manifest["index_state"],
            "counts": counts,
            "files": public_manifest_files,
        }
        write_json(PUBLIC_OUTPUT_DIR / "archive_manifest.json", public_manifest)

    return manifest


def resolve_to_block(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    if value.lower() == "latest":
        latest, _ = rpc_call("eth_blockNumber", [], urls=rpc_urls())
        return int(latest, 16)
    return int(value)


def latest_block_time(block_number: int) -> str | None:
    return fetch_block_times([block_number]).get(block_number)


def run_index(args: argparse.Namespace) -> dict[str, Any]:
    configs = load_configs()
    topics_by_name = event_topics(configs["events"])
    topics0 = list(topics_by_name.values())
    chain_id = int(configs["chain"]["chain"]["chain_id"])
    auction_house = str(configs["contracts"]["contracts"]["auction_house"]["address"])
    configured_from_block = int(configs["blocks"]["indexing"]["verified_from_block"])
    from_block_base = int(args.from_block or os.environ.get("MISSION3_FROM_BLOCK") or configured_from_block)
    to_block = resolve_to_block(args.to_block or os.environ.get("MISSION3_TO_BLOCK"))
    if to_block is None:
        latest, _ = rpc_call("eth_blockNumber", [], urls=rpc_urls())
        to_block = int(latest, 16)

    db_path = Path(args.db_path or os.environ.get("MISSION3_ARCHIVE_DB") or DEFAULT_DB).expanduser()
    output_dir = Path(args.output_dir or os.environ.get("MISSION3_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR).expanduser()
    full_refresh = bool(args.full_refresh)
    conn = init_db(db_path, full_refresh=full_refresh)
    previous_latest_indexed = get_latest_indexed_block(conn)

    if args.incremental and not full_refresh and not args.from_block and not os.environ.get("MISSION3_FROM_BLOCK"):
        from_block = (previous_latest_indexed + 1) if previous_latest_indexed is not None else from_block_base
    else:
        from_block = from_block_base

    chunk_size = max(1, min(int(os.environ.get("MISSION3_LOG_CHUNK", "10000")), 50000))
    workers = max(1, min(int(os.environ.get("MISSION3_LOG_WORKERS", "4")), 16))
    record_state(
        conn,
        chain_id=chain_id,
        auction_house=auction_house,
        from_block=from_block_base,
        latest_indexed_block=previous_latest_indexed,
        latest_indexed_block_time_utc=None,
        status="running",
    )

    try:
        fetched_at = utc_now()
        if from_block <= to_block:
            logs = fetch_logs(auction_house, topics0, from_block, to_block, chunk_size=chunk_size, workers=workers)
            insert_raw_logs(conn, logs, chain_id, fetched_at)
            decoded_counts = decode_and_insert(conn, logs, topics_by_name)
            print(f"decoded current run: {decoded_counts}", file=sys.stderr)
        else:
            logs = []
            print(f"nothing to index: from_block {from_block} > to_block {to_block}", file=sys.stderr)

        fetch_current_auction(conn, auction_house, to_block)
        latest_time = latest_block_time(to_block)
        record_state(
            conn,
            chain_id=chain_id,
            auction_house=auction_house,
            from_block=from_block_base,
            latest_indexed_block=to_block,
            latest_indexed_block_time_utc=latest_time,
            status="success",
        )
        resolve_covered_gaps(conn, from_block, to_block)
        apply_marts(conn)
        manifest = export_outputs(conn, output_dir, db_path=db_path, write_public=bool(args.write_public))
        print(json.dumps({"status": "success", "from_block": from_block, "to_block": to_block, "run_logs": len(logs), "counts": manifest["counts"]}, indent=2, sort_keys=True))
        return manifest
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
        if from_block <= to_block:
            record_gap(conn, from_block, to_block, reason, status="open")
        record_state(
            conn,
            chain_id=chain_id,
            auction_house=auction_house,
            from_block=from_block_base,
            latest_indexed_block=previous_latest_indexed,
            latest_indexed_block_time_utc=None,
            status="error",
            error=reason[:1000],
        )
        raise
    finally:
        conn.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Index Degen Dogs Mission 3 Base auction logs into a local archive.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="Validate configs, schema, event topics, chain id, and contract code, then exit.")
    mode.add_argument("--full-refresh", action="store_true", help="Rebuild the archive DB from the verified start block or supplied --from-block.")
    mode.add_argument("--incremental", action="store_true", help="Index from latest_indexed_block + 1 when possible. This is the default mode.")
    parser.add_argument("--from-block", type=int, help="Override the Mission 3 archive start block.")
    parser.add_argument("--to-block", help="Override the ending block; integer or 'latest'.")
    parser.add_argument("--db-path", help="Archive SQLite path. Defaults to archive/mission3/data/mission3_archive.sqlite.")
    parser.add_argument("--output-dir", help="Generated output directory. Defaults to archive/mission3/data/generated.")
    parser.add_argument("--write-public", action="store_true", help="Copy small future-ready JSON files to public/generated/mission3/.")
    parser.add_argument("--skip-rpc-check", action="store_true", help="For --verify-only, skip live RPC checks and validate local files only.")
    args = parser.parse_args(argv)
    if not args.verify_only and not args.full_refresh and not args.incremental:
        args.incremental = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.verify_only:
        report = verify_config(check_rpc=not args.skip_rpc_check)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    run_index(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
