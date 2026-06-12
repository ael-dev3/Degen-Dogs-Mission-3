#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "archive_fetch_historical_prices.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("archive_fetch_historical_prices", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_unified(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_eth_window_extends_to_current_utc_day_for_ongoing_mission3_pricing() -> None:
    prices = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        unified = Path(tmp) / "unified_dog_search_index.json"
        write_unified(unified, [
            {
                "activity_time_utc": "2026-06-08T20:15:03Z",
                "amount": {"native": "0.03631", "price_asset_key": "ETH"},
            },
            {
                "activity_time_utc": "2026-01-06T00:00:00Z",
                "amount": {"native": "1000", "price_asset_key": "DEGEN"},
            },
        ])
        prices.UNIFIED = unified
        prices.utc_today = lambda: date(2026, 6, 12)

        windows = prices.collect_asset_windows()

    assert windows["ETH"] == (date(2026, 6, 8), date(2026, 6, 12))
    assert windows["DEGEN"] == (date(2026, 1, 6), date(2026, 1, 6))


def test_no_eth_window_does_not_invent_one() -> None:
    prices = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        unified = Path(tmp) / "unified_dog_search_index.json"
        write_unified(unified, [{"activity_time_utc": "2026-01-06T00:00:00Z", "amount": {"native": "1000", "price_asset_key": "DEGEN"}}])
        prices.UNIFIED = unified
        prices.utc_today = lambda: date(2026, 6, 12)

        windows = prices.collect_asset_windows()

    assert "ETH" not in windows
    assert windows["DEGEN"] == (date(2026, 1, 6), date(2026, 1, 6))


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"archive_fetch_historical_prices_tests=pass count={len(tests)}")