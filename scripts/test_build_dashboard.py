#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_dashboard.py"


def load_module() -> Any:
    os.environ["MISSION3_LOG_CACHE"] = "1"
    os.environ["MISSION3_BALANCE_CACHE"] = "1"
    spec = importlib.util.spec_from_file_location("build_dashboard", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def log(block: int, tx: str, index: int) -> dict[str, Any]:
    return {"blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(index), "data": "0x", "topics": []}


def test_fetch_logs_extends_cached_ranges_with_overlap_and_dedupes() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        setattr(dashboard, "LOG_CACHE_OVERLAP_BLOCKS", 5)
        calls: list[tuple[int, int]] = []

        def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
            calls.append((start, end))
            if len(calls) == 1:
                return [log(100, "0xaaa", 0), log(150, "0xbbb", 2)]
            return [log(150, "0xbbb", 2), log(160, "0xccc", 1)]

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        first = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 150)
        assert [item["transactionHash"] for item in first] == ["0xaaa", "0xbbb"]
        assert calls == [(100, 150)]

        second = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 175)
        assert calls == [(100, 150), (146, 175)]
        assert [item["transactionHash"] for item in second] == ["0xaaa", "0xbbb", "0xccc"]

        third = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 175)
        assert calls == [(100, 150), (146, 175)]
        assert [item["transactionHash"] for item in third] == ["0xaaa", "0xbbb", "0xccc"]


def test_fetch_logs_caches_empty_ranges() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        calls: list[tuple[int, int]] = []

        def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
            calls.append((start, end))
            return []

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        assert dashboard.fetch_logs("0xabc", [dashboard.TOPIC_AUCTION_CREATED], 200, 250) == []
        assert dashboard.fetch_logs("0xabc", [dashboard.TOPIC_AUCTION_CREATED], 200, 250) == []
        assert calls == [(200, 250)]


def address_topic(address: str) -> str:
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def transfer_log(dashboard: Any, block: int, from_address: str, to_address: str) -> dict[str, Any]:
    return {
        "blockNumber": hex(block),
        "transactionHash": f"0x{block:064x}",
        "logIndex": "0x0",
        "topics": [dashboard.TOPIC_TRANSFER, address_topic(from_address), address_topic(to_address)],
        "data": "0x",
    }


def test_fetch_woof_holders_reuses_cached_balances_until_address_is_touched() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    carol = "0x00000000000000000000000000000000000000c3"
    balances = {alice: 100, bob: 200, carol: 300}

    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "WOOF_BALANCE_CACHE", Path(tmp) / "woof_balances.json")
        calls: list[list[str]] = []

        def fake_fetch(addresses: list[str], _block_tag: str) -> dict[str, int]:
            calls.append(addresses)
            return {address: balances[address] for address in addresses}

        setattr(dashboard, "fetch_balances", fake_fetch)
        first_logs = [transfer_log(dashboard, 100, alice, bob)]
        first = dashboard.fetch_woof_holders(first_logs, 0, "0x64")
        assert calls == [[alice, bob]]
        assert [(row["address"], row["balance_raw"]) for row in first] == [(bob, "200"), (alice, "100")]

        second = dashboard.fetch_woof_holders(first_logs, 0, "0x65")
        assert calls == [[alice, bob], []]
        assert [(row["address"], row["balance_raw"]) for row in second] == [(bob, "200"), (alice, "100")]

        balances[bob] = 250
        third_logs = [*first_logs, transfer_log(dashboard, 102, bob, carol)]
        third = dashboard.fetch_woof_holders(third_logs, 0, "0x66")
        assert calls == [[alice, bob], [], [bob, carol]]
        assert [(row["address"], row["balance_raw"]) for row in third] == [(carol, "300"), (bob, "250"), (alice, "100")]


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"build_dashboard_tests=pass count={len(tests)}")
