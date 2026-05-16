#!/usr/bin/env python3
from __future__ import annotations

import csv
import concurrent.futures
import html
import json
import os
import sqlite3
import time
import urllib.error
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
    "current_auction",
    "season5_sup_by_winner",
    "season5_sup_rewards_by_auction",
    "auction_winners",
    "recent_bids",
    "top_woof_holders",
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
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(cols)
        writer.writerows(rows)


def table_html(name: str, cols: list[str], rows: list[tuple[Any, ...]]) -> str:
    head = "".join(
        f'<th><button type="button" data-col="{i}">{html.escape(col)}</button></th>'
        for i, col in enumerate(cols)
    )
    body = []
    for row in rows:
        body.append("<tr>" + "".join(f"<td>{html.escape('' if v is None else str(v))}</td>" for v in row) + "</tr>")
    caption = f"<caption><span>{html.escape(name)}</span><span>{len(rows)} rows</span></caption>"
    return f'<section class="table-wrap"><table data-table="{html.escape(name)}">{caption}<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></section>'


def markdown_table(cols: list[str], rows: list[tuple[Any, ...]], limit: int | None = None) -> str:
    selected = rows if limit is None else rows[:limit]
    out = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
    for row in selected:
        out.append("| " + " | ".join(str("" if v is None else v).replace("|", "\\|") for v in row) + " |")
    return "\n".join(out) + "\n"


def write_html(tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
    parts = [table_html(name, cols, rows) for name, (cols, rows) in tables.items()]
    css = """
:root{color-scheme:dark;--bg:#06080d;--panel:#0d1118;--panel2:#111722;--line:#242b36;--line2:#303949;--text:#e8ebf0;--muted:#8e99aa;--accent:#8bffcb;--shadow:0 18px 60px rgba(0,0,0,.35)}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,#132017 0,#06080d 38rem);color:var(--text);font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}.controls{position:sticky;top:0;z-index:20;padding:8px;background:linear-gradient(180deg,rgba(6,8,13,.98),rgba(6,8,13,.86));backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}#filter{width:100%;height:34px;border:1px solid var(--line2);border-radius:8px;background:#0b0f16;color:var(--text);font:12px ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;padding:0 10px;outline:none}#filter:focus{border-color:var(--accent);box-shadow:0 0 0 1px rgba(139,255,203,.15)}main{width:min(1760px,100%);margin:0 auto;padding:8px;display:grid;gap:10px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px;background:linear-gradient(180deg,var(--panel2),var(--panel));box-shadow:var(--shadow)}table{width:100%;border-collapse:separate;border-spacing:0;font-size:12px;line-height:1.35}caption{caption-side:top;text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);color:var(--text);font-weight:650;display:flex;justify-content:space-between;gap:16px;background:rgba(17,23,34,.92);position:sticky;left:0}caption span:last-child{color:var(--muted);font-weight:500}th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;white-space:nowrap;font-variant-numeric:tabular-nums}thead th{position:sticky;top:51px;z-index:5;background:#121925;color:var(--muted);box-shadow:inset 0 -1px 0 var(--line2)}th button{all:unset;display:block;width:100%;cursor:pointer}th button:hover{color:var(--accent)}tbody tr:nth-child(2n){background:rgba(255,255,255,.018)}tbody tr:hover{background:rgba(139,255,203,.055)}tbody tr[hidden]{display:none}tr:last-child td{border-bottom:0}td:first-child,th:first-child{padding-left:12px}@media(max-width:900px){.controls{padding:6px}main{padding:6px;gap:8px}.table-wrap{border-radius:0;border-left:0;border-right:0}thead th{top:47px}}
""".strip()
    script = """
const filter=document.getElementById('filter');
filter.addEventListener('input',()=>{const q=filter.value.trim().toLowerCase();document.querySelectorAll('tbody tr').forEach(tr=>{tr.hidden=q!==''&&!tr.textContent.toLowerCase().includes(q);});});
const key=v=>{const s=v.trim().replaceAll(',','');const n=Number(s);return s!==''&&Number.isFinite(n)?n:v.trim().toLowerCase();};
document.querySelectorAll('th button').forEach(button=>{button.addEventListener('click',()=>{const table=button.closest('table');const tbody=table.tBodies[0];const col=Number(button.dataset.col);const next=button.dataset.dir==='asc'?'desc':'asc';table.querySelectorAll('th button').forEach(b=>delete b.dataset.dir);button.dataset.dir=next;const rows=[...tbody.rows].sort((a,b)=>{const av=key(a.cells[col]?.textContent||'');const bv=key(b.cells[col]?.textContent||'');const cmp=typeof av==='number'&&typeof bv==='number'?av-bv:String(av).localeCompare(String(bv));return next==='asc'?cmp:-cmp;});rows.forEach(row=>tbody.appendChild(row));});});
""".strip()
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#06080d">
<title>Degen Dogs Mission 3</title>
<style>{css}</style>
</head>
<body>
<div class="controls"><input id="filter" type="search" aria-label="filter rows" placeholder="filter" autocomplete="off"></div>
<main>{''.join(parts)}</main>
<script>{script}</script>
</body>
</html>
"""
    (ROOT / "index.html").write_text(html_doc, encoding="utf-8")


def main() -> None:
    GENERATED.mkdir(exist_ok=True)
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

    conn = sqlite3.connect(":memory:")
    insert_rows(conn, "auction_created", created, [("token_id", "INTEGER"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT")])
    insert_rows(conn, "auction_bids", bids, [("token_id", "INTEGER"), ("bidder", "TEXT"), ("bid_eth", "REAL"), ("bid_wei", "TEXT"), ("extended", "INTEGER"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "auction_settled", settled, [("token_id", "INTEGER"), ("winner", "TEXT"), ("amount_eth", "REAL"), ("amount_wei", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    insert_rows(conn, "woof_holders", holders, [("address", "TEXT"), ("balance_woof", "REAL"), ("balance_raw", "TEXT")])
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
        manifest_rows.append((table, f"generated/{table}.csv", len(rows)))

    write_csv(GENERATED / "manifest.csv", ["table", "file", "rows"], manifest_rows)
    write_html(tables)

    metrics_cols, metrics_rows = tables["mission3_metrics"]
    readme = markdown_table(["table", "file", "rows"], manifest_rows) + "\n" + markdown_table(metrics_cols, metrics_rows)
    (ROOT / "README.md").write_text(readme, encoding="utf-8")

    print(json.dumps({"latest_block": latest_block, "tables": {k: len(v[1]) for k, v in tables.items()}}, indent=2))


if __name__ == "__main__":
    main()
