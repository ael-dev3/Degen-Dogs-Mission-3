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
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

getcontext().prec = 80

ROOT = Path(__file__).resolve().parents[1]
SQL_PATH = ROOT / "sql" / "mission3_dashboard.sql"
GENERATED = ROOT / "generated"
PUBLIC_GENERATED = ROOT / "public" / "generated"

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
PRIMARY_TABLES = ["current_latest_bid", "recent_auction_winners"]


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


def fetch_token_stats(block_tag: str) -> dict[str, str]:
    name = decode_abi_string(eth_call(WOOF, SELECTOR_NAME, block_tag))
    symbol = decode_abi_string(eth_call(WOOF, SELECTOR_SYMBOL, block_tag))
    decimals = decode_uint_call(eth_call(WOOF, SELECTOR_DECIMALS, block_tag))
    supply_raw = decode_uint_call(eth_call(WOOF, SELECTOR_TOTAL_SUPPLY, block_tag))
    return {
        "auction_house": AUCTION_HOUSE,
        "dog_nft": DEGEN_DOGS,
        "woof_token": WOOF,
        "woof_name": name,
        "woof_symbol": symbol,
        "woof_decimals": str(decimals),
        "woof_total_supply": decimal_str(supply_raw, decimals, 6),
        "woof_total_supply_raw": str(supply_raw),
    }


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
                    "username": str(user.get("username") or ""),
                    "display_name": str(user.get("display_name") or ""),
                    "pfp_url": str(user.get("pfp_url") or ""),
                }
            )
    rows.sort(key=lambda row: row["address"])
    return rows



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


def css_class_for_col(col: str) -> str:
    lowered = col.lower()
    if "winner" in lowered or "bidder" in lowered or "farcaster" in lowered or "wallet" in lowered or "holder" in lowered:
        return "identity"
    if "time" in lowered or lowered.endswith("utc") or "date" in lowered:
        return "time"
    if "state" in lowered:
        return "state"
    numeric_markers = ("_eth", "_wei", "_pct", "_reward", "_balance", "count", "bids", "rank", "remaining")
    if any(marker in lowered for marker in numeric_markers) or lowered in {"eth", "bid", "reward", "balance", "supply_pct"}:
        return "num"
    return ""


