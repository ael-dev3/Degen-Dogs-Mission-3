#!/usr/bin/env python3
"""Fetch normalized daily historical USD prices for archive USD estimates."""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "archive" / "prices" / "config" / "asset_price_keys.json"
GENERATED = ROOT / "archive" / "prices" / "data" / "generated"
RAW = ROOT / "archive" / "prices" / "data" / "raw"
UNIFIED = ROOT / "archive" / "data" / "generated" / "unified_dog_search_index.json"
CG_BASE = "https://api.coingecko.com/api/v3"


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


def parse_event_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def ensure_unified_index() -> None:
    if UNIFIED.exists():
        return
    subprocess.run([sys.executable, "scripts/build_unified_dog_index.py"], cwd=ROOT, check=True)


def collect_asset_windows() -> dict[str, tuple[date, date]]:
    ensure_unified_index()
    records = load_json(UNIFIED, [])
    windows: dict[str, list[date]] = defaultdict(list)
    if not isinstance(records, list):
        return {}
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_amount = record.get("amount")
        amount = raw_amount if isinstance(raw_amount, dict) else {}
        if not amount.get("native"):
            continue
        asset_key = str(amount.get("price_asset_key") or "").strip()
        event_day = parse_event_date(record.get("activity_time_utc"))
        if asset_key and event_day:
            windows[asset_key].append(event_day)
    return {asset: (min(days), max(days)) for asset, days in windows.items() if days}


def unix_seconds(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def fetch_coingecko_range(coingecko_id: str, start: date, end: date) -> dict[str, Any]:
    start_ts = unix_seconds(start - timedelta(days=1))
    end_ts = unix_seconds(end + timedelta(days=2))
    params = urllib.parse.urlencode({"vs_currency": "usd", "from": start_ts, "to": end_ts})
    url = f"{CG_BASE}/coins/{urllib.parse.quote(coingecko_id)}/market_chart/range?{params}"
    headers = {"accept": "application/json", "user-agent": "degen-dogs-historical-prices/0.1"}
    if os.environ.get("COINGECKO_API_KEY"):
        headers["x-cg-demo-api-key"] = os.environ["COINGECKO_API_KEY"]
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == 3:
                break
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"CoinGecko fetch failed for {coingecko_id}: {last_error}")


def rows_from_timestamp_prices(
    asset_key: str,
    asset_cfg: dict[str, Any],
    samples: list[tuple[int, Decimal]],
    *,
    source: str,
    source_detail: str,
    confidence: str,
) -> list[dict[str, Any]]:
    by_day: dict[str, list[tuple[int, Decimal]]] = defaultdict(list)
    for ts, price in samples:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        by_day[day].append((ts, price))
    rows: list[dict[str, Any]] = []
    fetched_at = utc_now()
    for day, values in sorted(by_day.items()):
        # Prefer the first sample of the UTC day. Sources may return one daily sample
        # or a denser chart for shorter windows.
        ts, price = min(values, key=lambda item: item[0])
        timestamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append({
            "asset_key": asset_key,
            "symbol": asset_cfg.get("symbol") or asset_key,
            "chain": asset_cfg.get("chain") or "global",
            "date_utc": day,
            "timestamp_utc": timestamp,
            "price_usd": str(price),
            "source": source,
            "source_detail": source_detail,
            "confidence": confidence,
            "fetched_at_utc": fetched_at,
            "notes": asset_cfg.get("notes", ""),
        })
    return rows