def table_html(name: str, cols: list[str], rows: list[tuple[Any, ...]], *, featured: bool = False) -> str:
    head = "".join(
        f'<th scope="col" aria-sort="none" class="{css_class_for_col(col)}"><button type="button" data-col="{i}">{html.escape(col.replace("_", " "))}</button></th>'
        for i, col in enumerate(cols)
    )
    body = []
    for row in rows:
        cells = []
        for col, value in zip(cols, row):
            text = "" if value is None else str(value)
            cells.append(f'<td class="{css_class_for_col(col)}">{html.escape(text)}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    row_count = len(rows)
    caption = (
        f"<caption><span>{html.escape(name.replace('_', ' '))}</span>"
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
    cols, rows = tables.get("current_latest_bid", ([], []))
    if not rows:
        return {}
    row = rows[0]
    return {col: "" if row[i] is None else str(row[i]) for i, col in enumerate(cols)}


def export_links(name: str) -> str:
    safe = html.escape(name)
    return f'<a href="generated/{safe}.csv" download>CSV</a><a href="generated/{safe}.json" download>JSON</a>'


def render_metric_card(label: str, value: str, tone: str = "") -> str:
    return f'<article class="metric {tone}"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></article>'


def markdown_table(cols: list[str], rows: list[tuple[Any, ...]], limit: int | None = None) -> str:
    selected = rows if limit is None else rows[:limit]
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in selected:
        out.append("| " + " | ".join(str("" if v is None else v).replace("|", "\\|") for v in row) + " |")
    return "\n".join(out) + "\n"


def write_html(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
    metrics = metric_lookup(tables)
    current = current_lookup(tables)
    primary_parts = [table_html(name, *tables[name], featured=True) for name in PRIMARY_TABLES if name in tables]
    raw_parts = [
        table_html(name, cols, rows)
        for name, (cols, rows) in tables.items()
        if name not in PRIMARY_TABLES and name != "mission3_metrics"
    ]
    export_rows = []
    for name, (cols, rows) in tables.items():
        export_rows.append(
            f'<tr><td>{html.escape(name.replace("_", " "))}</td><td>{len(rows)}</td><td>{export_links(name)}</td></tr>'
        )

    cards = "".join(
        [
            render_metric_card("Current auction", current.get("dog", f"Dog #{metrics.get('current_auction_token_id', '')}"), "hot"),
            render_metric_card("Latest bid", f"{current.get('latest_bid_eth', metrics.get('current_bid_eth', '0'))} ETH", "money"),
            render_metric_card("Bidder", current.get("bidder", metrics.get("current_bidder", "")), "identity-card"),
            render_metric_card("Recent winners shown", "10", "cool"),
            render_metric_card("Settled auctions", metrics.get("settled_auctions", ""), ""),
            render_metric_card("Unique bidders", metrics.get("unique_bidders", ""), ""),
        ]
    )
    subtitle = html.escape(
        f"Cached at block {metrics.get('latest_block', '')} · {metrics.get('latest_block_time_utc', '')} UTC · generated by the private Mac mini runner"
    )
    detail_items = [
        ("Status", current.get("auction_state", "")),
        ("Ends", f"{current.get('auction_end_utc', '')} UTC"),
        ("Last bid", f"{current.get('bid_time_utc', '')} UTC"),
        ("Remaining", current.get("time_remaining", "")),
    ]
    current_detail = "".join(
        f'<span><b>{html.escape(label)}</b>{html.escape(value)}</span>'
        for label, value in detail_items
        if value and value != " UTC"
    )
    css = """
:root{color-scheme:dark;--bg:#050712;--bg2:#080d1b;--panel:#0d1424;--panel2:#101a2d;--panel3:#14213a;--line:#243455;--line2:#38527d;--text:#f3f7ff;--muted:#9fb0cf;--cyan:#58f5ff;--lime:#b6ff5d;--pink:#ff5de4;--orange:#ffb85d;--purple:#a78bfa;--red:#ff6b8a;--shadow:0 28px 90px rgba(0,0,0,.48);font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}html{background:var(--bg)}body{margin:0;min-width:320px;background:radial-gradient(circle at 12% -10%,rgba(88,245,255,.24),transparent 30rem),radial-gradient(circle at 82% 2%,rgba(255,93,228,.16),transparent 28rem),linear-gradient(180deg,var(--bg2),var(--bg));color:var(--text)}a{color:var(--cyan);text-decoration:none}a:hover{text-decoration:underline}.shell{width:min(1480px,calc(100% - 32px));margin:0 auto;padding:28px 0 40px}.hero{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(340px,.9fr);gap:18px;align-items:stretch;margin-bottom:18px}.headline,.panel,.table-card,.exports,details{border:1px solid rgba(88,245,255,.18);background:linear-gradient(180deg,rgba(16,26,45,.86),rgba(8,13,27,.94));box-shadow:var(--shadow);border-radius:24px}.headline{padding:24px;position:relative;overflow:hidden}.headline:before{content:"";position:absolute;inset:auto -8rem -10rem auto;width:23rem;height:23rem;border-radius:50%;background:radial-gradient(circle,rgba(182,255,93,.22),transparent 70%)}.eyebrow{display:inline-flex;gap:8px;align-items:center;color:var(--lime);font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.dot{width:8px;height:8px;border-radius:50%;background:var(--lime);box-shadow:0 0 18px var(--lime)}h1{margin:12px 0 10px;font-size:clamp(32px,5.2vw,64px);line-height:.94;letter-spacing:-.065em}.subtitle{margin:0;color:var(--muted);font-size:15px;line-height:1.6;max-width:760px}.current-detail{margin:18px 0 0;display:flex;flex-wrap:wrap;gap:8px;color:#dbe8ff;font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:12px}.current-detail span{display:inline-flex;gap:7px;align-items:center;border:1px solid rgba(88,245,255,.18);border-radius:999px;background:rgba(88,245,255,.07);padding:6px 9px}.current-detail b{color:var(--muted);font-weight:800;text-transform:uppercase;letter-spacing:.08em}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.metric{min-height:104px;padding:17px;border-radius:20px;border:1px solid rgba(255,255,255,.09);background:linear-gradient(180deg,rgba(20,33,58,.82),rgba(10,16,31,.92));display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}.metric span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.12em;font-weight:750}.metric strong{font-size:clamp(20px,3.2vw,34px);letter-spacing:-.04em;overflow-wrap:anywhere}.metric.hot strong{color:var(--orange)}.metric.money strong{color:var(--lime)}.metric.identity-card strong{color:var(--cyan)}.metric.cool strong{color:var(--pink)}.toolbar{position:sticky;top:0;z-index:20;margin:0 0 14px;padding:10px;border:1px solid rgba(88,245,255,.16);border-radius:18px;background:rgba(5,7,18,.82);backdrop-filter:blur(18px)}#filter{width:100%;height:42px;border:1px solid var(--line2);border-radius:12px;background:#071022;color:var(--text);font:600 14px ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;padding:0 14px;outline:none}#filter:focus{border-color:var(--cyan);box-shadow:0 0 0 3px rgba(88,245,255,.13)}.primary-grid{display:grid;gap:16px}.table-card{overflow:hidden}.table-card.featured-table{border-color:rgba(182,255,93,.22)}.table-scroll{overflow:auto;max-height:min(72vh,760px)}table{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;line-height:1.45}caption{caption-side:top;text-align:left;padding:16px 18px;border-bottom:1px solid var(--line);color:var(--text);font-weight:850;display:flex;justify-content:space-between;gap:16px;background:rgba(20,33,58,.92);position:relative;z-index:1;text-transform:capitalize}caption span:last-child{color:var(--muted);font-weight:700;text-transform:none}th,td{border-bottom:1px solid rgba(36,52,85,.72);padding:12px 14px;text-align:left;white-space:nowrap;font-variant-numeric:tabular-nums}thead th{position:sticky;top:0;z-index:5;background:#111d33;color:var(--muted);box-shadow:inset 0 -1px 0 var(--line2);font-size:11px;text-transform:uppercase;letter-spacing:.1em}tbody tr:hover td{background:rgba(88,245,255,.045)}td.num{color:var(--lime);font-weight:850}td.identity{color:var(--cyan);font-weight:850}td.state{color:var(--orange);font-weight:800}td.time{color:#c8d5ee}th button{all:unset;display:block;width:100%;cursor:pointer}th button[data-dir="asc"]::after{content:" ↑";color:var(--cyan)}th button[data-dir="desc"]::after{content:" ↓";color:var(--cyan)}.exports{margin-top:18px;padding:18px}.exports h2,details summary{margin:0 0 12px;font-size:16px;letter-spacing:-.02em}.exports table{font-size:13px}.exports td:last-child{display:flex;gap:10px}.exports a{display:inline-flex;border:1px solid rgba(88,245,255,.24);border-radius:999px;padding:5px 10px;background:rgba(88,245,255,.07);font-weight:800}details{margin-top:18px;padding:16px}details summary{cursor:pointer;color:var(--cyan);font-weight:850}.raw-grid{display:grid;gap:12px;margin-top:12px}.raw-grid .table-scroll{max-height:420px}@media (max-width:980px){.shell{width:min(100% - 18px,1480px);padding-top:14px}.hero{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:44px}th,td{white-space:normal;overflow-wrap:anywhere}}@media (max-width:620px){.metrics{grid-template-columns:1fr}.headline{padding:20px}.metric{min-height:92px}table{font-size:12px}th,td{padding:10px}}
""".strip()
    script = """
const filter=document.getElementById('filter');
const key=v=>{const s=v.trim().replaceAll(',','');const n=Number(s);return s!==''&&Number.isFinite(n)?n:v.trim().toLowerCase();};
const updateCounts=()=>{document.querySelectorAll('table').forEach(table=>{const rows=[...table.tBodies[0].rows];const visible=rows.filter(row=>!row.hidden).length;const total=table.caption?.querySelector('[data-total]');if(total){const suffix=visible===Number(total.dataset.total)?' rows':` / ${total.dataset.total} rows`;total.textContent=`${visible}${suffix}`;}});};
filter.addEventListener('input',()=>{const q=filter.value.trim().toLowerCase();document.querySelectorAll('tbody tr').forEach(tr=>{const table=tr.closest('table');const searchable=table?.closest('.primary-grid,details');tr.hidden=q!==''&&searchable&&!tr.textContent.toLowerCase().includes(q);});updateCounts();});
document.querySelectorAll('th button').forEach(button=>{button.addEventListener('click',()=>{const table=button.closest('table');const tbody=table.tBodies[0];const col=Number(button.dataset.col);const next=button.dataset.dir==='asc'?'desc':'asc';table.querySelectorAll('th').forEach(th=>{const b=th.querySelector('button');if(b)delete b.dataset.dir;th.setAttribute('aria-sort','none');});button.dataset.dir=next;button.closest('th').setAttribute('aria-sort',next==='asc'?'ascending':'descending');const rows=[...tbody.rows].sort((a,b)=>{const av=key(a.cells[col]?.textContent||'');const bv=key(b.cells[col]?.textContent||'');const cmp=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv));return next==='asc'?cmp:-cmp;});rows.forEach(row=>tbody.appendChild(row));});});
updateCounts();
""".strip()
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#050712">
<title>Degen Dogs Mission 3 Auctions</title>
<style>{css}</style>
</head>
<body>
<div class="shell">
  <section class="hero" aria-label="Auction overview">
    <div class="headline">
      <div class="eyebrow"><span class="dot"></span>Mission 3 auction feed</div>
      <h1>Latest bid + recent winners</h1>
      <p class="subtitle">{subtitle}</p>
      <p class="current-detail">{current_detail}</p>
    </div>
    <div class="metrics" aria-label="Key metrics">{cards}</div>
  </section>
  <div class="toolbar"><input id="filter" type="search" aria-label="filter visible tables" placeholder="Search current bid or recent winners" autocomplete="off"></div>
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
    current = fetch_current_auction(latest_block, latest_time, snapshot_tag)
    created, bids, settled = decode_auction_logs(created_logs, bid_logs, settled_logs)
    holders = fetch_woof_holders(transfer_logs, decimals, snapshot_tag)
    farcaster_profiles = fetch_farcaster_profiles(collect_identity_addresses(current, bids, settled, holders))

    conn = sqlite3.connect(":memory:")
    insert_rows(conn, "auction_created", created, [("token_id", "INTEGER"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT")])
    insert_rows(conn, "auction_bids", bids, [("token_id", "INTEGER"), ("bidder", "TEXT"), ("bid_eth", "REAL"), ("bid_wei", "TEXT"), ("extended", "INTEGER"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "auction_settled", settled, [("token_id", "INTEGER"), ("winner", "TEXT"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "woof_holders", holders, [("address", "TEXT"), ("balance_woof", "REAL"), ("balance_raw", "TEXT")])
    insert_rows(conn, "farcaster_profiles", farcaster_profiles, [("address", "TEXT"), ("fid", "INTEGER"), ("username", "TEXT"), ("display_name", "TEXT"), ("pfp_url", "TEXT")])
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

    metrics_cols, metrics_rows = tables["mission3_metrics"]
    readme = markdown_table(["table", "file", "rows"], manifest_rows) + "\n" + markdown_table(metrics_cols, metrics_rows)
    (ROOT / "README.md").write_text(readme, encoding="utf-8")

    print(json.dumps({"latest_block": latest_block, "tables": {k: len(v[1]) for k, v in tables.items()}}, indent=2))


if __name__ == "__main__":
    main()