def daily_rows_from_coingecko(asset_key: str, asset_cfg: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    samples: list[tuple[int, Decimal]] = []
    for item in payload.get("prices") or []:
        if not isinstance(item, list) or len(item) < 2:
            continue
        try:
            ms = int(item[0])
            samples.append((ms // 1000, Decimal(str(item[1]))))
        except Exception:
            continue
    return rows_from_timestamp_prices(
        asset_key,
        asset_cfg,
        samples,
        source="coingecko",
        source_detail=f"coins/{asset_cfg.get('coingecko_id')}/market_chart/range",
        confidence="high",
    )


def fetch_defillama_chart(coin_key: str, start: date, end: date) -> dict[str, Any]:
    all_prices: list[dict[str, Any]] = []
    cursor = start - timedelta(days=1)
    final = end + timedelta(days=2)
    while cursor <= final:
        chunk_end = min(cursor + timedelta(days=330), final)
        span = max(1, (chunk_end - cursor).days + 1)
        params = urllib.parse.urlencode({"start": unix_seconds(cursor), "span": span, "period": "1d"})
        url = f"https://coins.llama.fi/chart/{urllib.parse.quote(coin_key, safe=':')}?{params}"
        req = urllib.request.Request(url, headers={"accept": "application/json", "user-agent": "degen-dogs-historical-prices/0.1"})
        with urllib.request.urlopen(req, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
        coin_payload = (payload.get("coins") or {}).get(coin_key) or {}
        all_prices.extend(item for item in (coin_payload.get("prices") or []) if isinstance(item, dict))
        cursor = chunk_end + timedelta(days=1)
        if cursor <= final:
            time.sleep(0.35)
    return {"coins": {coin_key: {"prices": all_prices}}}


def daily_rows_from_defillama(asset_key: str, asset_cfg: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    coin_key = str(asset_cfg.get("defillama_coin") or "")
    coin_payload = (payload.get("coins") or {}).get(coin_key) or {}
    samples: list[tuple[int, Decimal]] = []
    for item in coin_payload.get("prices") or []:
        if not isinstance(item, dict):
            continue
        try:
            samples.append((int(item["timestamp"]), Decimal(str(item["price"]))))
        except Exception:
            continue
    return rows_from_timestamp_prices(
        asset_key,
        asset_cfg,
        samples,
        source="defillama_coin_prices",
        source_detail=f"coins.llama.fi/chart/{coin_key}",
        confidence="medium",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    cols = ["asset_key", "symbol", "chain", "date_utc", "timestamp_utc", "price_usd", "source", "source_detail", "confidence", "fetched_at_utc", "notes"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    config = load_json(CONFIG, {})
    assets = config.get("assets", {}) if isinstance(config, dict) else {}
    windows = collect_asset_windows()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    source_files: list[str] = []
    used_sources: set[str] = set()

    # Mission 1 WETH intentionally uses ETH price rows through price_asset_key=ETH.
    for asset_key, (start, end) in sorted(windows.items()):
        asset_cfg = assets.get(asset_key, {})
        if not isinstance(asset_cfg, dict):
            errors.append(f"{asset_key}: invalid asset config")
            continue
        coingecko_id = asset_cfg.get("coingecko_id")
        defillama_coin = asset_cfg.get("defillama_coin")
        fetched = False
        if coingecko_id:
            try:
                payload = fetch_coingecko_range(str(coingecko_id), start, end)
                raw_path = RAW / f"coingecko_{asset_key.lower()}_{start.isoformat()}_{end.isoformat()}.json"
                write_json(raw_path, {"asset_key": asset_key, "start_date": start.isoformat(), "end_date": end.isoformat(), "payload": payload})
                source_files.append(str(raw_path.relative_to(ROOT)))
                rows.extend(daily_rows_from_coingecko(asset_key, asset_cfg, payload))
                used_sources.add("coingecko_market_chart_range")
                fetched = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{asset_key}: CoinGecko failed: {exc}")
        if not fetched and defillama_coin:
            try:
                payload = fetch_defillama_chart(str(defillama_coin), start, end)
                raw_path = RAW / f"defillama_{asset_key.lower()}_{start.isoformat()}_{end.isoformat()}.json"
                write_json(raw_path, {"asset_key": asset_key, "start_date": start.isoformat(), "end_date": end.isoformat(), "payload": payload})
                source_files.append(str(raw_path.relative_to(ROOT)))
                fallback_rows = daily_rows_from_defillama(asset_key, asset_cfg, payload)
                if not fallback_rows:
                    raise RuntimeError("DefiLlama returned no chart rows")
                rows.extend(fallback_rows)
                used_sources.add("defillama_coin_prices")
                fetched = True
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{asset_key}: DefiLlama failed: {exc}")
        if not fetched:
            errors.append(f"{asset_key}: no price source produced rows")

    # De-duplicate rows by asset/date/source, keeping first source row.
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["asset_key"], row["date_utc"], row["source"])
        deduped.setdefault(key, row)
    rows = [deduped[key] for key in sorted(deduped)]

    GENERATED.mkdir(parents=True, exist_ok=True)
    write_json(GENERATED / "historical_prices_daily.json", rows)
    write_csv(GENERATED / "historical_prices_daily.csv", rows)
    manifest = {
        "schema_version": 1,
        "updated_at_utc": utc_now(),
        "asset_windows": {asset: {"start_date": s.isoformat(), "end_date": e.isoformat()} for asset, (s, e) in sorted(windows.items())},
        "rows": len(rows),
        "sources": sorted(used_sources),
        "source_files": source_files,
        "errors": errors,
        "notes": ["Missing source/API coverage leaves downstream USD estimates null; no zero fills are used."],
    }
    write_json(GENERATED / "price_manifest.json", manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
