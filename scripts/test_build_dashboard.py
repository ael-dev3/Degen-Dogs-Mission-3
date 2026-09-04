#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import base64
import hashlib
import html as html_module
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
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
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def log(block: int, tx: str, index: int) -> dict[str, Any]:
    return {"blockNumber": hex(block), "transactionHash": tx, "logIndex": hex(index), "data": "0x", "topics": []}


def canonical_log(
    address: str,
    topic: str,
    block: int,
    *,
    transaction_index: int = 0,
    log_index: int = 0,
) -> dict[str, Any]:
    seed = f"{block}:{transaction_index}:{log_index}".encode("utf-8")
    return {
        "address": address,
        "blockHash": "0x" + hashlib.sha256(b"block:" + str(block).encode("ascii")).hexdigest(),
        "blockNumber": hex(block),
        "data": "0x",
        "logIndex": hex(log_index),
        "removed": False,
        "topics": [topic],
        "transactionHash": "0x" + hashlib.sha256(b"tx:" + seed).hexdigest(),
        "transactionIndex": hex(transaction_index),
    }


def rarity_attributes(**overrides: str) -> list[dict[str, str]]:
    values = {
        "Background": "None",
        "Body": "Black",
        "Neck": "None",
        "Mouth": "None",
        "Ears": "None",
        "Head": "None",
        "Eyes": "None",
        **overrides,
    }
    return [{"trait_type": trait_type, "value": value} for trait_type, value in values.items()]


def test_atomic_writer_never_follows_target_or_parent_symlinks() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        external = root / "external.txt"
        external.write_text("do-not-overwrite", encoding="utf-8")
        target = root / "target.txt"
        target.symlink_to(external)
        dashboard.atomic_write_text(target, "safe replacement")
        assert external.read_text(encoding="utf-8") == "do-not-overwrite"
        assert not target.is_symlink()
        assert target.read_text(encoding="utf-8") == "safe replacement"

        real_dir = root / "real"
        real_dir.mkdir()
        linked_dir = root / "linked"
        linked_dir.symlink_to(real_dir, target_is_directory=True)
        try:
            dashboard.atomic_write_text(linked_dir / "escape.txt", "blocked")
        except RuntimeError as exc:
            assert "unsafe output directory" in str(exc)
        else:
            raise AssertionError("atomic writer followed a symlinked output directory")
        assert not (real_dir / "escape.txt").exists()

        owned = root / "owned"
        owned.mkdir()
        outside = root / "outside"
        outside.mkdir()
        ancestor_link = owned / "parent-link"
        ancestor_link.symlink_to(outside, target_is_directory=True)
        try:
            dashboard.atomic_write_text(ancestor_link / "created" / "escape.txt", "blocked")
        except RuntimeError as exc:
            assert "unsafe output directory ancestor" in str(exc)
        else:
            raise AssertionError("atomic writer followed a nested ancestor symlink")
        assert not (outside / "created").exists()


def test_quicknode_hostname_variants_share_one_quorum_vote() -> None:
    dashboard = load_module()
    assert dashboard._rpc_provider_key("https://alpha.quiknode.pro/key-a") == "quicknode"
    assert dashboard._rpc_provider_key("https://beta.quiknode.pro/key-b") == "quicknode"
    assert dashboard._rpc_provider_key("https://legacy.quicknode.pro/key") == "quicknode"
    assert dashboard._rpc_provider_key("https://base-mainnet.g.alchemy.com/public") == "alchemy"
    assert dashboard._rpc_provider_key("https://base-mainnet.public.blastapi.io") == "alchemy"
    custom_provider = dashboard._rpc_provider_key(
        "https://host-secret.rpc.custom.example/path-secret?token=query-secret"
    )
    custom_log_url = dashboard._redact_rpc_url(
        "https://host-secret.rpc.custom.example/path-secret?token=query-secret"
    )
    assert custom_provider.startswith("rpc-host-")
    assert custom_log_url.startswith("https://rpc-host-")
    assert "host-secret" not in custom_provider + custom_log_url
    assert "path-secret" not in custom_provider + custom_log_url
    assert "query-secret" not in custom_provider + custom_log_url
    base_failovers = dashboard._same_operator_rpc_urls("https://mainnet.base.org")
    assert "https://mainnet.base.org" in base_failovers
    assert "https://developer-access-mainnet.base.org" in base_failovers


def test_explicit_rpc_configuration_does_not_silently_add_public_fallbacks() -> None:
    names = ("BASE_RPC_URL", "BASE_RPC_URLS", "BASE_LOG_RPC_URLS", "BASE_INCLUDE_PUBLIC_FALLBACKS")
    saved = {name: os.environ.get(name) for name in names}
    try:
        os.environ.pop("BASE_RPC_URL", None)
        os.environ.pop("BASE_LOG_RPC_URLS", None)
        os.environ["BASE_RPC_URLS"] = "https://rpc.provider-one.example/key,https://rpc.provider-two.example/key"
        os.environ["BASE_INCLUDE_PUBLIC_FALLBACKS"] = "0"
        dashboard = load_module()
        assert dashboard._configured_rpc_urls() == [
            "https://rpc.provider-one.example/key",
            "https://rpc.provider-two.example/key",
        ]
        assert not any(url in dashboard._configured_rpc_urls() for url in dashboard.DEFAULT_RPC_URLS)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_blank_rpc_environment_uses_working_public_defaults() -> None:
    names = ("BASE_RPC_URL", "BASE_RPC_URLS", "BASE_LOG_RPC_URLS", "BASE_INCLUDE_PUBLIC_FALLBACKS")
    saved = {name: os.environ.get(name) for name in names}
    try:
        os.environ["BASE_RPC_URL"] = ""
        os.environ["BASE_RPC_URLS"] = ""
        os.environ["BASE_LOG_RPC_URLS"] = ""
        os.environ["BASE_INCLUDE_PUBLIC_FALLBACKS"] = "0"
        dashboard = load_module()
        assert dashboard.RPC_URLS == dashboard.DEFAULT_RPC_URLS
        assert dashboard.LOG_RPC_URLS == dashboard.DEFAULT_LOG_RPC_URLS
        assert dashboard._quorum_rpc_urls()
        assert len({dashboard._rpc_provider_key(url) for url in dashboard.LOG_RPC_URLS}) >= 2
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_rpc_and_credentialed_requests_reject_redirects_before_forwarding_headers() -> None:
    dashboard = load_module()
    request = dashboard.urllib.request.Request(
        "https://api.neynar.com/v2/farcaster/user/bulk-by-address",
        headers={"x-api-key": "secret"},
    )
    handler = dashboard.NoRedirectHandler()
    try:
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example/steal",
        )
    except dashboard.urllib.error.HTTPError as exc:
        assert exc.code == 302
        assert "redirect" in str(exc).lower()
    else:
        raise AssertionError("credentialed request was allowed to follow a redirect")


def test_rpc_transport_is_https_exact_bounded_and_secret_safe() -> None:
    dashboard = load_module()
    original_open = dashboard.open_no_redirect

    class Response:
        def __init__(
            self,
            body: bytes,
            *,
            content_type: str = "application/json",
            status: int = 200,
            final_url: str = "",
        ) -> None:
            self.body = body
            self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
            self.status = status
            self.final_url = final_url
            self.read_limit = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self) -> int:
            return self.status

        def geturl(self) -> str:
            return self.final_url

        def read(self, limit: int) -> bytes:
            self.read_limit = limit
            return self.body[:limit]

    url = "https://custom-provider.example/v2/path-secret?api_key=query-secret"
    encoded = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'
    try:
        response = Response(encoded, content_type="application/json; charset=utf-8", final_url=url)
        dashboard.open_no_redirect = lambda *_args, **_kwargs: response
        assert dashboard.post_json({"jsonrpc": "2.0"}, 3, url)["result"] == "0x1"
        assert response.read_limit == dashboard.RPC_MAX_RESPONSE_BYTES + 1

        dashboard.open_no_redirect = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe RPC URL reached the network")
        )
        for unsafe in (
            "http://custom-provider.example/rpc",
            "https://user:password-secret@custom-provider.example/rpc",
            "https://custom-provider.example:8443/rpc",
            "https://custom-provider.example/rpc#fragment",
        ):
            try:
                dashboard.post_json({}, 3, unsafe)
            except RuntimeError as exc:
                assert "password-secret" not in str(exc)
            else:
                raise AssertionError(f"unsafe RPC URL was accepted: {unsafe}")

        cases = [
            (Response(encoded, final_url="https://attacker.example/rpc"), "URL changed"),
            (Response(encoded, status=206, final_url=url), "HTTP status"),
            (Response(encoded, content_type="text/html", final_url=url), "content type"),
        ]
        for unsafe_response, expected_error in cases:
            dashboard.open_no_redirect = lambda *_args, _response=unsafe_response, **_kwargs: _response
            try:
                dashboard.post_json({}, 3, url)
            except RuntimeError as exc:
                assert expected_error in str(exc)
            else:
                raise AssertionError(f"unsafe RPC response was accepted: {expected_error}")

        oversized = Response(b"{}", final_url=url)
        oversized.headers["Content-Length"] = str(dashboard.RPC_MAX_RESPONSE_BYTES + 1)
        dashboard.open_no_redirect = lambda *_args, **_kwargs: oversized
        try:
            dashboard.post_json({}, 3, url)
        except RuntimeError as exc:
            assert "exceeds" in str(exc)
        else:
            raise AssertionError("oversize RPC response was accepted")
        assert oversized.read_limit is None

        invalid_utf8 = Response(b"\xff", final_url=url)
        dashboard.open_no_redirect = lambda *_args, **_kwargs: invalid_utf8
        try:
            dashboard.post_json({}, 3, url)
        except RuntimeError as exc:
            assert "invalid JSON" in str(exc)
        else:
            raise AssertionError("invalid UTF-8 RPC response was accepted")

        secret_error = dashboard.urllib.error.HTTPError(url, 401, "body-secret", {}, None)
        dashboard.open_no_redirect = lambda *_args, **_kwargs: (_ for _ in ()).throw(secret_error)
        try:
            dashboard.post_json({}, 3, url)
        except RuntimeError as exc:
            assert str(exc) == "HTTP 401"
            assert "path-secret" not in str(exc)
            assert "query-secret" not in str(exc)
            assert "body-secret" not in str(exc)
        else:
            raise AssertionError("RPC HTTP failure was accepted")
    finally:
        dashboard.open_no_redirect = original_open


def test_rpc_uses_strict_single_response_envelope() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    calls: list[tuple[str, str, list[Any], int]] = []

    def fake_once(url: str, method: str, params: list[Any], *, timeout: int = 30) -> str:
        calls.append((url, method, params, timeout))
        return "0x2a"

    try:
        dashboard._rpc_once = fake_once
        assert dashboard.rpc("eth_blockNumber", [], timeout=7, urls=["https://rpc.example"]) == "0x2a"
    finally:
        dashboard._rpc_once = old_rpc_once
    assert calls == [("https://rpc.example", "eth_blockNumber", [], 7)]

    old_post_json = dashboard.post_json
    try:
        for malformed in (
            {"jsonrpc": "2.0", "id": True, "result": "0x1"},
            {"jsonrpc": "1.0", "id": 1, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": "0x1", "error": {"code": -1}},
        ):
            dashboard.post_json = lambda _payload, _timeout, _url, response=malformed: response
            try:
                dashboard._rpc_once("https://rpc.example", "eth_call", [], timeout=1)
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"malformed single JSON-RPC envelope was accepted: {malformed!r}")

        dashboard.post_json = lambda *_args: {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "provider-body-secret", "data": "provider-data-secret"},
        }
        try:
            dashboard._rpc_once("https://rpc.example", "eth_call", [], timeout=1)
        except RuntimeError as exc:
            assert str(exc) == "JSON-RPC error code=-32601 for eth_call"
            assert "secret" not in str(exc)
        else:
            raise AssertionError("JSON-RPC error response was accepted")
    finally:
        dashboard.post_json = old_post_json


def test_rpc_batch_rejects_malformed_duplicate_and_incomplete_envelopes() -> None:
    dashboard = load_module()
    old_post_json = dashboard.post_json
    old_attempts = dashboard.RPC_ATTEMPTS
    old_sleep = dashboard.time.sleep
    calls = [("eth_call", []), ("eth_blockNumber", [])]
    malformed_responses = (
        {"jsonrpc": "2.0", "id": 0, "result": "0x1"},
        [
            {"jsonrpc": "2.0", "id": 0, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 0, "result": "0x2"},
        ],
        [
            {"jsonrpc": "2.0", "id": 0, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 3, "result": "0x2"},
        ],
        [
            {"jsonrpc": "1.0", "id": 0, "result": "0x1"},
            {"jsonrpc": "2.0", "id": 1, "result": "0x2"},
        ],
        [
            {"jsonrpc": "2.0", "id": 0, "result": "0x1", "error": {"code": -1}},
            {"jsonrpc": "2.0", "id": 1, "result": "0x2"},
        ],
        [
            {"jsonrpc": "2.0", "id": 0, "error": {"code": True, "message": "invalid bool code"}},
            {"jsonrpc": "2.0", "id": 1, "result": "0x2"},
        ],
        [{"jsonrpc": "2.0", "id": 0, "result": "0x1"}],
    )
    try:
        dashboard.RPC_ATTEMPTS = 1
        dashboard.time.sleep = lambda _seconds: None
        for malformed in malformed_responses:
            dashboard.post_json = lambda _payload, _timeout, _url, response=malformed: response
            try:
                dashboard.rpc_batch(calls, timeout=1, urls=["https://rpc.example"])
            except RuntimeError:
                pass
            else:
                raise AssertionError(f"malformed JSON-RPC batch envelope was accepted: {malformed!r}")
    finally:
        dashboard.post_json = old_post_json
        dashboard.RPC_ATTEMPTS = old_attempts
        dashboard.time.sleep = old_sleep


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
        assert calls == [(100, 150), (146, 175), (171, 175)]
        assert [item["transactionHash"] for item in third] == ["0xaaa", "0xbbb", "0xccc"]


def test_fetch_logs_replaces_reorged_overlap_instead_of_retaining_orphans() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        setattr(dashboard, "LOG_CACHE_DIR", Path(tmp))
        setattr(dashboard, "LOG_CACHE_OVERLAP_BLOCKS", 10)
        calls = 0

        def fake_fetch(_address: str, _topics: str | list[str], _start: int, _end: int) -> list[dict[str, Any]]:
            nonlocal calls
            calls += 1
            if calls == 1:
                orphan = log(150, "0xorphan", 0)
                orphan["blockHash"] = "0xold"
                return [log(100, "0xstable", 0), orphan]
            replacement = log(150, "0xcanonical", 0)
            replacement["blockHash"] = "0xnew"
            return [replacement]

        setattr(dashboard, "_fetch_logs_uncached", fake_fetch)
        first = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 150)
        assert [item["transactionHash"] for item in first] == ["0xstable", "0xorphan"]

        second = dashboard.fetch_logs("0x123", dashboard.TOPIC_TRANSFER, 100, 155)
        assert [item["transactionHash"] for item in second] == ["0xstable", "0xcanonical"]
        assert all(item["transactionHash"] != "0xorphan" for item in second)


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
        assert calls == [(200, 250), (200, 250)]


def test_fetch_logs_checkpoints_completed_batches_before_transient_failure() -> None:
    dashboard = load_module()
    calls: list[tuple[int, int]] = []
    old_cache_dir = dashboard.LOG_CACHE_DIR
    old_chunk = dashboard.LOG_CHUNK
    old_workers = dashboard.LOG_WORKERS
    old_overlap = dashboard.LOG_CACHE_OVERLAP_BLOCKS

    def fake_fetch(_address: str, _topics: str | list[str], start: int, end: int) -> list[dict[str, Any]]:
        calls.append((start, end))
        if len(calls) == 2:
            raise RuntimeError("transient provider failure")
        return [{
            "address": "0xabc",
            "blockHash": f"0x{end:064x}",
            "blockNumber": hex(end),
            "data": "0x",
            "logIndex": "0x0",
            "removed": False,
            "topics": [dashboard.TOPIC_AUCTION_CREATED],
            "transactionHash": f"0x{end + 1:064x}",
        }]

    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.LOG_CACHE_DIR = Path(tmp)
            dashboard.LOG_CHUNK = 10
            dashboard.LOG_WORKERS = 1
            dashboard.LOG_CACHE_OVERLAP_BLOCKS = 0
            dashboard._fetch_logs_uncached = fake_fetch
            try:
                dashboard.fetch_logs("0xabc", dashboard.TOPIC_AUCTION_CREATED, 100, 200)
            except RuntimeError as exc:
                assert "transient provider failure" in str(exc)
            else:
                raise AssertionError("expected transient provider failure")
            cache_path = dashboard._log_cache_path("0xabc", dashboard.TOPIC_AUCTION_CREATED, 100)
            cached_to, cached_logs = dashboard._load_log_cache(
                cache_path,
                "0xabc",
                dashboard.TOPIC_AUCTION_CREATED,
                100,
            )
            assert cached_to == 109
            assert [int(row["blockNumber"], 16) for row in cached_logs] == [109]
        finally:
            dashboard.LOG_CACHE_DIR = old_cache_dir
            dashboard.LOG_CHUNK = old_chunk
            dashboard.LOG_WORKERS = old_workers
            dashboard.LOG_CACHE_OVERLAP_BLOCKS = old_overlap


def test_corrupt_log_cache_is_a_miss_instead_of_a_refresh_crash() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "logs.json"
        base = {
            "schema_version": 1,
            "address": "0xabc",
            "topics": [dashboard.TOPIC_TRANSFER.lower()],
            "from_block": 100,
            "to_block": 110,
            "logs": [],
        }
        for mutation in (
            {"from_block": "100"},
            {"to_block": "broken"},
            {"to_block": 99},
            {"logs": [{"blockNumber": "not-hex", "transactionHash": "0xtx", "logIndex": "0x0", "topics": []}]},
            {"logs": [{"blockNumber": "0x70", "transactionHash": "0xtx", "logIndex": "0x0", "topics": []}]},
        ):
            path.write_text(json.dumps({**base, **mutation}), encoding="utf-8")
            assert dashboard._load_log_cache(path, "0xabc", dashboard.TOPIC_TRANSFER, 100) == (0, [])


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


def test_fetch_woof_holders_refreshes_every_new_block_for_supertoken_accounting() -> None:
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
        assert calls == [[alice, bob], [alice, bob]]
        assert [(row["address"], row["balance_raw"]) for row in second] == [(bob, "200"), (alice, "100")]

        balances[bob] = 250
        third_logs = [*first_logs, transfer_log(dashboard, 102, bob, carol)]
        third = dashboard.fetch_woof_holders(third_logs, 0, "0x66")
        assert calls == [[alice, bob], [alice, bob], [alice, bob, carol]]
        assert [(row["address"], row["balance_raw"]) for row in third] == [(carol, "300"), (bob, "250"), (alice, "100")]


def test_woof_holder_summary_never_turns_unavailable_into_zero() -> None:
    dashboard = load_module()
    unavailable = {
        "woof_holder_verification_status": "unavailable_fail_closed",
        "woof_holders": "0",
    }
    assert dashboard.woof_holder_summary(unavailable) == "Unavailable (onchain verification incomplete)"
    verified = {
        "woof_holder_verification_status": "candidate_complete_onchain_quorum_verified",
        "woof_holders": "42",
    }
    assert dashboard.woof_holder_summary(verified) == "42"


def test_woof_holder_completeness_rejects_a_missing_funded_holder_log() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    dashboard.fetch_balances = lambda addresses, _block_tag: {address: 60 for address in addresses}
    with tempfile.TemporaryDirectory() as tmp:
        dashboard.WOOF_BALANCE_CACHE = Path(tmp) / "woof_balances.json"
        incomplete_logs = [transfer_log(dashboard, 100, dashboard.ZERO, alice)]
        try:
            dashboard.fetch_woof_holders(
                incomplete_logs,
                18,
                "0x64",
                expected_total_supply_raw="100",
            )
        except RuntimeError as exc:
            assert "holder completeness mismatch" in str(exc)
        else:
            raise AssertionError("missing funded WOOF holder was accepted as a complete holder set")


def test_corrupt_woof_balance_cache_is_a_miss() -> None:
    dashboard = load_module()
    address = "0x00000000000000000000000000000000000000a1"
    with tempfile.TemporaryDirectory() as tmp:
        dashboard.WOOF_BALANCE_CACHE = Path(tmp) / "woof_balances.json"
        base = {
            "schema_version": 1,
            "woof_token": dashboard.WOOF.lower(),
            "checked_block": 100,
            "balances": {address: "10"},
        }
        for mutation in (
            {"checked_block": "100"},
            {"checked_block": -1},
            {"balances": {address: "-1"}},
            {"balances": {"0xnot-an-address": "10"}},
        ):
            dashboard.WOOF_BALANCE_CACHE.write_text(json.dumps({**base, **mutation}), encoding="utf-8")
            assert dashboard.load_woof_balance_cache() == {}


def test_blockscout_holder_discovery_is_strict_bounded_and_paginated() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    pages = [
        {
            "items": [{"address": {"hash": alice}, "value": "60"}],
            "next_page_params": {
                "value": "60",
                "address_hash": alice,
                "items_count": 1,
            },
        },
        {
            "items": [{"address": {"hash": bob}, "value": "40"}],
            "next_page_params": None,
        },
    ]
    opened_urls: list[str] = []

    class FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int | None = None) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    class FakeOpener:
        def open(self, request: Any, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
            opened_urls.append(request.full_url)
            return FakeResponse(pages[len(opened_urls) - 1])

    old_build_opener = dashboard.urllib.request.build_opener
    try:
        dashboard.urllib.request.build_opener = lambda *_handlers: FakeOpener()
        assert dashboard.fetch_woof_holder_candidates() == [alice, bob]
    finally:
        dashboard.urllib.request.build_opener = old_build_opener

    assert len(opened_urls) == 2
    assert opened_urls[0] == dashboard.DEFAULT_WOOF_HOLDER_DISCOVERY_URL
    assert "address_hash=" in opened_urls[1] and "items_count=1" in opened_urls[1]
    for unsafe in (
        "https://base.blockscout.com:444/api/v2/tokens/" + dashboard.WOOF + "/holders",
        "https://evil.example/api/v2/tokens/" + dashboard.WOOF + "/holders",
        "https://base.blockscout.com/api/v2/tokens/" + dashboard.WOOF + "/holders/extra",
        dashboard.DEFAULT_WOOF_HOLDER_DISCOVERY_URL + "?redirect=https://evil.example",
    ):
        try:
            dashboard.validate_woof_holder_discovery_url(unsafe)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"unsafe WOOF holder discovery URL was accepted: {unsafe}")


def test_holder_surface_scope_is_added_only_after_exact_completeness() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    old_candidates = dashboard.fetch_woof_holder_candidates
    old_holders = dashboard.fetch_woof_holders
    try:
        dashboard.fetch_woof_holder_candidates = lambda: [alice]
        token_stats = {
            "woof_total_supply_raw": "100",
            "onchain_verification_scope": "snapshot_hash,woof_token_state",
        }
        incomplete = RuntimeError("holder completeness mismatch")
        setattr(incomplete, "observed_supply", 60)
        setattr(incomplete, "expected_supply", 100)

        def fail_holders(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            raise incomplete

        dashboard.fetch_woof_holders = fail_holders
        assert dashboard.fetch_verified_woof_holders(18, "0x64", token_stats) == []
        assert token_stats["woof_holder_verification_status"] == "unavailable_fail_closed"
        assert "woof_holder_balances" not in token_stats["onchain_verification_scope"]
        assert token_stats["woof_holder_balance_sum_raw"] == "60"

        dashboard.fetch_woof_holders = lambda *_args, **_kwargs: [{
            "address": alice,
            "balance_woof": 0.0,
            "balance_raw": "100",
        }]
        complete_stats = {
            "woof_total_supply_raw": "100",
            "onchain_verification_scope": "snapshot_hash,woof_token_state",
        }
        rows = dashboard.fetch_verified_woof_holders(18, "0x64", complete_stats)
        assert rows[0]["balance_raw"] == "100"
        assert complete_stats["woof_holder_verification_status"] == "candidate_complete_onchain_quorum_verified"
        assert "woof_holder_balances" in complete_stats["onchain_verification_scope"].split(",")
    finally:
        dashboard.fetch_woof_holder_candidates = old_candidates
        dashboard.fetch_woof_holders = old_holders


def test_fetch_farcaster_profiles_stops_after_neynar_auth_failure() -> None:
    dashboard = load_module()
    original_key_loader = dashboard.load_neynar_api_key
    original_build_opener = dashboard.urllib.request.build_opener
    original_sleep = dashboard.time.sleep
    try:
        for code in (401, 403):
            calls: list[str] = []
            sleeps: list[float] = []

            class FakeOpener:
                def open(self, req: Any, timeout: int = 0, *, status_code: int = code) -> Any:  # noqa: ARG002
                    calls.append(req.full_url)
                    raise dashboard.urllib.error.HTTPError(req.full_url, status_code, "Auth failed", {}, None)

            dashboard.load_neynar_api_key = lambda: "bad-key"
            dashboard.urllib.request.build_opener = lambda *_handlers: FakeOpener()
            dashboard.time.sleep = lambda seconds: sleeps.append(seconds)
            addresses = [f"0x{i:040x}" for i in range(205)]
            assert dashboard.fetch_farcaster_profiles(addresses) == []
            assert len(calls) == 1
            assert sleeps == []
    finally:
        dashboard.load_neynar_api_key = original_key_loader
        dashboard.urllib.request.build_opener = original_build_opener
        dashboard.time.sleep = original_sleep


def test_degendogs_auction_profiles_include_all_current_bid_history_bidders() -> None:
    dashboard = load_module()
    original_build_opener = dashboard.urllib.request.build_opener
    current_bidder = "0x00000000000000000000000000000000000000b2"
    early_bidder = "0x00000000000000000000000000000000000000c3"

    class FakeResponse:
        headers: dict[str, str] = {"Content-Type": "application/json"}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int | None = None) -> bytes:
            return json.dumps({
                "nounId": 11,
                "bidder": current_bidder,
                "amount": 1.0,
                "bids": [
                    {"nounId": 11, "bidder": current_bidder, "username": "unitcurrent", "pfp_url": ""},
                    {"nounId": 11, "bidder": early_bidder, "username": "unitearly", "pfp_url": ""},
                ],
            }).encode("utf-8")

    class FakeOpener:
        def open(self, _req: Any, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
            return FakeResponse()

    try:
        dashboard.urllib.request.build_opener = lambda *_handlers: FakeOpener()
        rows = dashboard.fetch_degendogs_auction_profiles({"token_id": 11, "bidder": current_bidder, "amount_eth": 1.0})
    finally:
        dashboard.urllib.request.build_opener = original_build_opener

    by_address = {row["address"]: row for row in rows}
    assert by_address[current_bidder]["username"] == "unitcurrent"
    assert by_address[early_bidder]["username"] == "unitearly"


def test_optional_identity_apis_ignore_malformed_success_payloads() -> None:
    dashboard = load_module()
    address = "0x00000000000000000000000000000000000000b2"

    class FakeResponse:
        headers: dict[str, str] = {"Content-Type": "application/json"}

        def __init__(self, payload: Any) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int | None = None) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    class FakeOpener:
        def __init__(self, payload: Any) -> None:
            self.payload = payload

        def open(self, _req: Any, timeout: int = 0) -> FakeResponse:  # noqa: ARG002
            return FakeResponse(self.payload)

    old_key_loader = dashboard.load_neynar_api_key
    old_build_opener = dashboard.urllib.request.build_opener
    try:
        dashboard.load_neynar_api_key = lambda: "unit-key"
        for payload in (
            ["unexpected-list"],
            {address: "not-a-user-list"},
            {address: [None, "bad", {"fid": "invalid", "verified_addresses": []}]},
        ):
            dashboard.urllib.request.build_opener = lambda *_handlers, value=payload: FakeOpener(value)
            rows = dashboard.fetch_farcaster_profiles([address])
            assert isinstance(rows, list)

        current = {"token_id": 11, "bidder": address, "amount_eth": 1.0}
        for payload in (
            ["unexpected-list"],
            {"nounId": 11, "bidder": address, "amount": 1.0, "bids": "not-a-list"},
            {"nounId": 11, "bidder": address, "amount": 1.0, "bids": [None, "bad"]},
        ):
            dashboard.urllib.request.build_opener = lambda *_handlers, value=payload: FakeOpener(value)
            assert dashboard.fetch_degendogs_auction_profiles(current) == []
    finally:
        dashboard.load_neynar_api_key = old_key_loader
        dashboard.urllib.request.build_opener = old_build_opener


def test_cached_wallet_identity_profiles_can_backfill_dashboard_labels() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000d4"
    with tempfile.TemporaryDirectory() as tmp:
        identity_path = Path(tmp) / "wallet_profiles.json"
        identity_path.write_text(json.dumps({
            wallet: {
                "wallet": wallet,
                "display": "@cachedbidder",
                "farcaster_handle": "cachedbidder",
                "farcaster_fid": 104,
                "profile_url": "https://farcaster.xyz/cachedbidder",
            }
        }), encoding="utf-8")
        rows = dashboard.load_cached_farcaster_profiles(identity_path)

    assert rows == [{
        "address": wallet,
        "fid": 104,
        "username": "cachedbidder",
        "display_name": "@cachedbidder",
        "pfp_url": "",
    }]
    assert dashboard.merge_farcaster_profiles([], rows)[0]["username"] == "cachedbidder"


def test_current_bid_reward_stats_calculates_payback_daily_roi_and_simple_apr() -> None:
    dashboard = load_module()
    stats = dashboard.current_bid_reward_stats(
        {"amount_wei": "10000000000000000"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0.113508"},
    )
    assert stats["reward_current_bid_payback_days"] == "176.02"
    assert stats["reward_current_bid_daily_roi_pct"] == "0.5681"
    assert stats["reward_current_bid_apr_pct"] == "207.36"
    assert stats["reward_current_bid_apr_display"] == "≈207% APR"


def test_current_bid_reward_stats_unavailable_when_bid_or_daily_flow_missing() -> None:
    dashboard = load_module()
    zero_bid = dashboard.current_bid_reward_stats(
        {"amount_wei": "0"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0.113508"},
    )
    assert zero_bid["reward_current_bid_payback_days"] == "N/A"
    assert zero_bid["reward_current_bid_apr_pct"] == "N/A"
    assert zero_bid["reward_current_bid_apr_display"] == "N/A"

    zero_flow = dashboard.current_bid_reward_stats(
        {"amount_wei": "10000000000000000"},
        {"eth_usd_price": "1998", "reward_total_per_dog_usd_per_day": "0"},
    )
    assert zero_flow["reward_current_bid_payback_days"] == "N/A"
    assert zero_flow["reward_current_bid_apr_pct"] == "N/A"
    assert zero_flow["reward_current_bid_apr_display"] == "N/A"


def test_fetch_current_auction_preserves_exact_coverage_tuple_types() -> None:
    dashboard = load_module()
    token_id = 819
    amount_wei = 5_500_000_000_000_000
    start_time_unix = 1_780_000_000
    end_time_unix = 1_780_003_600
    bidder = int("76d0e7a13248945ee9f808b4a472262b28778942", 16)
    raw = "0x" + "".join(
        f"{value:064x}"
        for value in (
            token_id,
            amount_wei,
            start_time_unix,
            end_time_unix,
            bidder,
            0,
        )
    )
    original = dashboard.eth_call
    dashboard.eth_call = lambda *_args: raw
    try:
        current = dashboard.fetch_current_auction(
            123,
            "2026-08-30 12:34:00",
            "0x7b",
        )
    finally:
        dashboard.eth_call = original

    assert current["amount_wei"] == "5500000000000000"
    assert type(current["start_time_unix"]) is int
    assert current["start_time_unix"] == start_time_unix
    assert type(current["end_time_unix"]) is int
    assert current["end_time_unix"] == end_time_unix


def test_timer_urgency_stays_calm_until_less_than_one_hour_remains() -> None:
    dashboard = load_module()
    assert dashboard.timer_urgency_state(3601, "live") == "calm"
    assert dashboard.timer_urgency_state(3600, "live") == "calm"
    assert dashboard.timer_urgency_state(3599, "live") == "urgent"
    assert dashboard.timer_urgency_state(600, "live") == "critical"
    assert dashboard.timer_urgency_state(0, "live") == "ended"


def run_pricing_sql_fixture(
    dashboard: Any,
    current_eth_usd: str,
    bid_rows: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    conn = sqlite3.connect(":memory:")
    dashboard.insert_rows(conn, "auction_created", [
        {"token_id": 8, "start_time_utc": "2026-06-07 00:00:00", "end_time_utc": "2026-06-08 12:00:00", "block_number": 80, "tx_hash": "0xcreated8"},
        {"token_id": 9, "start_time_utc": "2026-05-31 00:00:00", "end_time_utc": "2026-05-31 12:00:00", "block_number": 90, "tx_hash": "0xcreated9"},
        {"token_id": 10, "start_time_utc": "2026-06-01 00:00:00", "end_time_utc": "2026-06-01 12:00:00", "block_number": 100, "tx_hash": "0xcreated10"},
        {"token_id": 11, "start_time_utc": "2026-06-02 00:00:00", "end_time_utc": "2026-06-03 00:00:00", "block_number": 200, "tx_hash": "0xcreated11"},
    ], [("token_id", "INTEGER"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT")])
    dashboard.insert_rows(conn, "auction_extensions", [
        {"token_id": 8, "end_time_utc": "2026-06-08 12:05:00", "block_number": 81, "tx_hash": "0xextended8a", "log_index": 1},
        {"token_id": 8, "end_time_utc": "2026-06-08 12:10:00", "block_number": 82, "tx_hash": "0xextended8b", "log_index": 2},
    ], [("token_id", "INTEGER"), ("end_time_utc", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER")])
    dashboard.insert_rows(conn, "auction_bids", bid_rows or [
        {"token_id": 9, "bidder": "0x0000000000000000000000000000000000000099", "bid_eth": 0.25, "bid_eth_exact": "0.25", "bid_wei": "250000000000000000", "extended": 0, "block_number": 95, "tx_hash": "0xbid9", "log_index": 0, "block_time_utc": "2026-05-31 19:00:00"},
        {"token_id": 10, "bidder": "0x00000000000000000000000000000000000000a1", "bid_eth": 0.5, "bid_eth_exact": "0.5", "bid_wei": "500000000000000000", "extended": 0, "block_number": 110, "tx_hash": "0xbid10", "log_index": 0, "block_time_utc": "2026-06-01 19:00:00"},
        {"token_id": 11, "bidder": "0x00000000000000000000000000000000000000c3", "bid_eth": 0.75, "bid_eth_exact": "0.75", "bid_wei": "750000000000000000", "extended": 0, "block_number": 205, "tx_hash": "0xbid11early", "log_index": 0, "block_time_utc": "2026-06-02 00:30:00"},
        {"token_id": 11, "bidder": "0x00000000000000000000000000000000000000b2", "bid_eth": 1.0, "bid_eth_exact": "1", "bid_wei": "1000000000000000000", "extended": 0, "block_number": 210, "tx_hash": "0xbid11", "log_index": 0, "block_time_utc": "2026-06-02 01:00:00"},
    ], [("token_id", "INTEGER"), ("bidder", "TEXT"), ("bid_eth", "REAL"), ("bid_eth_exact", "TEXT"), ("bid_wei", "TEXT"), ("extended", "INTEGER"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    dashboard.insert_rows(conn, "auction_settled", [
        {"token_id": 9, "winner": "0x0000000000000000000000000000000000000099", "amount_eth": 0.25, "amount_eth_exact": "0.25", "amount_wei": "250000000000000000", "block_number": 98, "tx_hash": "0xsettled9", "log_index": 0, "block_time_utc": "2026-05-31 19:12:29"},
        {"token_id": 10, "winner": "0x00000000000000000000000000000000000000a1", "amount_eth": 0.5, "amount_eth_exact": "0.5", "amount_wei": "500000000000000000", "block_number": 120, "tx_hash": "0xsettled10", "log_index": 0, "block_time_utc": "2026-06-01 20:00:00"},
    ], [("token_id", "INTEGER"), ("winner", "TEXT"), ("amount_eth", "REAL"), ("amount_eth_exact", "TEXT"), ("amount_wei", "TEXT"), ("block_number", "INTEGER"), ("tx_hash", "TEXT"), ("log_index", "INTEGER"), ("block_time_utc", "TEXT")])
    dashboard.insert_rows(conn, "woof_holders", [], [("address", "TEXT"), ("balance_woof", "REAL"), ("balance_raw", "TEXT")])
    dashboard.insert_rows(conn, "farcaster_profiles", [
        {"address": "0x00000000000000000000000000000000000000b2", "fid": 102, "username": "unitcurrent", "display_name": "Unit Current", "pfp_url": ""},
        {"address": "0x00000000000000000000000000000000000000c3", "fid": 103, "username": "unitearly", "display_name": "Unit Early", "pfp_url": ""},
    ], [("address", "TEXT"), ("fid", "INTEGER"), ("username", "TEXT"), ("display_name", "TEXT"), ("pfp_url", "TEXT")])
    dashboard.insert_rows(conn, "dog_metadata", [
        {"token_id": 9, "dog_name": "Degen Dog #9", "dog_image_url": "", "dog_external_url": "", "dog_opensea_url": "", "traits": "", "trait_rarity": "", "rarity": "", "rarity_score": 0, "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": 10, "dog_name": "Degen Dog #10", "dog_image_url": "", "dog_external_url": "", "dog_opensea_url": "", "traits": "", "trait_rarity": "", "rarity": "", "rarity_score": 0, "metadata_verification_status": "onchain_token_uri_verified"},
        {"token_id": 11, "dog_name": "Degen Dog #11", "dog_image_url": "", "dog_external_url": "", "dog_opensea_url": "", "traits": "", "trait_rarity": "", "rarity": "Unavailable", "rarity_score": None, "metadata_verification_status": "unavailable"},
    ], [("token_id", "INTEGER"), ("dog_name", "TEXT"), ("dog_image_url", "TEXT"), ("dog_external_url", "TEXT"), ("dog_opensea_url", "TEXT"), ("traits", "TEXT"), ("trait_rarity", "TEXT"), ("rarity", "TEXT"), ("rarity_score", "REAL"), ("metadata_verification_status", "TEXT")])
    dashboard.insert_rows(conn, "token_stats", [
        {"metric": "eth_usd_price", "value": current_eth_usd},
        {"metric": "eth_usd_source", "value": "unit_current_price"},
        {"metric": "woof_total_supply", "value": "1"},
        {"metric": "dog_total_supply", "value": "3"},
        {"metric": "dog_token_uri_verification_status", "value": "hash_pinned_cross_provider_exact_outcome_quorum"},
        {"metric": "dog_token_uri_present_count", "value": "3"},
        {"metric": "dog_token_uri_unavailable_count", "value": "0"},
        {"metric": "dog_metadata_verification_status", "value": "incomplete_metadata_unavailable"},
        {"metric": "dog_metadata_onchain_verified_count", "value": "2"},
        {"metric": "dog_metadata_unavailable_count", "value": "1"},
    ], [("metric", "TEXT"), ("value", "TEXT")])
    dashboard.insert_rows(conn, "current_auction_source", [{
        "token_id": 11,
        "amount_eth": 1.0,
        "amount_eth_exact": "1",
        "amount_wei": "1000000000000000000",
        "start_time_utc": "2026-06-02 00:00:00",
        "end_time_utc": "2026-06-03 00:00:00",
        "start_time_unix": 1780358400,
        "end_time_unix": 1780444800,
        "bidder": "0x00000000000000000000000000000000000000b2",
        "settled": 0,
        "latest_block": 220,
        "latest_block_time_utc": "2026-06-02 02:00:00",
    }], [("token_id", "INTEGER"), ("amount_eth", "REAL"), ("amount_eth_exact", "TEXT"), ("amount_wei", "TEXT"), ("start_time_utc", "TEXT"), ("end_time_utc", "TEXT"), ("start_time_unix", "INTEGER"), ("end_time_unix", "INTEGER"), ("bidder", "TEXT"), ("settled", "INTEGER"), ("latest_block", "INTEGER"), ("latest_block_time_utc", "TEXT")])
    dashboard.insert_rows(conn, "historical_prices_daily", [{
        "asset_key": "ETH",
        "date_utc": "2026-06-01",
        "price_usd": "1000",
        "source": "unit_event_price",
        "source_detail": "unit fixture",
        "confidence": "high",
        "timestamp_utc": "2026-06-01T00:00:00Z",
        "notes": "fixture",
    }, {
        "asset_key": "ETH",
        "date_utc": "2026-06-02",
        "price_usd": "9000",
        "source": "unit_next_day_price",
        "source_detail": "unit fixture",
        "confidence": "medium",
        "timestamp_utc": "2026-06-02T00:00:00Z",
        "notes": "fixture that is closer by timestamp but not the event date",
    }], [("asset_key", "TEXT"), ("date_utc", "TEXT"), ("price_usd", "TEXT"), ("source", "TEXT"), ("source_detail", "TEXT"), ("confidence", "TEXT"), ("timestamp_utc", "TEXT"), ("notes", "TEXT")])
    conn.executescript(dashboard.SQL_PATH.read_text(encoding="utf-8"))
    return {
        name: dashboard.table_dicts(*dashboard.fetch_table(conn, name))
        for name in ["recent_bids", "auction_winners", "auction_feed", "current_auction", "current_auction_bid_history", "auction_timeline", "auction_bidder_leaderboard", "mission3_metrics"]
    }


def test_sql_bidder_leaderboard_contains_every_distinct_bidder() -> None:
    dashboard = load_module()
    fixture = run_pricing_sql_fixture(
        dashboard,
        "2000",
        [
            {
                "token_id": index,
                "bidder": f"0x{index:040x}",
                "bid_eth": float(index),
                "bid_eth_exact": str(index),
                "bid_wei": str(index * 10**18),
                "extended": 0,
                "block_number": index,
                "tx_hash": f"0xbid{index}",
                "log_index": 0,
                "block_time_utc": "2026-06-01 19:00:00",
            }
            for index in range(1, 102)
        ],
    )
    rows = fixture["auction_bidder_leaderboard"]

    assert len(rows) == 101
    assert len({row["bidder_wallet"] for row in rows}) == 101
    assert "0x0000000000000000000000000000000000000065" in {
        row["bidder_wallet"] for row in rows
    }


def test_historical_auction_usd_uses_event_day_price_while_live_bid_uses_current_price() -> None:
    dashboard = load_module()
    low_current = run_pricing_sql_fixture(dashboard, "2000")
    high_current = run_pricing_sql_fixture(dashboard, "9000")

    low_winner = low_current["auction_winners"][0]
    high_winner = high_current["auction_winners"][0]
    assert low_winner["winning_bid_usd"] == high_winner["winning_bid_usd"] == 500.0
    assert low_winner["winning_bid_usd_at_settlement"] == 500.0
    assert low_winner["eth_usd_price_at_event"] == "1000"
    assert low_winner["eth_usd_price_date_utc"] == "2026-06-01"
    assert low_winner["usd_estimate_source"] == "unit_event_price"
    assert low_winner["usd_estimate_confidence"] == "high"

    historical_bid = next(row for row in low_current["recent_bids"] if row["token_id"] == 10)
    assert historical_bid["bid_usd"] == 500.0
    assert historical_bid["bid_usd_at_event"] == 500.0
    assert historical_bid["usd_estimate_source"] == "unit_event_price"


def test_metadata_verification_status_reaches_public_sql_outputs() -> None:
    dashboard = load_module()
    fixture = run_pricing_sql_fixture(dashboard, "2000")
    current = fixture["current_auction"][0]
    winner = next(row for row in fixture["auction_winners"] if row["token_id"] == 10)
    timeline = next(row for row in fixture["auction_timeline"] if row["token_id"] == 10)
    current_feed = next(row for row in fixture["auction_feed"] if row["status"] == "ongoing")
    assert current["metadata_verification_status"] == "unavailable"
    assert current["rarity"] == "Unavailable"
    assert current["rarity_score"] is None
    assert current["amount_wei"] == "1000000000000000000"
    assert current["start_time_unix"] == 1780358400
    assert current["end_time_unix"] == 1780444800
    assert winner["metadata_verification_status"] == "onchain_token_uri_verified"
    assert timeline["metadata_verification_status"] == "onchain_token_uri_verified"
    assert current_feed["metadata_verification_status"] == "unavailable"


def test_full_builder_preserves_rarity_score_in_historical_search() -> None:
    dashboard = load_module()
    conn = sqlite3.connect(":memory:")
    for table in ("auction_timeline", "auction_winners", "current_auction"):
        dashboard.insert_rows(conn, table, [], [("token_id", "INTEGER")])
    metadata = [
        {
            "token_id": 0,
            "dog_name": "Degen Dog #0",
            "dog_image_url": "",
            "dog_external_url": "",
            "dog_opensea_url": "",
            "traits": "Background: None",
            "trait_rarity": "Background: None (100.0%)",
            "rarity": "#1/1",
            "rarity_score": 46.059583,
            "metadata_verification_status": "onchain_token_uri_verified",
        },
        {
            "token_id": 1,
            "dog_name": "Degen Dog #1",
            "dog_image_url": "",
            "dog_external_url": "",
            "dog_opensea_url": "",
            "traits": "",
            "trait_rarity": "",
            "rarity": "Unavailable",
            "rarity_score": None,
            "metadata_verification_status": "onchain_token_uri_unavailable",
        },
    ]
    original_loader = dashboard.load_archive_lookup
    try:
        dashboard.load_archive_lookup = lambda: ({}, -1, 0)
        dashboard.build_historical_dog_tables(conn, 2, metadata)
    finally:
        dashboard.load_archive_lookup = original_loader

    columns, values = dashboard.fetch_table(conn, "historical_dog_search")
    rows = dashboard.table_dicts(columns, values)
    by_token = {row["token_id"]: row for row in rows}
    assert "rarity_score" in columns
    assert abs(float(by_token[0]["rarity_score"]) - 46.059583) < 1e-9
    assert by_token[1]["rarity_score"] is None
    conn.close()


def test_token_uri_and_metadata_attestations_reach_mission3_metrics_sql_surface() -> None:
    dashboard = load_module()
    fixture = run_pricing_sql_fixture(dashboard, "2000")
    metrics = {row["metric"]: row["value"] for row in fixture["mission3_metrics"]}
    assert {
        key: metrics[key]
        for key in (
            "dog_token_uri_verification_status",
            "dog_token_uri_present_count",
            "dog_token_uri_unavailable_count",
            "dog_metadata_verification_status",
            "dog_metadata_onchain_verified_count",
            "dog_metadata_unavailable_count",
        )
    } == {
        "dog_token_uri_verification_status": "hash_pinned_cross_provider_exact_outcome_quorum",
        "dog_token_uri_present_count": "3",
        "dog_token_uri_unavailable_count": "0",
        "dog_metadata_verification_status": "incomplete_metadata_unavailable",
        "dog_metadata_onchain_verified_count": "2",
        "dog_metadata_unavailable_count": "1",
    }


def test_auction_extended_uses_verified_abi_and_latest_event_for_timeline_end() -> None:
    dashboard = load_module()
    assert dashboard.TOPIC_AUCTION_EXTENDED == "0x6e912a3a9105bdd2af817ba5adc14e6c127c1035b5b648faa29ca0d58ab8ff4e"
    encoded_end = 1_781_100_600
    decoded = dashboard.decode_auction_extension_logs([{
        "topics": [dashboard.TOPIC_AUCTION_EXTENDED, "0x" + f"{8:064x}"],
        "data": "0x" + f"{encoded_end:064x}",
        "blockNumber": hex(82),
        "transactionHash": "0xextended8b",
        "logIndex": hex(2),
    }])
    assert decoded == [{
        "token_id": 8,
        "end_time_utc": dashboard.utc_from_unix(encoded_end),
        "block_number": 82,
        "tx_hash": "0xextended8b",
        "log_index": 2,
    }]

    fixture = run_pricing_sql_fixture(dashboard, "2000")
    timeline = next(row for row in fixture["auction_timeline"] if row["token_id"] == 8)
    assert timeline["initial_end_time_utc"] == "2026-06-08 12:00:00"
    assert timeline["end_time_utc"] == "2026-06-08 12:10:00"
    assert timeline["extension_count"] == 2
    assert timeline["latest_extension_tx_hash"] == "0xextended8b"


def test_auction_schedule_validator_rejects_getter_log_disagreement() -> None:
    dashboard = load_module()
    created = [{"token_id": 7, "end_time_utc": "2026-06-01 00:00:00", "block_number": 10}]
    extensions = [{"token_id": 7, "end_time_utc": "2026-06-01 00:05:00", "block_number": 11, "log_index": 1}]
    dashboard.validate_auction_schedules(created, extensions, {"token_id": 7, "end_time_utc": "2026-06-01 00:05:00"})
    try:
        dashboard.validate_auction_schedules(created, extensions, {"token_id": 7, "end_time_utc": "2026-06-01 00:04:00"})
    except RuntimeError as exc:
        assert "disagrees" in str(exc)
    else:
        raise AssertionError("expected an auction end-time disagreement")


def test_full_builder_requires_exact_bid_extension_event_pairs() -> None:
    dashboard = load_module()
    bid = {
        "token_id": 7,
        "tx_hash": "0xpaired",
        "block_number": 11,
        "log_index": 4,
        "extended": 1,
    }
    extension = {
        "token_id": 7,
        "tx_hash": "0xpaired",
        "block_number": 11,
        "log_index": 5,
    }
    dashboard.validate_auction_extension_pairs([bid], [extension])
    second_bid = {**bid, "token_id": 8, "log_index": 6}
    second_extension = {**extension, "token_id": 8, "log_index": 7}
    dashboard.validate_auction_extension_pairs(
        [bid, second_bid],
        [extension, second_extension],
    )

    invalid_cases = [
        ([{**bid, "extended": 2}], [extension]),
        ([bid], []),
        ([{**bid, "extended": 0}], [extension]),
        ([bid, dict(bid)], [extension]),
        ([bid], [extension, dict(extension)]),
        ([bid], [{**extension, "token_id": 8}]),
        ([bid], [{**extension, "log_index": 6}]),
    ]
    for bids, extensions in invalid_cases:
        try:
            dashboard.validate_auction_extension_pairs(bids, extensions)
        except RuntimeError:
            pass
        else:
            raise AssertionError("malformed bid/extension pairing was accepted")


def test_exact_wei_survives_decoder_and_published_bid_rows() -> None:
    dashboard = load_module()
    exact_wei = 123_456_789_123_456_789
    bidder_word = int("0000000000000000000000000000000000000088", 16)
    bid_log = {
        "topics": [dashboard.TOPIC_AUCTION_BID, "0x" + f"{8:064x}"],
        "data": "0x" + f"{bidder_word:064x}{exact_wei:064x}{1:064x}",
        "blockNumber": hex(211),
        "blockHash": "0x" + "ab" * 32,
        "transactionHash": "0xexactbid8",
        "logIndex": hex(3),
    }
    old_fetch = dashboard.fetch_block_times
    try:
        dashboard.fetch_block_times = lambda _blocks, _hashes=None: {211: "2026-06-07 01:00:00"}
        _created, bids, _settled = dashboard.decode_auction_logs([], [bid_log], [])
    finally:
        dashboard.fetch_block_times = old_fetch
    assert bids[0]["bid_wei"] == str(exact_wei)
    assert bids[0]["bid_eth_exact"] == "0.123456789123456789"
    dashboard.validate_exact_wei_rows(bids, wei_field="bid_wei", eth_field="bid_eth_exact", label="AuctionBid")

    low_current = run_pricing_sql_fixture(dashboard, "2000")
    high_current = run_pricing_sql_fixture(dashboard, "9000")
    nearest_bid = next(row for row in low_current["recent_bids"] if row["token_id"] == 9)
    assert nearest_bid["bid_usd"] == 250.0
    assert nearest_bid["bid_usd_at_event"] == 250.0
    assert nearest_bid["eth_usd_price_date_utc"] == "2026-06-01"
    assert nearest_bid["usd_estimate_basis"] == "nearest_bid_date_eth_usd"

    nearest_winner = next(row for row in low_current["auction_winners"] if row["token_id"] == 9)
    assert nearest_winner["winning_bid_usd"] == 250.0
    assert nearest_winner["winning_bid_usd_at_settlement"] == 250.0
    assert nearest_winner["eth_usd_price_date_utc"] == "2026-06-01"
    assert nearest_winner["usd_estimate_basis"] == "nearest_settlement_date_eth_usd"

    low_feed_settled = next(row for row in low_current["auction_feed"] if row["status"] == "settled" and row["dog"] == "Dog #10")
    high_feed_settled = next(row for row in high_current["auction_feed"] if row["status"] == "settled" and row["dog"] == "Dog #10")
    assert low_feed_settled["amount_usd"] == high_feed_settled["amount_usd"] == 500.0
    assert low_feed_settled["amount_usd_at_event"] == 500.0
    assert low_feed_settled["eth_usd_price_at_event"] == "1000"

    low_live = next(row for row in low_current["auction_feed"] if row["status"] == "ongoing")
    high_live = next(row for row in high_current["auction_feed"] if row["status"] == "ongoing")
    assert low_live["amount_usd"] == 2000.0
    assert high_live["amount_usd"] == 9000.0
    assert low_live["usd_estimate_source"] == "current_eth_usd_price"

    low_current_row = low_current["current_auction"][0]
    high_current_row = high_current["current_auction"][0]
    low_history_high_bid = low_current["current_auction_bid_history"][0]
    high_history_high_bid = high_current["current_auction_bid_history"][0]
    assert low_current_row["current_bid_usd"] == low_live["amount_usd"] == low_history_high_bid["bid_usd"] == 2000.0
    assert high_current_row["current_bid_usd"] == high_live["amount_usd"] == high_history_high_bid["bid_usd"] == 9000.0
    assert low_history_high_bid["eth_usd_price_live"] == "2000"
    assert high_history_high_bid["eth_usd_price_live"] == "9000"
    assert low_history_high_bid["usd_estimate_source"] == "current_eth_usd_price"
    assert low_history_high_bid["usd_estimate_confidence"] == "live_current"


def test_current_auction_bid_history_archives_all_current_bids_with_live_usd_and_profiles() -> None:
    dashboard = load_module()
    result = run_pricing_sql_fixture(dashboard, "2000")
    history = result["current_auction_bid_history"]

    assert [row["token_id"] for row in history] == [11, 11]
    assert [row["bidder"] for row in history] == ["@unitcurrent", "@unitearly"]
    assert [row["bidder_wallet"] for row in history] == [
        "0x00000000000000000000000000000000000000b2",
        "0x00000000000000000000000000000000000000c3",
    ]
    assert history[0]["bid_eth"] == 1.0
    assert history[0]["bid_usd"] == 2000.0
    assert history[0]["bid"] == "1.00000 ETH ($2000)"
    assert history[1]["bid_eth"] == 0.75
    assert history[1]["bid_usd"] == 1500.0
    assert history[0]["eth_usd_price_live"] == "2000"
    assert history[0]["usd_estimate_basis"] == "current_auction_bid_history_live_eth_usd"
    assert history[0]["tx_hash"] == "0xbid11"
    assert history[1]["tx_hash"] == "0xbid11early"


def test_current_bid_history_renders_top_dropdown_without_bottom_table() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000b2"
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [("site_url", "https://example.test"), ("current_auction_token_id", "11")],
        ),
        "auction_feed": (
            [
                "status",
                "dog",
                "dog_image_url",
                "dog_external_url",
                "dog_opensea_url",
                "bidder_winner",
                "bidder_winner_url",
                "bidder_winner_wallet",
                "bid",
                "amount_eth",
                "amount_usd",
                "time_remaining",
                "auction_end_utc",
                "rarity",
                "traits",
                "trait_rarity",
            ],
            [(
                "ongoing",
                "Dog #11",
                "",
                "",
                "",
                "@unitcurrent",
                "https://farcaster.xyz/unitcurrent",
                wallet,
                "1.00000 ETH ($2000)",
                1.0,
                2000.0,
                "02:00:00",
                "2026-06-02 04:00:00",
                "Rank 1",
                "",
                "",
            )],
        ),
        "current_auction_bid_history": (
            ["bid_time_utc", "token_id", "dog", "bidder", "bidder_url", "bidder_wallet", "bid", "bid_eth", "bid_usd", "block_number", "log_index", "tx_hash"],
            [
                ("2026-06-02 00:30:00", 11, "Dog #11", "@unitearly", "https://farcaster.xyz/unitearly", "0x00000000000000000000000000000000000000c3", "0.75000 ETH ($1500)", 0.75, 1500.0, 205, 0, "0xbid11early"),
                ("2026-06-02 01:00:00", 11, "Dog #11", "@unitcurrent", "https://farcaster.xyz/unitcurrent", wallet, "1.00000 ETH ($2000)", 1.0, 2000.0, 210, 0, "0xbid11"),
            ],
        ),
    }
    menu = dashboard.render_bid_history_menu(tables)
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    assert 'class="bid-history-menu"' in rendered
    assert "Bid history" in rendered
    assert "2 bids" in rendered
    assert "@unitcurrent" in rendered
    assert wallet in rendered
    assert "1.00000 ETH ($2000)" in rendered
    assert menu.index("1.00000 ETH ($2000)") < menu.index("0.75000 ETH ($1500)")
    assert '<span class="bid-history-rank">High bid</span>' in menu
    assert "const bidHistoryHighFirst=" in rendered
    assert "const topBid=bidHistoryHighFirst(history)[0]" in rendered
    assert "Live dashboard refresh failed:" in rendered
    assert rendered.index("detail-bidder") < rendered.index("bid-history-menu")
    assert 'data-table="current_auction_bid_history"' not in rendered
    assert 'data-name="current_auction_bid_history"' not in rendered
    css_markers = [
        ".bid-history-menu{position:relative;align-self:stretch;flex:0 1 158px;min-width:150px;max-width:100%;margin-inline:0",
        ".bid-history-menu summary{list-style:none;cursor:pointer;position:relative;display:flex;min-height:48px;height:100%;flex-direction:column;align-items:center;justify-content:center;text-align:center",
        ".bid-history-list{position:absolute;left:50%;top:calc(100% + 3px);z-index:24;transform:translateX(-50%);width:min(340px,calc(100vw - 24px))",
        "@media (max-width:640px){.bid-history-menu{flex:0 1 150px;min-width:136px}",
        "@media (max-width:380px){.current-detail{display:grid;grid-template-columns:1fr}.current-detail > span,.bid-history-menu{width:100%;max-width:100%}",
    ]
    for marker in css_markers:
        assert marker in rendered


def test_log_chunk_is_capped_for_public_base_rpc() -> None:
    dashboard = load_module()
    assert dashboard.LOG_CHUNK <= 10000


def test_verified_snapshot_requires_hash_agreement_from_independent_providers() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example", "https://three.example"]
    snapshot_hash = "0x" + "cd" * 32
    snapshot_timestamp = int(time.time())
    old_quorum_urls = dashboard._quorum_rpc_urls
    old_rpc_once = dashboard._rpc_once
    old_quorum_size = dashboard.RPC_QUORUM_SIZE
    old_confirmations = dashboard.SNAPSHOT_CONFIRMATIONS
    old_log_urls = list(dashboard.LOG_RPC_URLS)
    old_from_block = dashboard.FROM_BLOCK
    archive_probes: list[str] = []
    verified_log_urls: list[str] = []
    fail_log_urls: set[str] = set()

    def fake_rpc_once(url: str, method: str, params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_blockNumber":
            return {urls[0]: "0x64", urls[1]: "0x63", urls[2]: "0x64"}[url]
        if method == "eth_getBlockByNumber":
            if params[0] == "0xa":
                archive_probes.append(url)
                return {
                    "number": params[0],
                    "hash": "0x" + "ab" * 32,
                    "timestamp": hex(snapshot_timestamp - 1000),
                }
            return {"number": params[0], "hash": snapshot_hash, "timestamp": hex(snapshot_timestamp)}
        if method == "eth_getCode":
            return "0x60016000"
        if method == "eth_getLogs":
            if url in fail_log_urls:
                raise RuntimeError("log capability unavailable")
            if url == urls[-1]:
                # A moderately slower spare is retained inside the bounded
                # post-quorum grace without becoming a hard requirement.
                time.sleep(dashboard.RPC_HEAD_PROBE_GRACE_SECONDS / 2)
            return []
        raise AssertionError(method)

    try:
        dashboard._quorum_rpc_urls = lambda: urls
        dashboard._rpc_once = fake_rpc_once
        dashboard.RPC_QUORUM_SIZE = 2
        dashboard.SNAPSHOT_CONFIRMATIONS = 1
        dashboard.LOG_RPC_URLS = urls
        dashboard.FROM_BLOCK = 10
        block, block_data, verification = dashboard.verified_snapshot()
        verified_log_urls = list(dashboard.VERIFIED_LOG_URLS)

        fail_log_urls.update(urls[1:])
        dashboard.RPC_SLOW_UNTIL.clear()
        try:
            dashboard.verified_snapshot()
        except RuntimeError as exc:
            assert "Base RPC log quorum unavailable: healthy=1 required=2" in str(exc)
        else:
            raise AssertionError("one healthy log provider satisfied the required two-provider quorum")
    finally:
        dashboard._quorum_rpc_urls = old_quorum_urls
        dashboard._rpc_once = old_rpc_once
        dashboard.RPC_QUORUM_SIZE = old_quorum_size
        dashboard.SNAPSHOT_CONFIRMATIONS = old_confirmations
        dashboard.LOG_RPC_URLS = old_log_urls
        dashboard.FROM_BLOCK = old_from_block
        dashboard.VERIFIED_SNAPSHOT_URLS = []
        dashboard.VERIFIED_LOG_URLS = []

    assert block == 99
    assert block_data["hash"] == snapshot_hash
    assert verification["onchain_verification_status"] == "current_snapshot_cross_provider_verified"
    assert "current_auction" in verification["onchain_verification_scope"]
    assert verification["onchain_chain_id"] == "8453"
    assert verification["rpc_quorum_size"] == "2"
    assert verification["snapshot_block_hash"] == snapshot_hash
    assert len(verification["log_rpc_quorum_providers"].split(",")) >= 2
    assert set(verified_log_urls) == set(urls)
    assert len(set(archive_probes)) >= 2


def test_snapshot_qualification_retains_all_exact_candidates_and_survives_one_failure() -> None:
    dashboard = load_module()
    urls = [f"https://provider-{index}.example/rpc" for index in range(5)]
    seed_urls = urls[:2]
    snapshot_hash = "0x" + "44" * 32
    snapshot_timestamp = int(time.time())
    auction_code = "0x60016000"
    dog_code = "0x60026000"
    old_quorum_urls = dashboard._quorum_rpc_urls
    old_rpc_quorum = dashboard.rpc_quorum
    old_rpc_once_with_retry = dashboard._rpc_once_with_retry
    old_quorum_size = dashboard.RPC_QUORUM_SIZE
    old_confirmations = dashboard.SNAPSHOT_CONFIRMATIONS
    old_log_urls = list(dashboard.LOG_RPC_URLS)

    def seed_quorum(method: str, params: list[Any], **_kwargs: Any) -> tuple[Any, list[str]]:
        if method == "eth_getBlockByNumber":
            return {
                "number": "0x63",
                "hash": snapshot_hash,
                "timestamp": hex(snapshot_timestamp),
            }, seed_urls
        if method == "eth_getCode":
            return (auction_code if params[0] == dashboard.AUCTION_HOUSE else dog_code), seed_urls
        raise AssertionError(method)

    def qualification_rpc(
        url: str,
        method: str,
        params: list[Any],
        *,
        timeout: int = 30,
    ) -> Any:  # noqa: ARG001
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "hash": snapshot_hash, "timestamp": hex(snapshot_timestamp)}
        if method == "eth_getCode":
            expected = auction_code if params[0] == dashboard.AUCTION_HOUSE else dog_code
            return "0xdeadbeef" if url == urls[-1] else expected
        if method == "eth_getLogs":
            return []
        raise AssertionError(method)

    try:
        dashboard._quorum_rpc_urls = lambda: urls
        dashboard.rpc_quorum = seed_quorum
        dashboard._rpc_once_with_retry = qualification_rpc
        dashboard.RPC_QUORUM_SIZE = 2
        dashboard.SNAPSHOT_CONFIRMATIONS = 1
        dashboard.LOG_RPC_URLS = urls
        _block, _block_data, verification = dashboard.verified_snapshot()

        assert set(dashboard.VERIFIED_SNAPSHOT_URLS) == set(urls[:4])
        assert verification["rpc_quorum_agreement"] == "4/5"
        assert "https://" not in verification["rpc_quorum_providers"]
        assert "/rpc" not in verification["rpc_quorum_providers"]

        dashboard.rpc_quorum = old_rpc_quorum

        def one_provider_down(
            url: str,
            method: str,
            params: list[Any],
            *,
            timeout: int = 30,
        ) -> Any:  # noqa: ARG001
            if method == "eth_call":
                if url == urls[0]:
                    raise RuntimeError("HTTP 500")
                return "0x1234"
            return qualification_rpc(url, method, params, timeout=timeout)

        dashboard._rpc_once_with_retry = one_provider_down
        value, agreeing = dashboard.rpc_quorum(
            "eth_call",
            [{"to": dashboard.DEGEN_DOGS, "data": dashboard.SELECTOR_TOTAL_SUPPLY}, "0x63"],
            urls=dashboard.VERIFIED_SNAPSHOT_URLS,
            min_agreement=2,
            timeout=1,
        )
        assert value == "0x1234"
        assert len(agreeing) >= 2
        assert urls[0] not in agreeing
    finally:
        dashboard._quorum_rpc_urls = old_quorum_urls
        dashboard.rpc_quorum = old_rpc_quorum
        dashboard._rpc_once_with_retry = old_rpc_once_with_retry
        dashboard.RPC_QUORUM_SIZE = old_quorum_size
        dashboard.SNAPSHOT_CONFIRMATIONS = old_confirmations
        dashboard.LOG_RPC_URLS = old_log_urls
        dashboard.VERIFIED_SNAPSHOT_URLS = []
        dashboard.VERIFIED_LOG_URLS = []
        dashboard.RPC_SLOW_UNTIL.clear()


def test_verified_snapshot_rejects_stale_split_heads() -> None:
    dashboard = load_module()
    urls = ["https://fresh.example", "https://stale.example"]
    old_quorum_urls = dashboard._quorum_rpc_urls
    old_rpc_once = dashboard._rpc_once
    old_quorum_size = dashboard.RPC_QUORUM_SIZE
    old_spread = dashboard.RPC_MAX_HEAD_SPREAD_BLOCKS

    def fake_rpc_once(url: str, method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_blockNumber":
            return "0x3e8" if url == urls[0] else "0x64"
        raise AssertionError(f"unexpected request after split-head detection: {method}")

    try:
        dashboard._quorum_rpc_urls = lambda: urls
        dashboard._rpc_once = fake_rpc_once
        dashboard.RPC_QUORUM_SIZE = 2
        dashboard.RPC_MAX_HEAD_SPREAD_BLOCKS = 20
        try:
            dashboard.verified_snapshot()
        except RuntimeError as exc:
            assert "cannot form a recent quorum" in str(exc)
        else:
            raise AssertionError("split fresh/stale RPC heads were accepted")
    finally:
        dashboard._quorum_rpc_urls = old_quorum_urls
        dashboard._rpc_once = old_rpc_once
        dashboard.RPC_QUORUM_SIZE = old_quorum_size
        dashboard.RPC_MAX_HEAD_SPREAD_BLOCKS = old_spread
        dashboard.VERIFIED_SNAPSHOT_URLS = []
        dashboard.VERIFIED_LOG_URLS = []


def test_verified_snapshot_rejects_old_block_timestamp() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example"]
    old_quorum_urls = dashboard._quorum_rpc_urls
    old_rpc_once = dashboard._rpc_once
    old_quorum_size = dashboard.RPC_QUORUM_SIZE
    old_max_age = dashboard.RPC_MAX_BLOCK_AGE_SECONDS

    def fake_rpc_once(_url: str, method: str, params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_blockNumber":
            return "0x64"
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "hash": "0xcanonical", "timestamp": hex(int(time.time()) - 3600)}
        raise AssertionError(method)

    try:
        dashboard._quorum_rpc_urls = lambda: urls
        dashboard._rpc_once = fake_rpc_once
        dashboard.RPC_QUORUM_SIZE = 2
        dashboard.RPC_MAX_BLOCK_AGE_SECONDS = 600
        try:
            dashboard.verified_snapshot()
        except RuntimeError as exc:
            assert "outside the freshness window" in str(exc)
        else:
            raise AssertionError("stale snapshot timestamp was accepted")
    finally:
        dashboard._quorum_rpc_urls = old_quorum_urls
        dashboard._rpc_once = old_rpc_once
        dashboard.RPC_QUORUM_SIZE = old_quorum_size
        dashboard.RPC_MAX_BLOCK_AGE_SECONDS = old_max_age
        dashboard.VERIFIED_SNAPSHOT_URLS = []
        dashboard.VERIFIED_LOG_URLS = []


def test_rpc_quorum_rejects_disagreement() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        return {"https://one.example": "0x1", "https://two.example": "0x2"}[url]

    try:
        dashboard._rpc_once = fake_rpc_once
        try:
            dashboard.rpc_quorum(
                "eth_call",
                [],
                urls=["https://one.example", "https://two.example"],
                min_agreement=2,
            )
        except RuntimeError as exc:
            assert "quorum disagreement" in str(exc)
        else:
            raise AssertionError("RPC quorum accepted conflicting provider results")
    finally:
        dashboard._rpc_once = old_rpc_once


def test_log_quorum_canonicalizes_and_enforces_transaction_index() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example"]
    canonical = {
        "address": "0x" + "a" * 40,
        "blockHash": "0x" + "b" * 64,
        "blockNumber": "0x64",
        "data": "0x",
        "logIndex": "0x0",
        "removed": False,
        "topics": ["0x" + "c" * 64],
        "transactionHash": "0x" + "d" * 64,
        "transactionIndex": "0xA",
    }
    case_variant = {**canonical, "transactionIndex": "0xa"}
    conflicting = {**canonical, "transactionIndex": "0xb"}
    assert dashboard._canonical_rpc_result("eth_getLogs", [canonical]) == dashboard._canonical_rpc_result(
        "eth_getLogs", [case_variant]
    )
    assert dashboard._canonical_rpc_result("eth_getLogs", [canonical]) != dashboard._canonical_rpc_result(
        "eth_getLogs", [conflicting]
    )
    missing_removed = dict(canonical)
    missing_removed.pop("removed")
    assert dashboard._canonical_rpc_result(
        "eth_getLogs", [canonical]
    ) != dashboard._canonical_rpc_result("eth_getLogs", [missing_removed])

    old_rpc_once = dashboard._rpc_once
    answers = {urls[0]: [canonical], urls[1]: [conflicting]}

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        return answers[url]

    try:
        dashboard._rpc_once = fake_rpc_once
        try:
            dashboard.rpc_quorum("eth_getLogs", [{}], urls=urls, min_agreement=2)
        except RuntimeError as exc:
            assert "quorum disagreement" in str(exc)
        else:
            raise AssertionError("log quorum accepted conflicting transaction indexes")
    finally:
        dashboard._rpc_once = old_rpc_once


def test_log_quorum_never_selects_missing_removed_by_response_order() -> None:
    dashboard = load_module()
    address = "0x" + "a" * 40
    topic = dashboard.TOPIC_AUCTION_CREATED
    explicit = canonical_log(address, topic, 100)
    missing = dict(explicit)
    missing.pop("removed")
    urls = [
        "https://missing.example",
        "https://valid-one.example",
        "https://valid-two.example",
    ]
    old_call = dashboard._rpc_once_with_retry
    try:
        for missing_delay in (0.0, 0.03):
            dashboard.RPC_SLOW_UNTIL.clear()
            delays = {
                urls[0]: missing_delay,
                urls[1]: 0.01 if missing_delay == 0 else 0.0,
                urls[2]: 0.02 if missing_delay == 0 else 0.0,
            }

            def fake_call(
                url: str,
                _method: str,
                _params: list[Any],
                *,
                timeout: int = 30,  # noqa: ARG001
            ) -> Any:
                time.sleep(delays[url])
                return [missing] if url == urls[0] else [explicit]

            dashboard._rpc_once_with_retry = fake_call
            result, providers = dashboard.rpc_quorum(
                "eth_getLogs", [{}], urls=urls, min_agreement=2
            )
            assert result == [explicit]
            assert len(providers) == 2
            validated, _position = dashboard._validated_quorum_log_chunk(
                result,
                address=address,
                topics=topic,
                from_block=100,
                to_block=100,
                seen_identities=set(),
                previous_position=None,
                block_hashes={},
            )
            assert validated == [explicit]
    finally:
        dashboard._rpc_once_with_retry = old_call
        dashboard.RPC_SLOW_UNTIL.clear()


def test_explicit_log_range_classifier_is_narrow() -> None:
    dashboard = load_module()
    assert dashboard.is_explicit_log_range_error(-32005, "maximum block range is 250") is True
    assert dashboard.is_explicit_log_range_error(-32000, "too many results; please limit the query") is True
    assert dashboard.is_explicit_log_range_error(-32000, "upstream timeout") is False
    assert dashboard.is_explicit_log_range_error(-32000, "rate limit exceeded") is False
    assert dashboard.is_explicit_log_range_error(413, "response size") is False


def test_rpc_circuit_breaker_keeps_a_spare_provider_for_later_quorums() -> None:
    dashboard = load_module()
    urls = [f"https://provider-{index}.example" for index in range(4)]
    old_rpc_once_with_retry = dashboard._rpc_once_with_retry
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    attempted: list[str] = []
    future = time.monotonic() + 60

    try:
        dashboard.RPC_SLOW_UNTIL.clear()
        dashboard.RPC_SLOW_UNTIL[("eth_call", urls[3])] = future
        assert dashboard._responsive_rpc_urls(urls, 2, "eth_call") == urls[:3]

        # Repeated earlier quorum wins may have left two different responders
        # pending. Filtering both would leave a brittle bare 2-of-2 pool, so
        # the breaker must reintroduce all four qualified candidates.
        dashboard.RPC_SLOW_UNTIL[("eth_call", urls[2])] = future
        assert dashboard._responsive_rpc_urls(urls, 2, "eth_call") == urls

        def one_active_failure(
            url: str,
            _method: str,
            _params: list[Any],
            *,
            timeout: int = 30,
        ) -> Any:  # noqa: ARG001
            attempted.append(url)
            if url == urls[0]:
                raise RuntimeError("HTTP 429")
            return "0x42"

        dashboard._rpc_once_with_retry = one_active_failure
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 1
        value, agreeing = dashboard.rpc_quorum(
            "eth_call",
            [],
            urls=urls,
            min_agreement=2,
            timeout=1,
        )
    finally:
        dashboard._rpc_once_with_retry = old_rpc_once_with_retry
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert value == "0x42"
    assert len(agreeing) >= 2
    assert urls[0] not in agreeing
    assert set(attempted) == set(urls)


def test_rpc_batch_quorum_rejects_provider_disagreement() -> None:
    dashboard = load_module()
    old_rpc_batch = dashboard.rpc_batch
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    urls = ["https://one.example", "https://two.example"]

    def fake_batch(_calls: list[tuple[str, list[Any]]], timeout: int = 120, urls: list[str] | None = None) -> list[Any]:  # noqa: ARG001
        assert urls and len(urls) == 1
        return ["0x1" if urls[0] == "https://one.example" else "0x2"]

    try:
        dashboard.rpc_batch = fake_batch
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 1
        try:
            dashboard.rpc_batch_quorum(
                [("eth_call", [])],
                urls=urls,
                min_agreement=2,
                timeout=1,
            )
        except RuntimeError as exc:
            message = str(exc)
            assert "batch quorum disagreement" in message
            one = dashboard._rpc_provider_key("https://one.example")
            two = dashboard._rpc_provider_key("https://two.example")
            assert "provider_groups=" in message
            assert one in message
            assert two in message
            assert "https://" not in message
        else:
            raise AssertionError("conflicting JSON-RPC batch results reached quorum")
    finally:
        dashboard.rpc_batch = old_rpc_batch
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()


def test_block_batch_quorum_normalizes_hex_quantity_formatting_only() -> None:
    dashboard = load_module()
    old_rpc_batch = dashboard.rpc_batch
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    urls = ["https://one.example", "https://two.example"]
    block_hash = "0x" + "ab" * 32

    def fake_batch(
        _calls: list[tuple[str, list[Any]]],
        timeout: int = 120,
        urls: list[str] | None = None,
    ) -> list[Any]:  # noqa: ARG001
        assert urls and len(urls) == 1
        if urls[0] == "https://one.example":
            return [{"number": "0X00064", "hash": block_hash.upper(), "timestamp": "0x0000000A"}]
        return [{"number": "0x64", "hash": block_hash, "timestamp": "0Xa"}]

    try:
        dashboard.rpc_batch = fake_batch
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 1
        result = dashboard.rpc_batch_quorum(
            [("eth_getBlockByNumber", ["0x64", False])],
            urls=urls,
            min_agreement=2,
            timeout=1,
        )
    finally:
        dashboard.rpc_batch = old_rpc_batch
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert len(result) == 1
    assert int(result[0]["number"], 16) == 100
    assert int(result[0]["timestamp"], 16) == 10
    assert result[0]["hash"].lower() == block_hash


def test_token_uri_quorum_gives_each_queued_chunk_a_fresh_deadline() -> None:
    dashboard = load_module()
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_post_json = dashboard.post_json
    old_batch_limit = dashboard.RPC_BATCH_LIMIT
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    old_workers = dashboard.TOKEN_URI_CHUNK_WORKERS
    old_chunk_delay = dashboard.TOKEN_URI_CHUNK_DELAY_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example"]
    calls: list[tuple[str, int]] = []
    block_hash = "0x" + ("11" * 32)

    def encode_abi_string(value: str) -> str:
        encoded = value.encode("utf-8")
        padding = (-len(encoded)) % 32
        return "0x" + (
            (32).to_bytes(32, "big")
            + len(encoded).to_bytes(32, "big")
            + encoded
            + (b"\x00" * padding)
        ).hex()

    def fake_post_json(payload: Any, timeout: int, url: str) -> list[dict[str, Any]]:  # noqa: ARG001
        assert isinstance(payload, list) and len(payload) == 2
        time.sleep(0.18)
        response: list[dict[str, Any]] = []
        for item in payload:
            assert item["method"] == "eth_call"
            assert item["params"][1] == {"blockHash": block_hash, "requireCanonical": True}
            call_data = str(item["params"][0]["data"])
            token_id = int(call_data[-64:], 16)
            if call_data.startswith(dashboard.SELECTOR_EXISTS):
                result = "0x" + f"{1:064x}"
            else:
                calls.append((url, token_id))
                result = encode_abi_string(f"https://ipfs.io/ipfs/dog-{token_id}")
            response.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
        return response

    try:
        dashboard.VERIFIED_SNAPSHOT_URLS = urls
        dashboard.post_json = fake_post_json
        dashboard.RPC_BATCH_LIMIT = 1
        dashboard.TOKEN_URI_CHUNK_WORKERS = 1
        dashboard.TOKEN_URI_CHUNK_DELAY_SECONDS = 0
        # Token 1 waits behind token 0. Its work finishes after this duration
        # measured from the collection start, but
        # within a fresh duration measured from that chunk's actual start.
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 0.3
        started = time.monotonic()
        bindings = dashboard.fetch_token_uri_bindings([0, 1], "0x64", block_hash=block_hash)
        elapsed = time.monotonic() - started
    finally:
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls
        dashboard.post_json = old_post_json
        dashboard.RPC_BATCH_LIMIT = old_batch_limit
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.TOKEN_URI_CHUNK_WORKERS = old_workers
        dashboard.TOKEN_URI_CHUNK_DELAY_SECONDS = old_chunk_delay
        dashboard.RPC_SLOW_UNTIL.clear()

    assert bindings == {token_id: f"https://ipfs.io/ipfs/dog-{token_id}" for token_id in range(2)}
    assert set(calls) == {(url, token_id) for url in urls for token_id in range(2)}
    assert elapsed > 0.3
    assert elapsed < 0.8


def test_token_uri_quorum_accepts_mixed_exact_uri_and_nonexistent_token_outcomes() -> None:
    dashboard = load_module()
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_post_json = dashboard.post_json
    urls = ["https://publicnode.example", "https://tenderly.example"]
    block_hash = "0x" + ("22" * 32)
    observed_tags: list[Any] = []

    def encode_abi_string(value: str) -> str:
        encoded = value.encode("utf-8")
        padding = (-len(encoded)) % 32
        return "0x" + (
            (32).to_bytes(32, "big")
            + len(encoded).to_bytes(32, "big")
            + encoded
            + (b"\x00" * padding)
        ).hex()

    def fake_post_json(payload: Any, timeout: int, url: str) -> list[dict[str, Any]]:  # noqa: ARG001
        assert url in urls and isinstance(payload, list)
        response: list[dict[str, Any]] = []
        for item in payload:
            observed_tags.append(item["params"][1])
            call_data = str(item["params"][0]["data"])
            token_id = int(call_data[-64:], 16)
            if call_data.startswith(dashboard.SELECTOR_EXISTS):
                response.append({
                    "jsonrpc": "2.0",
                    "id": item["id"],
                    "result": "0x" + f"{int(token_id != 2):064x}",
                })
            elif token_id == 2:
                response.append({
                    "jsonrpc": "2.0",
                    "id": item["id"],
                    "error": {
                        "code": 3,
                        "message": "execution reverted",
                        "data": dashboard.ERC721_NONEXISTENT_TOKEN_ERROR + f"{token_id:x}".rjust(64, "0"),
                    },
                })
            else:
                response.append({
                    "jsonrpc": "2.0",
                    "id": item["id"],
                    "result": encode_abi_string(f"https://degendogs.club/meta/{token_id}"),
                })
        return list(reversed(response)) if url == urls[1] else response

    try:
        dashboard.VERIFIED_SNAPSHOT_URLS = urls
        dashboard.post_json = fake_post_json
        bindings = dashboard.fetch_token_uri_bindings([0, 2], "0x64", block_hash=block_hash)
    finally:
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls
        dashboard.post_json = old_post_json
        dashboard.RPC_SLOW_UNTIL.clear()

    assert bindings == {0: "https://degendogs.club/meta/0", 2: None}
    assert observed_tags == [{"blockHash": block_hash, "requireCanonical": True}] * 8


def test_token_uri_chunk_quorum_fails_closed_on_provider_disagreement() -> None:
    dashboard = load_module()
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_post_json = dashboard.post_json
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    urls = ["https://one.example", "https://two.example"]

    def encode_abi_string(value: str) -> str:
        encoded = value.encode("utf-8")
        padding = (-len(encoded)) % 32
        return "0x" + (
            (32).to_bytes(32, "big")
            + len(encoded).to_bytes(32, "big")
            + encoded
            + (b"\x00" * padding)
        ).hex()

    def fake_post_json(payload: Any, timeout: int, url: str) -> list[dict[str, Any]]:  # noqa: ARG001
        assert isinstance(payload, list) and len(payload) == 2
        response: list[dict[str, Any]] = []
        for item in payload:
            call_data = str(item["params"][0]["data"])
            result = (
                "0x" + f"{1:064x}"
                if call_data.startswith(dashboard.SELECTOR_EXISTS)
                else encode_abi_string(f"https://ipfs.io/ipfs/{url}")
            )
            response.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
        return response

    try:
        dashboard.VERIFIED_SNAPSHOT_URLS = urls
        dashboard.post_json = fake_post_json
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 0.5
        try:
            dashboard.fetch_token_uri_bindings([0], "0x64")
        except RuntimeError as exc:
            assert "quorum disagreement" in str(exc)
        else:
            raise AssertionError("conflicting tokenURI provider results reached the dashboard")
    finally:
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls
        dashboard.post_json = old_post_json
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()


def test_token_uri_quorum_rejects_exists_outcome_mismatch() -> None:
    dashboard = load_module()
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_post_json = dashboard.post_json
    old_attempts = dashboard.RPC_ATTEMPTS
    urls = ["https://one.example", "https://two.example"]

    def encode_abi_string(value: str) -> str:
        encoded = value.encode("utf-8")
        padding = (-len(encoded)) % 32
        return "0x" + (
            (32).to_bytes(32, "big")
            + len(encoded).to_bytes(32, "big")
            + encoded
            + (b"\x00" * padding)
        ).hex()

    def fake_post_json(payload: Any, timeout: int, url: str) -> list[dict[str, Any]]:  # noqa: ARG001
        response = []
        for item in payload:
            call_data = str(item["params"][0]["data"])
            result = (
                "0x" + f"{0:064x}"
                if call_data.startswith(dashboard.SELECTOR_EXISTS)
                else encode_abi_string("https://degendogs.club/meta/0")
            )
            response.append({"jsonrpc": "2.0", "id": item["id"], "result": result})
        return response

    try:
        dashboard.VERIFIED_SNAPSHOT_URLS = urls
        dashboard.post_json = fake_post_json
        dashboard.RPC_ATTEMPTS = 1
        try:
            dashboard.fetch_token_uri_bindings([0], "0x64")
        except RuntimeError as exc:
            assert "exists()" in str(exc)
        else:
            raise AssertionError("exists()/tokenURI mismatch reached rarity metadata")
    finally:
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls
        dashboard.post_json = old_post_json
        dashboard.RPC_ATTEMPTS = old_attempts
        dashboard.RPC_SLOW_UNTIL.clear()


def test_all_hash_pinned_token_state_and_holder_balances_use_quorum() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example"]
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_rpc_quorum = dashboard.rpc_quorum
    old_batch_quorum = dashboard.rpc_batch_quorum
    single_calls: list[tuple[str, list[Any]]] = []
    batch_calls: list[list[tuple[str, list[Any]]]] = []

    def fake_quorum(method: str, params: list[Any], **_kwargs: Any) -> tuple[str, list[str]]:
        single_calls.append((method, params))
        return "0x" + "00" * 31 + "01", urls

    def fake_batch_quorum(calls: list[tuple[str, list[Any]]], **_kwargs: Any) -> list[Any]:
        batch_calls.append(calls)
        return ["0x" + "00" * 31 + "2a" for _call in calls]

    try:
        dashboard.VERIFIED_SNAPSHOT_URLS = urls
        dashboard.rpc_quorum = fake_quorum
        dashboard.rpc_batch_quorum = fake_batch_quorum
        assert dashboard.eth_call(dashboard.SUP, dashboard.SELECTOR_SYMBOL, "0x64").endswith("01")
        address = "0x00000000000000000000000000000000000000a1"
        assert dashboard.fetch_balances([address], "0x64") == {address: 42}
    finally:
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls
        dashboard.rpc_quorum = old_rpc_quorum
        dashboard.rpc_batch_quorum = old_batch_quorum

    assert single_calls and single_calls[0][0] == "eth_call"
    assert batch_calls and batch_calls[0][0][0] == "eth_call"


def test_block_time_cache_is_hash_bound_and_lookups_are_quorum_checked() -> None:
    dashboard = load_module()
    old_cache_path = dashboard.BLOCK_TIME_CACHE
    old_snapshot_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_log_urls = list(dashboard.VERIFIED_LOG_URLS)
    old_batch_quorum = dashboard.rpc_batch_quorum
    canonical_hash = "0x" + "22" * 32
    stale_hash = "0x" + "11" * 32
    calls: list[list[tuple[str, list[Any]]]] = []
    quorum_urls: list[list[str]] = []

    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.BLOCK_TIME_CACHE = Path(tmp) / "block_times.json"
            dashboard.BLOCK_TIME_CACHE.write_text(
                json.dumps({
                    "schema_version": 2,
                    "blocks": {
                        "100": {
                            "block_hash": stale_hash,
                            "timestamp_utc": "2020-01-01 00:00:00",
                        }
                    },
                }),
                encoding="utf-8",
            )
            dashboard.VERIFIED_SNAPSHOT_URLS = ["https://hot-one.example", "https://hot-two.example"]
            dashboard.VERIFIED_LOG_URLS = ["https://archive-one.example", "https://archive-two.example"]

            def fake_batch_quorum(batch: list[tuple[str, list[Any]]], **kwargs: Any) -> list[Any]:
                calls.append(batch)
                quorum_urls.append(list(kwargs["urls"]))
                return [
                    {
                        "number": params[0],
                        "hash": canonical_hash,
                        "timestamp": hex(1_700_000_000),
                    }
                    for _method, params in batch
                ]

            dashboard.rpc_batch_quorum = fake_batch_quorum
            result = dashboard.fetch_block_times({100}, {100: canonical_hash})
            assert result[100] == dashboard.utc_from_unix(1_700_000_000)
            stored = json.loads(dashboard.BLOCK_TIME_CACHE.read_text(encoding="utf-8"))
            assert stored["blocks"]["100"]["block_hash"] == canonical_hash
        finally:
            dashboard.BLOCK_TIME_CACHE = old_cache_path
            dashboard.VERIFIED_SNAPSHOT_URLS = old_snapshot_urls
            dashboard.VERIFIED_LOG_URLS = old_log_urls
            dashboard.rpc_batch_quorum = old_batch_quorum

    assert len(calls) == 1
    assert quorum_urls == [["https://archive-one.example", "https://archive-two.example"]]


def test_block_time_lookup_checkpoints_each_fresh_archive_quorum_chunk() -> None:
    dashboard = load_module()
    old_cache_path = dashboard.BLOCK_TIME_CACHE
    old_snapshot_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)
    old_log_urls = list(dashboard.VERIFIED_LOG_URLS)
    old_batch_quorum = dashboard.rpc_batch_quorum
    old_batch_limit = dashboard.RPC_BATCH_LIMIT
    hashes = {block: "0x" + f"{block:064x}" for block in (100, 101, 102)}
    calls: list[list[tuple[str, list[Any]]]] = []

    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.BLOCK_TIME_CACHE = Path(tmp) / "block_times.json"
            dashboard.VERIFIED_SNAPSHOT_URLS = ["https://hot-one.example", "https://hot-two.example"]
            dashboard.VERIFIED_LOG_URLS = ["https://archive-one.example", "https://archive-two.example"]
            dashboard.RPC_BATCH_LIMIT = 2

            def fake_batch_quorum(batch: list[tuple[str, list[Any]]], **kwargs: Any) -> list[Any]:
                assert kwargs["urls"] == dashboard.VERIFIED_LOG_URLS
                calls.append(batch)
                if len(calls) == 2:
                    raise RuntimeError("late archive outage")
                return [
                    {
                        "number": params[0],
                        "hash": hashes[int(params[0], 16)],
                        "timestamp": hex(1_700_000_000 + int(params[0], 16)),
                    }
                    for _method, params in batch
                ]

            dashboard.rpc_batch_quorum = fake_batch_quorum
            try:
                dashboard.fetch_block_times(set(hashes), hashes)
            except RuntimeError as exc:
                assert "late archive outage" in str(exc)
            else:
                raise AssertionError("late archive outage did not fail closed")

            stored = json.loads(dashboard.BLOCK_TIME_CACHE.read_text(encoding="utf-8"))
            assert set(stored["blocks"]) == {"100", "101"}
        finally:
            dashboard.BLOCK_TIME_CACHE = old_cache_path
            dashboard.VERIFIED_SNAPSHOT_URLS = old_snapshot_urls
            dashboard.VERIFIED_LOG_URLS = old_log_urls
            dashboard.rpc_batch_quorum = old_batch_quorum
            dashboard.RPC_BATCH_LIMIT = old_batch_limit

    assert [len(batch) for batch in calls] == [2, 1]


def test_auction_event_timestamp_binding_rejects_conflicting_hashes_at_one_height() -> None:
    dashboard = load_module()
    block = 211
    template = {
        "topics": [dashboard.TOPIC_AUCTION_BID, "0x" + f"{8:064x}"],
        "data": "0x" + f"{1:064x}{1:064x}{0:064x}",
        "blockNumber": hex(block),
        "transactionHash": "0x" + "01" * 32,
        "logIndex": "0x0",
    }
    first = {**template, "blockHash": "0x" + "11" * 32}
    second = {
        **template,
        "blockHash": "0x" + "22" * 32,
        "transactionHash": "0x" + "02" * 32,
        "logIndex": "0x1",
    }
    try:
        dashboard.decode_auction_logs([], [first, second], [])
    except RuntimeError as exc:
        assert "disagree" in str(exc) and str(block) in str(exc)
    else:
        raise AssertionError("conflicting event block hashes were accepted")


def test_rpc_quorum_rejects_two_by_two_tie() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    answers = {
        "https://one.example": "0x1",
        "https://two.example": "0x1",
        "https://three.example": "0x2",
        "https://four.example": "0x2",
    }

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        return answers[url]

    try:
        dashboard._rpc_once = fake_rpc_once
        try:
            dashboard.rpc_quorum("eth_call", [], urls=list(answers), min_agreement=2)
        except RuntimeError as exc:
            assert "votes=[2, 2]" in str(exc)
        else:
            raise AssertionError("RPC quorum accepted a 2-2 provider tie")
    finally:
        dashboard._rpc_once = old_rpc_once


def test_rpc_quorum_returns_without_waiting_for_decisive_straggler() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]

    def fake_rpc_once(url: str, _method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        if url == urls[-1]:
            time.sleep(0.6)
        return "0xcanonical"

    try:
        dashboard._rpc_once = fake_rpc_once
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 1.0
        started = time.monotonic()
        value, agreeing = dashboard.rpc_quorum("eth_call", [], urls=urls, min_agreement=2)
        elapsed = time.monotonic() - started
    finally:
        dashboard._rpc_once = old_rpc_once
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert value == "0xcanonical"
    assert len(agreeing) == 2
    assert elapsed < 0.25


def test_head_probe_returns_after_minimum_quorum_and_grace() -> None:
    dashboard = load_module()
    old_grace = dashboard.RPC_HEAD_PROBE_GRACE_SECONDS
    old_deadline = dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://slow.example"]

    def probe(url: str) -> tuple[str, int]:
        if url == urls[-1]:
            time.sleep(0.6)
        return url, 100

    try:
        dashboard.RPC_HEAD_PROBE_GRACE_SECONDS = 0.0
        dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS = 1.0
        started = time.monotonic()
        results, _errors = dashboard._collect_rpc_probes(urls, required=2, probe=probe, label="test-head")
        elapsed = time.monotonic() - started
    finally:
        dashboard.RPC_HEAD_PROBE_GRACE_SECONDS = old_grace
        dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert len(results) == 2
    assert elapsed < 0.25


def test_preferred_probe_spare_never_forces_the_hard_deadline() -> None:
    dashboard = load_module()
    old_grace = dashboard.RPC_HEAD_PROBE_GRACE_SECONDS
    old_deadline = dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS
    urls = ["https://fast-one.example", "https://fast-two.example", "https://dead.example"]

    def probe(url: str) -> str:
        if url == urls[-1]:
            time.sleep(0.8)
        return url

    try:
        dashboard.RPC_HEAD_PROBE_GRACE_SECONDS = 0.05
        dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS = 0.6
        started = time.monotonic()
        results, errors = dashboard._collect_rpc_probes(
            urls,
            required=2,
            preferred=3,
            probe=probe,
            label="test-log-spare",
        )
        elapsed = time.monotonic() - started
    finally:
        dashboard.RPC_HEAD_PROBE_GRACE_SECONDS = old_grace
        dashboard.RPC_HEAD_PROBE_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()

    assert len(results) == 2
    assert any("deadline exceeded" in error for error in errors)
    assert elapsed < 0.25


def test_long_log_scan_always_quorum_checks_recent_tail() -> None:
    dashboard = load_module()
    old_verified = list(dashboard.VERIFIED_LOG_URLS)
    old_max = dashboard.LOG_QUORUM_MAX_BLOCKS
    old_window = dashboard.LOG_QUORUM_WINDOW_BLOCKS
    old_checkpointed = dashboard._fetch_logs_checkpointed
    old_quorum = dashboard.rpc_quorum
    prefix_calls: list[tuple[int, int]] = []
    tail_calls: list[tuple[int, int]] = []

    def fake_checkpointed(
        _address: str,
        _topics: str | list[str],
        start: int,
        end: int,
        _checkpoint: Any = None,
    ) -> list[dict[str, Any]]:
        prefix_calls.append((start, end))
        return []

    def fake_quorum(_method: str, params: list[Any], **_kwargs: Any) -> tuple[list[Any], list[str]]:
        filter_data = params[0]
        tail_calls.append((int(filter_data["fromBlock"], 16), int(filter_data["toBlock"], 16)))
        return [], ["https://one.example", "https://two.example"]

    try:
        dashboard.VERIFIED_LOG_URLS = ["https://one.example", "https://two.example"]
        dashboard.LOG_QUORUM_MAX_BLOCKS = 50
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = 500
        dashboard._fetch_logs_checkpointed = fake_checkpointed
        dashboard.rpc_quorum = fake_quorum
        assert dashboard._fetch_logs_verified_or_uncached(
            "0x" + "a" * 40,
            dashboard.TOPIC_AUCTION_CREATED,
            0,
            1000,
        ) == []
    finally:
        dashboard.VERIFIED_LOG_URLS = old_verified
        dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
        dashboard._fetch_logs_checkpointed = old_checkpointed
        dashboard.rpc_quorum = old_quorum

    assert prefix_calls == [(0, 500)]
    assert tail_calls == [(start, min(1000, start + 49)) for start in range(501, 1001, 50)]


def test_adaptive_quorum_log_scan_large_range_matches_legacy_small_ranges() -> None:
    dashboard = load_module()
    address = "0x" + "a" * 40
    topic = dashboard.TOPIC_AUCTION_CREATED
    expected = [
        canonical_log(address, topic, 520, transaction_index=1),
        canonical_log(address, topic, 750, transaction_index=2),
        canonical_log(address, topic, 999, transaction_index=3),
    ]

    def run_with_span(span: int) -> tuple[list[dict[str, Any]], list[tuple[int, int]]]:
        old_verified = list(dashboard.VERIFIED_LOG_URLS)
        old_max = dashboard.LOG_QUORUM_MAX_BLOCKS
        old_effective = dashboard.VERIFIED_LOG_MAX_BLOCKS
        old_window = dashboard.LOG_QUORUM_WINDOW_BLOCKS
        old_quorum = dashboard.rpc_quorum
        calls: list[tuple[int, int]] = []

        def fake_quorum(method: str, params: list[Any], **kwargs: Any) -> tuple[list[Any], list[str]]:
            assert method == "eth_getLogs"
            assert kwargs["min_agreement"] == 2
            request = params[0]
            start = int(request["fromBlock"], 16)
            end = int(request["toBlock"], 16)
            calls.append((start, end))
            return [row for row in expected if start <= int(row["blockNumber"], 16) <= end], [
                "https://one.example",
                "https://two.example",
            ]

        try:
            dashboard.VERIFIED_LOG_URLS = ["https://one.example", "https://two.example"]
            dashboard.LOG_QUORUM_MAX_BLOCKS = span
            dashboard.VERIFIED_LOG_MAX_BLOCKS = span
            dashboard.LOG_QUORUM_WINDOW_BLOCKS = 500
            dashboard.rpc_quorum = fake_quorum
            result = dashboard._fetch_logs_verified_or_uncached(address, topic, 501, 1000)
        finally:
            dashboard.VERIFIED_LOG_URLS = old_verified
            dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
            dashboard.VERIFIED_LOG_MAX_BLOCKS = old_effective
            dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
            dashboard.rpc_quorum = old_quorum
        return result, calls

    large_result, large_calls = run_with_span(500)
    legacy_result, legacy_calls = run_with_span(50)
    assert large_result == legacy_result == expected
    assert large_calls == [(501, 1000)]
    assert len(legacy_calls) == 10


def test_adaptive_quorum_log_scan_splits_only_explicit_range_rejections() -> None:
    dashboard = load_module()
    address = "0x" + "a" * 40
    topic = dashboard.TOPIC_AUCTION_CREATED
    expected = [
        canonical_log(address, topic, 520, transaction_index=1),
        canonical_log(address, topic, 750, transaction_index=2),
        canonical_log(address, topic, 999, transaction_index=3),
    ]
    old_verified = list(dashboard.VERIFIED_LOG_URLS)
    old_max = dashboard.LOG_QUORUM_MAX_BLOCKS
    old_effective = dashboard.VERIFIED_LOG_MAX_BLOCKS
    old_window = dashboard.LOG_QUORUM_WINDOW_BLOCKS
    old_quorum = dashboard.rpc_quorum
    calls: list[tuple[int, int]] = []

    def bounded_quorum(_method: str, params: list[Any], **_kwargs: Any) -> tuple[list[Any], list[str]]:
        request = params[0]
        start = int(request["fromBlock"], 16)
        end = int(request["toBlock"], 16)
        calls.append((start, end))
        if end - start + 1 > 250:
            raise dashboard.RpcLogRangeLimit("explicit provider range limit")
        return [row for row in expected if start <= int(row["blockNumber"], 16) <= end], [
            "https://one.example",
            "https://two.example",
        ]

    try:
        dashboard.VERIFIED_LOG_URLS = ["https://one.example", "https://two.example"]
        dashboard.LOG_QUORUM_MAX_BLOCKS = 500
        dashboard.VERIFIED_LOG_MAX_BLOCKS = 500
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = 500
        dashboard.rpc_quorum = bounded_quorum
        result = dashboard._fetch_logs_verified_or_uncached(address, topic, 501, 1000)
    finally:
        dashboard.VERIFIED_LOG_URLS = old_verified
        dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
        dashboard.VERIFIED_LOG_MAX_BLOCKS = old_effective
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
        dashboard.rpc_quorum = old_quorum

    assert result == expected
    assert calls == [(501, 1000), (501, 750), (751, 1000)]


def test_adaptive_quorum_log_scan_does_not_split_generic_failures() -> None:
    dashboard = load_module()
    address = "0x" + "a" * 40
    old_verified = list(dashboard.VERIFIED_LOG_URLS)
    old_max = dashboard.LOG_QUORUM_MAX_BLOCKS
    old_effective = dashboard.VERIFIED_LOG_MAX_BLOCKS
    old_window = dashboard.LOG_QUORUM_WINDOW_BLOCKS
    old_quorum = dashboard.rpc_quorum
    calls = 0

    def failed_quorum(*_args: Any, **_kwargs: Any) -> tuple[list[Any], list[str]]:
        nonlocal calls
        calls += 1
        raise RuntimeError("transport/quorum unavailable")

    try:
        dashboard.VERIFIED_LOG_URLS = ["https://one.example", "https://two.example"]
        dashboard.LOG_QUORUM_MAX_BLOCKS = 500
        dashboard.VERIFIED_LOG_MAX_BLOCKS = 500
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = 500
        dashboard.rpc_quorum = failed_quorum
        try:
            dashboard._fetch_logs_verified_or_uncached(
                address,
                dashboard.TOPIC_AUCTION_CREATED,
                501,
                1000,
            )
        except RuntimeError as exc:
            assert type(exc) is RuntimeError
            assert "transport/quorum unavailable" in str(exc)
        else:
            raise AssertionError("generic quorum failure was amplified into smaller log queries")
    finally:
        dashboard.VERIFIED_LOG_URLS = old_verified
        dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
        dashboard.VERIFIED_LOG_MAX_BLOCKS = old_effective
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
        dashboard.rpc_quorum = old_quorum
    assert calls == 1


def test_verified_log_scan_rejects_out_of_range_and_malformed_rows() -> None:
    dashboard = load_module()
    address = "0x" + "a" * 40
    topic = dashboard.TOPIC_AUCTION_CREATED
    valid = canonical_log(address, topic, 550)
    wrong_address = {**valid, "address": "0x" + "b" * 40}
    out_of_range = {**valid, "blockNumber": hex(1001)}
    wrong_topic = {**valid, "topics": [dashboard.TOPIC_AUCTION_BID]}
    malformed = dict(valid)
    malformed.pop("transactionIndex")
    missing_removed = dict(valid)
    missing_removed.pop("removed")

    for rows, expected in (
        ([wrong_address], "wrong contract address"),
        ([out_of_range], "outside its requested block range"),
        ([wrong_topic], "unexpected event topic"),
        ([malformed], "malformed transactionIndex"),
        ([missing_removed], "removed or malformed log"),
        ([None], "non-object log"),
    ):
        try:
            dashboard._validated_quorum_log_chunk(
                rows,
                address=address,
                topics=topic,
                from_block=501,
                to_block=1000,
                seen_identities=set(),
                previous_position=None,
                block_hashes={},
            )
        except dashboard.RpcLogValidationError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"malformed quorum logs were accepted: {expected}")


def test_log_quorum_range_classification_never_uses_generic_failures() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example"]
    old_call = dashboard._rpc_once_with_retry

    try:
        dashboard._rpc_once_with_retry = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            dashboard.RpcLogRangeLimit("explicit range rejection")
        )
        try:
            dashboard.rpc_quorum("eth_getLogs", [{}], urls=urls, min_agreement=2)
        except dashboard.RpcLogRangeLimit:
            pass
        else:
            raise AssertionError("explicit two-provider range rejection was not classified")

        dashboard.RPC_SLOW_UNTIL.clear()
        dashboard._rpc_once_with_retry = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("transport unavailable")
        )
        try:
            dashboard.rpc_quorum("eth_getLogs", [{}], urls=urls, min_agreement=2)
        except dashboard.RpcLogRangeLimit as exc:
            raise AssertionError("generic provider failure was classified as a range limit") from exc
        except RuntimeError as exc:
            assert "quorum disagreement" in str(exc)
        else:
            raise AssertionError("generic provider failure unexpectedly formed quorum")
    finally:
        dashboard._rpc_once_with_retry = old_call
        dashboard.RPC_SLOW_UNTIL.clear()


def test_log_quorum_range_rejection_does_not_wait_for_hung_spare() -> None:
    dashboard = load_module()
    urls = ["https://one.example", "https://two.example", "https://three.example"]
    old_call = dashboard._rpc_once_with_retry
    old_deadline = dashboard.RPC_QUORUM_DEADLINE_SECONDS

    def fake_call(url: str, *_args: Any, **_kwargs: Any) -> Any:
        if url == urls[-1]:
            time.sleep(2.0)
            return []
        raise dashboard.RpcLogRangeLimit("explicit range rejection")

    try:
        dashboard.RPC_SLOW_UNTIL.clear()
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = 2.0
        dashboard._rpc_once_with_retry = fake_call
        started = time.monotonic()
        try:
            dashboard.rpc_quorum("eth_getLogs", [{}], urls=urls, min_agreement=2)
        except dashboard.RpcLogRangeLimit:
            pass
        else:
            raise AssertionError("range-limited quorum unexpectedly waited for the spare")
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"range split waited {elapsed:.3f}s for a hung spare"
    finally:
        dashboard._rpc_once_with_retry = old_call
        dashboard.RPC_QUORUM_DEADLINE_SECONDS = old_deadline
        dashboard.RPC_SLOW_UNTIL.clear()


def test_log_range_error_classifier_requires_explicit_size_language() -> None:
    dashboard = load_module()
    assert dashboard.is_explicit_log_range_error(-32000, "maximum block range is 500")
    assert dashboard.is_explicit_log_range_error(-32602, "block range too large")
    assert dashboard.is_explicit_log_range_error(-32005, "query returned more than 10000 results")
    assert not dashboard.is_explicit_log_range_error(-32000, "block range temporarily unavailable")
    assert not dashboard.is_explicit_log_range_error(-32602, "invalid block range")
    assert not dashboard.is_explicit_log_range_error(413, "maximum block range is 500")


def test_snapshot_recheck_rejects_mid_refresh_reorg() -> None:
    dashboard = load_module()
    old_rpc_once = dashboard._rpc_once
    old_urls = list(dashboard.VERIFIED_SNAPSHOT_URLS)

    def fake_rpc_once(_url: str, method: str, _params: list[Any], *, timeout: int = 30) -> Any:  # noqa: ARG001
        assert method == "eth_getBlockByNumber"
        return {"hash": "0xchanged"}

    try:
        dashboard._rpc_once = fake_rpc_once
        dashboard.VERIFIED_SNAPSHOT_URLS = ["https://one.example", "https://two.example"]
        try:
            dashboard.verify_snapshot_unchanged(100, "0xexpected")
        except RuntimeError as exc:
            assert "reorganized during refresh" in str(exc)
        else:
            raise AssertionError("expected a mid-refresh reorg failure")
    finally:
        dashboard._rpc_once = old_rpc_once
        dashboard.VERIFIED_SNAPSHOT_URLS = old_urls


def test_write_html_includes_browser_favicon_only() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [
                ("site_url", "https://example.test"),
                ("current_auction_token_id", "11"),
            ],
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    assert '<link rel="icon" type="image/png" sizes="32x32" href="favicon-32x32.png">' in rendered
    for marker in (
        '<link rel="icon" href="data:,">',
        "apple-touch-icon",
        "site-brand",
        "site-logo",
        "degen-dogs-logo.png",
    ):
        assert marker not in rendered


def test_unified_archive_bid_cell_formats_usd_from_shared_numeric_fallbacks() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (["metric", "value"], [("site_url", "https://example.test"), ("current_auction_token_id", "11")]),
        "auction_feed": ([
            "status", "dog", "dog_image_url", "dog_external_url", "dog_opensea_url", "bidder_winner",
            "bidder_winner_url", "bidder_winner_wallet", "bid", "amount_eth", "amount_usd", "time_remaining",
            "auction_end_utc", "rarity", "traits", "trait_rarity",
        ], [(
            "ongoing", "Dog #11", "", "", "", "@unitcurrent", "https://farcaster.xyz/unitcurrent",
            "0x00000000000000000000000000000000000000b2", "1.00000 ETH ($2000)", 1.0, 2000.0,
            "02:00:00", "2026-06-02 04:00:00", "Rank 1", "", "",
        )]),
        "current_auction_bid_history": (["bid_time_utc", "token_id", "dog", "bidder", "bidder_url", "bidder_wallet", "bid", "bid_eth", "bid_usd", "block_number", "log_index", "tx_hash"], []),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    required_markers = [
        "const usdCandidates=record=>",
        "amount.amount_usd_at_event",
        "const getUsdSortValue=record=>firstNumeric(usdCandidates(record))",
        "const usdDisplay=record=>",
        "const display=usdDisplay(record)",
        "const archiveCurrentRank=record=>",
        "status==='live'||status.includes('ongoing')?1:0",
    ]
    for marker in required_markers:
        assert marker in rendered


def test_write_html_hydrates_every_current_surface_without_overlapping_polls() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [("site_url", "https://example.test"), ("latest_block", "210"), ("current_auction_token_id", "11")],
        ),
        "auction_feed": (["status", "dog", "bid", "auction_end_utc"], [("ongoing", "Dog #11", "1 ETH", "2026-06-02 04:00:00")]),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    for marker in (
        "data-current-dog",
        "data-current-detail",
        "data-current-rewards",
        "data-current-traits",
        "data-current-dog-stage",
        "const LIVE_REFRESH_MS=5000",
        "const LIVE_RECENT_MS=5*60*1000",
        "const LIVE_RETRY_MAX_MS=2*60*1000",
        "const CURRENT_FETCH_TIMEOUT_MS=6000",
        "const ARCHIVE_FETCH_TIMEOUT_MS=45000",
        "const controller=new AbortController()",
        "const refreshLiveSurface=()=>liveRefreshPromise||",
        "if(liveSnapshotBlock&&Number(nextBlock)<Number(liveSnapshotBlock))throw new Error('verified snapshot block regressed')",
        "cache:'no-store'",
        "const generatedUrls=(name,version)=>{const url=new URL(`generated/${name}.json`,document.baseURI)",
        "return [url.href]",
        "fetchGenerated('current_auction',block)",
        "fetchGenerated('auction_feed',block)",
        "fetchGenerated('current_auction_bid_history',block)",
        "fetchGenerated('mission3_metrics',block)",
        "const fetchVerifiedGenerated=async(filename,expectedSha,expectedBytes,maxBytes,timeoutMs)=>",
        "crypto.subtle.digest('SHA-256',bytes)",
        "const liveSnapshotPointer=status=>",
        "const archivePointer=status=>",
        "const assertLiveBundle=",
        "const loadLiveSnapshot=async status=>",
        "live snapshot pointer changed during verification",
        "target.archive?await fetchVerifiedGenerated('unified_dog_search_index.json'",
        "const assertCurrentSnapshot=",
        "const assertStatusAttestation=status=>",
        "if(!statusFreshness(status).usable)throw new Error('verified snapshot is unsuccessful or has invalid timestamps')",
        "const canonicalUint=(value,label,minimum=0)=>",
        "const assertArchiveSnapshot=",
        "const queueArchiveRefresh=context=>",
        "if(target.key!==liveSnapshotKey||targetArchiveKey!==activeArchiveKey)continue",
        "generated snapshot is not atomic yet",
        "hydrateCurrentCard(context.feed,context.current,context.history,context.metrics)",
        "setVerificationState(status);const nextArchiveKey=",
        "if(context&&archiveSnapshotKey!==nextArchiveKey)queueArchiveRefresh(context)",
        "const refreshNow=async()=>",
        "if(document.hidden)return",
        "refreshNow();",
        "window.addEventListener('online',refreshNow)",
        "archiveState.columnSort=col",
        "if(table===auctionTable)",
        "window.requestAnimationFrame",
        "img.fetchPriority='high'",
    ):
        assert marker in rendered, marker
    assert "setInterval(refreshLiveSurface" not in rendered
    assert "refreshLiveSurface().finally(scheduleLiveRefresh)" not in rendered
    assert "status.last_refresh_result!=='success_generated'||!setVerificationState(status)" not in rendered
    assert "fetchGenerated('mission3_metrics',block),fetchGenerated('unified_dog_search_index'" not in rendered
    assert "raw.githubusercontent.com" not in rendered
    assert "rootLocal" not in rendered
    assert "const updateLiveDots=" not in rendered

    csp_match = re.search(
        r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', rendered
    )
    style_match = re.search(r"<style>(.*?)</style>", rendered, re.DOTALL)
    script_match = re.search(r"<script>(.*?)</script>", rendered, re.DOTALL)
    assert csp_match and style_match and script_match
    csp = html_module.unescape(csp_match.group(1))
    style_hash = base64.b64encode(hashlib.sha256(style_match.group(1).encode()).digest()).decode()
    script_hash = base64.b64encode(hashlib.sha256(script_match.group(1).encode()).digest()).decode()
    assert f"style-src 'sha256-{style_hash}'" in csp
    assert f"script-src 'sha256-{script_hash}'" in csp
    assert "default-src 'none'" in csp
    assert "connect-src 'self'" in csp
    assert '<meta name="referrer" content="no-referrer">' in rendered


def test_live_ui_sorts_archive_globally_and_verifies_only_complete_snapshots() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [("site_url", "https://example.test"), ("latest_block", "210"), ("current_auction_token_id", "11")],
        ),
        "auction_feed": (
            ["status", "dog", "bid", "auction_end_utc", "dog_image_url"],
            [("ongoing", "Dog #11", "1 ETH", "2026-06-02 04:00:00", "https://api.degendogs.club/images/11.png")],
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    assert script.index("rows=sortRows(rows)") < script.index("const pageRows=rows.slice")
    assert "if(table===auctionTable){" in script
    archive_branch = script.split("if(table===auctionTable){", 1)[1].split("const tbody=table.tBodies[0]", 1)[0]
    assert "archiveState.columnSort=col" in archive_branch
    assert "renderArchive();return" in archive_branch
    attestation = script.split("const assertStatusAttestation=status=>", 1)[1].split(
        "const assertCurrentSnapshot=", 1
    )[0]
    assert "setVerificationState" not in attestation
    refresh = script.split("const refreshLiveSurface=", 1)[1].split("const emptyArchiveMessage=", 1)[0]
    assert refresh.index("assertCurrentSnapshot") < refresh.index("setVerificationState(status)")
    assert refresh.index("loadLiveSnapshot(status)") < refresh.index("setVerificationState(status)")
    assert "confirmedKey!==candidate.key" in refresh
    assert "crypto.subtle.digest('SHA-256',bytes)" in script
    assert "live_snapshot_([1-9]\\d*)_([0-9a-f]{64})_([0-9a-f]{64})" in script
    assert "target.archive?await fetchVerifiedGenerated('unified_dog_search_index.json'" in script
    assert "targetArchiveKey!==activeArchiveKey" in script
    assert "if(document.hidden)return false" in script
    assert "if(document.hidden)return" in script
    assert "window.requestAnimationFrame" in script
    assert 'fetchpriority="high"' in rendered


def test_live_ui_accepts_attested_stale_snapshot_for_last_good_archive() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (
            ["metric", "value"],
            [("site_url", "https://example.test"), ("latest_block", "210")],
        ),
        "auction_feed": (
            ["status", "dog", "bid", "auction_end_utc"],
            [("ongoing", "Dog #11", "1 ETH", "2026-06-02 04:00:00")],
        ),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    prefixes = (
        "const LIVE_RECENT_MS=",
        "const LIVE_STALE_MS=",
        "const parseUtc=",
        "const statusFreshness=",
        "const canonicalUint=",
        "const assertStatusAttestation=",
    )
    lines = script.splitlines()
    definitions = "\n".join(next(line for line in lines if line.startswith(prefix)) for prefix in prefixes)
    status = {
        "last_refresh_result": "success_generated",
        "last_successful_refresh_time_utc": "2026-06-01T00:00:00Z",
        "latest_generated_block_time_utc": "2026-06-01T00:00:00Z",
        "latest_generated_block": 210,
        "snapshot_block_hash": "0x" + "a" * 64,
        "auction_house_code_sha256": "b" * 64,
        "dog_nft_code_sha256": "c" * 64,
        "rpc_quorum_size": 2,
        "rpc_quorum_agreement": "2/3",
        "rpc_quorum_providers": "provider-a,provider-b",
        "snapshot_confirmations": 2,
        "onchain_chain_id": 8453,
        "onchain_verification_scope": (
            "snapshot_hash,contract_code,current_auction,dog_total_supply,"
            "dog_token_uri_bindings,recent_event_logs"
        ),
    }
    program = f"""
{definitions}
Date.now=()=>Date.parse('2026-06-03T00:00:00Z');
const status={json.dumps(status, separators=(',', ':'))};
const freshness=statusFreshness(status);
if(!freshness.usable||freshness.recent||!freshness.stale)throw new Error('stale last-good classification failed');
if(assertStatusAttestation(status)!=='210')throw new Error('stale attestation was rejected');
const failed={{...status,last_refresh_result:'failed'}};
let rejected=false;
try{{assertStatusAttestation(failed);}}catch(_error){{rejected=true;}}
if(!rejected)throw new Error('unsuccessful status was accepted');
"""
    result = subprocess.run(
        ["node", "-e", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    refresh = script.split("const refreshLiveSurface=", 1)[1].split(
        "const emptyArchiveMessage=", 1
    )[0]
    assert refresh.index("assertCurrentSnapshot") < refresh.index("queueArchiveRefresh(context)")
    assert "verified stale last-good" in script



def write_reward_snapshot(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "snapshot_utc": "2026-06-02T20:55:15Z",
        "reward_account_dogs_count": "133",
        "account_woof_flow_per_day": "20494201.30",
        "account_sup_flow_per_day": "199.58",
        "account_woof_received": "2856495886.75",
        "account_sup_received": "38733.66",
        "derived_woof_per_dog_per_day": "154091.739097744361",
        "derived_sup_per_dog_per_day": "1.5006015037593985",
        "basis_source": "observed_stream_snapshot_133_dogs",
        "note": "Observed reward account stream snapshot; update when the live stream changes.",
    }, indent=2) + "\n", encoding="utf-8")


def test_load_reward_stream_snapshot_derives_observed_per_dog_values() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "config" / "reward_stream_snapshot.json"
        write_reward_snapshot(snapshot_path)
        snapshot = dashboard.load_reward_stream_snapshot(snapshot_path)
        assert snapshot.dogs_count == Decimal("133")
        assert snapshot.woof_flow_per_day == Decimal("20494201.30")
        assert snapshot.sup_flow_per_day == Decimal("199.58")
        assert dashboard.decimal_value_str(snapshot.woof_per_dog_per_day, 12) == "154091.739097744361"
        assert dashboard.decimal_value_str(snapshot.sup_per_dog_per_day, 16) == "1.5006015037593985"


def test_reward_token_stats_uses_observed_snapshot_for_per_dog_flows_and_usd_totals() -> None:
    dashboard = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = Path(tmp) / "config" / "reward_stream_snapshot.json"
        write_reward_snapshot(snapshot_path)
        snapshot = dashboard.load_reward_stream_snapshot(snapshot_path)
    stats = dashboard.reward_token_stats(Decimal("0.0000005"), Decimal("0.02"), snapshot=snapshot)
    assert stats["reward_basis_dogs"] == "133"
    assert stats["reward_basis_source"] == "observed_stream_snapshot_133_dogs"
    assert stats["reward_observed_dogs_count"] == "133"
    assert stats["reward_observed_woof_flow_per_day"] == "20494201.3"
    assert stats["reward_observed_sup_flow_per_day"] == "199.58"
    assert stats["reward_observed_woof_received"] == "2856495886.75"
    assert stats["reward_observed_sup_received"] == "38733.66"
    assert stats["reward_observed_woof_per_dog_per_day"] == "154091.739097744361"
    assert stats["reward_observed_sup_per_dog_per_day"] == "1.5006015037593985"
    assert stats["reward_woof_per_dog_per_day"] == "154091.739097744361"
    assert stats["reward_sup_per_dog_per_day"] == "1.5006015037593985"
    assert stats["reward_total_per_dog_usd_per_day"] == "0.107058"


def test_reward_strip_renders_apr_inside_bid_payback_card_with_caveat_copy() -> None:
    dashboard = load_module()
    metrics = {
        "reward_basis_dogs": "133",
        "reward_basis_source": "observed_stream_snapshot_133_dogs",
        "reward_woof_per_dog_per_day": "154091.739097744361",
        "reward_woof_per_dog_usd_per_day": "0.077046",
        "reward_sup_per_dog_per_day": "1.5006015037593985",
        "reward_sup_per_dog_usd_per_day": "0.030012",
        "reward_total_per_dog_usd_per_day": "0.107058",
        "reward_current_bid_payback_days": "186.63",
        "reward_current_bid_apr_pct": "195.58",
        "reward_current_bid_apr_display": "≈196% APR",
    }
    rendered = dashboard.render_reward_strip(metrics)
    assert "<b>Bid payback</b>" in rendered
    assert "Observed 133-Dog stream" not in rendered
    assert "WOOF Vault Bonus excluded." not in rendered
    assert "≈187 days" in rendered
    assert "≈196% APR" in rendered
    assert "Current bid / observed per-Dog flow" in rendered
    assert "Simple APR estimate" in rendered
    assert "not guaranteed" in rendered.lower()
    assert "guaranteed return" not in rendered.lower()


def season6_test_config(
    dashboard: Any,
    *,
    total: str = "1000",
    cap: str = "600",
    campaign_seconds: int = 100,
    expected_future_settlement_interval_seconds: int = 0,
    projection_model: str = "time_weighted_xp_unit_test",
) -> Any:
    campaign_end = f"2026-06-02T00:{campaign_seconds // 60:02d}:{campaign_seconds % 60:02d}Z"
    return dashboard.Season6SupConfig(
        enabled=True,
        sup_token=dashboard.SUP.lower(),
        season_start_utc="2026-06-02T00:00:00Z",
        season_end_utc=campaign_end,
        total_sup=Decimal(total),
        cap_sup=Decimal(cap),
        xp_per_settled_win=Decimal("100"),
        reward_start_delay_days=0,
        cap_level="wallet_estimate",
        projection_model=projection_model,
        expected_future_settlement_interval_seconds=expected_future_settlement_interval_seconds,
        visible_dashboard_mode="compact_final_estimate_only",
        cap_percent_label="5% cap",
    )


def test_season6_time_sliced_rewards_split_after_later_xp_event() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:50Z"},
        ],
        {"token_id": 3, "bidder": bob, "amount_eth": 0.03, "end_time_utc": "2026-06-02T00:01:40Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard),
    )
    by_winner = {row["winner_wallet"]: row for row in outputs["season6_sup_by_winner"]}
    assert by_winner[alice]["season6_wins_confirmed"] == 1
    assert by_winner[alice]["season6_xp_confirmed"] == 100
    assert by_winner[alice]["season6_raw_sup_projected_full"] == "750"
    assert by_winner[bob]["season6_raw_sup_projected_full"] == "250"
    assert by_winner[alice]["season6_capped_sup_projected_full"] == "600"
    assert by_winner[alice]["season6_cap_limited"] == "true"
    assert outputs["season6_metrics"]["season6_sup_unallocated_due_to_zero_xp"] == "0"


def test_season6_cap_uses_explicit_12500_sup_not_percent_math() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": wallet, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {},
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard, total="251340", cap="12500"),
    )
    row = outputs["season6_sup_by_winner"][0]
    assert row["season6_raw_sup_projected_full"] == "251340"
    assert row["season6_cap_sup"] == "12500"
    assert row["season6_capped_sup_projected_full"] == "12500"
    assert row["season6_cap_limited"] == "true"


def test_season6_price_missing_keeps_raw_sup_and_na_usd() -> None:
    dashboard = load_module()
    wallet = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": wallet, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {},
        {"sup_usd_price": "0", "sup_usd_source": "unavailable"},
        snapshot_time_utc="2026-06-02T00:01:40Z",
        config=season6_test_config(dashboard),
    )
    row = outputs["season6_sup_by_winner"][0]
    assert row["season6_raw_sup_projected_full"] == "1000"
    assert row["season6_raw_usd_projected_full"] == "N/A"
    assert row["season6_capped_usd_projected_full"] == "N/A"


def test_season6_current_bidder_projection_adds_hypothetical_win_and_prior_status() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    outputs = dashboard.build_season6_sup_outputs(
        [{"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"}],
        {"token_id": 2, "bidder": alice, "amount_eth": 0.02, "end_time_utc": "2026-06-02T00:00:50Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:00:50Z",
        config=season6_test_config(dashboard),
    )
    status = outputs["season6_sup_current_bidder_status"][0]
    metrics = outputs["season6_metrics"]
    assert status["current_bidder_wallet"] == alice
    assert status["prior_s6_wins_confirmed"] == 1
    assert status["prior_s6_xp_confirmed"] == 100
    assert status["projected_s6_wins_if_current_bid_wins"] == 2
    assert status["projected_s6_xp_if_current_bid_wins"] == 200
    assert status["projected_capped_sup_if_current_bid_wins"] == "600"
    assert status["current_bidder_cap_status"] == "wallet_near_cap"
    assert metrics["season6_sup_current_bidder_prior_s6_wins"] == "1"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_sup"] == "0"
    assert metrics["season6_sup_current_bid_estimate_status"] == "wallet_near_cap"


def test_season6_three_equal_winners_split_one_third_after_third_win() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    carol = "0x00000000000000000000000000000000000000c3"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:30Z"},
            {"token_id": 3, "winner": carol, "amount_eth": 0.03, "block_time_utc": "2026-06-02T00:01:00Z"},
        ],
        {},
        {"sup_usd_price": "1", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:30Z",
        config=season6_test_config(dashboard, total="90", cap="1000", campaign_seconds=90),
    )
    by_winner = {row["winner_wallet"]: row for row in outputs["season6_sup_by_winner"]}
    assert by_winner[alice]["season6_raw_sup_projected_full"] == "55"
    assert by_winner[bob]["season6_raw_sup_projected_full"] == "25"
    assert by_winner[carol]["season6_raw_sup_projected_full"] == "10"


def test_season6_outputs_are_independent_of_settlement_input_order() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    settled = [
        {"token_id": 3, "winner": alice, "amount_eth": 0.03, "block_time_utc": "2026-06-02T00:01:00Z"},
        {"token_id": 1, "winner": bob, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
        {"token_id": 2, "winner": alice, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:30Z"},
    ]
    kwargs = {
        "current": {},
        "token_stats": {
            "sup_usd_price": "1",
            "sup_usd_source": "unit-test",
            "eth_usd_price": "1000",
        },
        "snapshot_time_utc": "2026-06-02T00:01:30Z",
        "config": season6_test_config(
            dashboard,
            total="90",
            cap="1000",
            campaign_seconds=90,
        ),
    }

    forward = dashboard.build_season6_sup_outputs(settled, **kwargs)
    reversed_input = dashboard.build_season6_sup_outputs(list(reversed(settled)), **kwargs)

    assert forward == reversed_input
    alice_row = next(
        row
        for row in forward["season6_sup_by_winner"]
        if row["winner_wallet"] == alice
    )
    assert alice_row["season6_token_ids"] == "2,3"


def test_season6_current_bid_estimate_is_incremental_cap_aware_and_counts_prior_wins() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    bob = "0x00000000000000000000000000000000000000b2"
    outputs = dashboard.build_season6_sup_outputs(
        [
            {"token_id": 1, "winner": alice, "amount_eth": 0.01, "block_time_utc": "2026-06-02T00:00:00Z"},
            {"token_id": 2, "winner": bob, "amount_eth": 0.02, "block_time_utc": "2026-06-02T00:00:50Z"},
        ],
        {"token_id": 3, "bidder": alice, "amount_eth": 0.03, "end_time_utc": "2026-06-02T00:01:15Z"},
        {"sup_usd_price": "2", "sup_usd_source": "unit-test", "eth_usd_price": "1000"},
        snapshot_time_utc="2026-06-02T00:01:15Z",
        config=season6_test_config(dashboard, total="1000", cap="760", campaign_seconds=100),
    )
    metrics = outputs["season6_metrics"]
    assert metrics["season6_sup_current_bidder_prior_s6_wins"] == "1"
    assert metrics["season6_sup_current_bidder_prior_s6_xp"] == "100"
    assert metrics["season6_sup_current_bid_projected_total_without_win_sup"] == "750"
    assert metrics["season6_sup_current_bid_projected_total_with_win_sup"] == "791.666667"
    assert metrics["season6_sup_current_bid_estimated_raw_incremental_sup"] == "41.666667"
    assert metrics["season6_sup_current_bid_cap_remaining_before_win_sup"] == "10"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_sup"] == "10"
    assert metrics["season6_sup_current_bid_estimated_cap_aware_usd"] == "20"


def test_season6_future_daily_dilution_reduces_current_bid_estimate() -> None:
    dashboard = load_module()
    alice = "0x00000000000000000000000000000000000000a1"
    current = {"token_id": 1, "bidder": alice, "amount_eth": 0.01, "end_time_utc": "2026-06-02T00:00:00Z"}
    no_future = dashboard.build_season6_sup_outputs(
        [],
        current,
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:00:00Z",
        config=season6_test_config(dashboard, total="1000", cap="2000", campaign_seconds=100, expected_future_settlement_interval_seconds=0),
    )["season6_metrics"]
    with_future = dashboard.build_season6_sup_outputs(
        [],
        current,
        {"sup_usd_price": "1", "sup_usd_source": "unit-test"},
        snapshot_time_utc="2026-06-02T00:00:00Z",
        config=season6_test_config(dashboard, total="1000", cap="2000", campaign_seconds=100, expected_future_settlement_interval_seconds=50),
    )["season6_metrics"]
    assert no_future["season6_sup_current_bid_estimated_cap_aware_sup"] == "1000"
    assert with_future["season6_sup_future_dilution_enabled"] == "true"
    assert with_future["season6_sup_current_bid_estimated_cap_aware_sup"] == "750"
    assert Decimal(with_future["season6_sup_current_bid_estimated_cap_aware_sup"]) < Decimal(no_future["season6_sup_current_bid_estimated_cap_aware_sup"])


def test_season6_compact_card_uses_final_cap_aware_estimate_only() -> None:
    dashboard = load_module()
    rendered = dashboard.render_season6_strip({
        "season6_sup_enabled": "true",
        "season6_sup_estimate_status": "estimated",
        "season6_sup_current_bid_estimated_cap_aware_sup": "11240.25",
        "season6_sup_current_bid_estimated_cap_aware_usd": "118.02",
    })
    assert "Season 6 SUP estimate" in rendered
    assert "≈11,240 SUP" in rendered
    assert "≈$118 if current bid wins" in rendered
    assert "Adjusted for prior S6 wins; estimate only." in rendered
    forbidden = ["Pool:", "Cap:", "100 XP per settled Dog win", "Projected if current bid wins", "Cap-limited estimate"]
    assert not any(text in rendered for text in forbidden)


def test_season6_compact_card_neutral_without_current_high_bidder() -> None:
    dashboard = load_module()
    rendered = dashboard.render_season6_strip({
        "season6_sup_enabled": "true",
        "season6_sup_estimate_status": "no_current_bid",
    })
    assert "Bid to estimate S6 SUP" in rendered
    assert "Pool:" not in rendered
    assert "Cap:" not in rendered


def test_dashboard_urls_reject_executable_mixed_content_and_lookalike_hosts() -> None:
    dashboard = load_module()
    for unsafe in (
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "http://opensea.io/item/base/0x1/1",
        "//opensea.io/item/base/0x1/1",
        "https://opensea.io.evil.example/item/1",
        "https://opensea.io@evil.example/item/1",
        "https://opensea.io:444/item/base/0x1/1",
        "https://127.0.0.1/private",
    ):
        assert dashboard.safe_dashboard_link(unsafe) == ""
    assert dashboard.safe_dashboard_link("https://opensea.io/item/base/0x1/1").startswith("https://opensea.io/")
    assert dashboard.safe_dashboard_image("https://api.degendogs.club/images/1.png").startswith("https://api.degendogs.club/")
    assert dashboard.safe_dashboard_image("https://opensea.io/image.png") == ""


def test_static_dashboard_cells_drop_untrusted_links_and_images() -> None:
    dashboard = load_module()
    dog = dashboard.render_cell("dog", "Dog #1", {
        "dog_image_url": "javascript:alert(1)",
        "dog_external_url": "https://opensea.io.evil.example/item/1",
        "dog_opensea_url": "data:text/html,boom",
    })
    bidder = dashboard.render_cell("bidder_winner", "Attacker", {
        "bidder_winner_url": "javascript:alert(1)",
    })
    assert "href=" not in dog and "src=" not in dog
    assert "href=" not in bidder
    assert "javascript:" not in dog + bidder


def test_metadata_fetch_blocks_ssrf_and_accepts_bounded_inline_json() -> None:
    dashboard = load_module()
    for unsafe in (
        "file:///etc/passwd",
        "http://api.degendogs.club/meta/1",
        "https://localhost/meta/1",
        "https://127.0.0.1/meta/1",
        "https://api.degendogs.club.evil.example/meta/1",
        "https://api.degendogs.club@evil.example/meta/1",
        "https://evil.example/meta/1",
    ):
        try:
            dashboard.fetch_url_json(unsafe)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe metadata URL was accepted: {unsafe}")

    raw = json.dumps({"name": "Degen Dog #1", "attributes": []}).encode("utf-8")
    import base64
    inline = "data:application/json;base64," + base64.b64encode(raw).decode("ascii")
    assert dashboard.fetch_url_json(inline)["name"] == "Degen Dog #1"


def test_metadata_fetch_rejects_non_json_response_content_type() -> None:
    dashboard = load_module()
    old_build_opener = dashboard.urllib.request.build_opener

    class FakeResponse:
        def __init__(self, content_type: str) -> None:
            self.headers = {"Content-Type": content_type, "Content-Length": "2"}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b"{}"

    class FakeOpener:
        def __init__(self, content_type: str) -> None:
            self.content_type = content_type

        def open(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse(self.content_type)

    try:
        dashboard.urllib.request.build_opener = lambda *_args: FakeOpener("text/html")
        try:
            dashboard.fetch_url_json("https://api.degendogs.club/meta/1")
        except ValueError as exc:
            assert "unsafe content type" in str(exc)
        else:
            raise AssertionError("HTML metadata response was accepted")
        dashboard.urllib.request.build_opener = lambda *_args: FakeOpener("application/ld+json; charset=utf-8")
        assert dashboard.fetch_url_json("https://api.degendogs.club/meta/1") == {}
    finally:
        dashboard.urllib.request.build_opener = old_build_opener


def test_generated_dashboard_hardens_dynamic_urls_freshness_and_archive_rows() -> None:
    dashboard = load_module()
    tables = {
        "mission3_metrics": (["metric", "value"], [("site_url", "https://example.test"), ("current_auction_token_id", "1")]),
        "auction_feed": ([
            "status", "dog", "dog_image_url", "dog_external_url", "dog_opensea_url",
            "bidder_winner", "bidder_winner_url",
        ], [(
            "ongoing", "Dog #1", "javascript:alert(1)", "https://evil.example/dog",
            "javascript:alert(2)", "Attacker", "data:text/html,boom",
        )]),
    }
    with tempfile.TemporaryDirectory() as tmp:
        old_root = dashboard.ROOT
        try:
            dashboard.ROOT = Path(tmp)
            dashboard.write_html(tables)
            rendered = (Path(tmp) / "index.html").read_text(encoding="utf-8")
        finally:
            dashboard.ROOT = old_root

    for executable_attribute in ('href="javascript:', 'src="javascript:', 'href="data:text', 'src="data:text'):
        assert executable_attribute not in rendered
    for marker in (
        "const SAFE_LINK_HOSTS=new Set(",
        "const SAFE_IMAGE_HOSTS=new Set(",
        "url.protocol==='https:'",
        "url.port===''",
        "Array.isArray(record.bid_tx_hashes)?record.bid_tx_hashes:[]",
        "const LIVE_RECENT_MS=5*60*1000",
        "const LIVE_STALE_MS=90*60*1000",
        "data-live-label",
        "status?.last_refresh_result==='success_generated'",
        "verified last-good",
        "const generatedUrls=(name,version)=>{const url=new URL(`generated/${name}.json`,document.baseURI)",
    ):
        assert marker in rendered
    assert "const LIVE_STALE_MS=3*60*60*1000" not in rendered
    assert "raw.githubusercontent.com" not in rendered


def test_verified_log_collection_rejects_disabled_quorum_and_overlap_knobs() -> None:
    dashboard = load_module()
    old_urls = list(dashboard.VERIFIED_LOG_URLS)
    old_max = dashboard.LOG_QUORUM_MAX_BLOCKS
    old_window = dashboard.LOG_QUORUM_WINDOW_BLOCKS
    old_overlap = dashboard.LOG_CACHE_OVERLAP_BLOCKS
    try:
        dashboard.VERIFIED_LOG_URLS = ["https://one.example", "https://two.example"]
        dashboard.LOG_QUORUM_MAX_BLOCKS = 0
        try:
            dashboard._fetch_logs_verified_or_uncached("0xabc", dashboard.TOPIC_AUCTION_CREATED, 1, 2)
        except RuntimeError as exc:
            assert "positive quorum" in str(exc)
        else:
            raise AssertionError("expected zero-sized verified log quorum to fail closed")

        dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
        dashboard.LOG_CACHE_OVERLAP_BLOCKS = 0
        try:
            dashboard.fetch_logs("0xabc", dashboard.TOPIC_AUCTION_CREATED, 1, 2)
        except RuntimeError as exc:
            assert "positive cache overlap" in str(exc)
        else:
            raise AssertionError("expected zero-overlap verified log cache to fail closed")
    finally:
        dashboard.VERIFIED_LOG_URLS = old_urls
        dashboard.LOG_QUORUM_MAX_BLOCKS = old_max
        dashboard.LOG_QUORUM_WINDOW_BLOCKS = old_window
        dashboard.LOG_CACHE_OVERLAP_BLOCKS = old_overlap


def test_onchain_token_uri_is_metadata_authority_and_cache_is_content_bound() -> None:
    dashboard = load_module()
    authoritative = {
        0: {"name": "Degen Dog #0", "attributes": rarity_attributes(Body="Rare")},
        1: {"name": "Degen Dog #1", "attributes": rarity_attributes(Body="Common")},
        2: {"name": "Degen Dog #2", "attributes": rarity_attributes(Body="Common")},
    }
    uris = {token_id: f"https://ipfs.io/ipfs/authoritative-{token_id}" for token_id in authoritative}
    mirror_calls: list[str] = []
    content_calls: list[str] = []
    old_cache = dashboard.DOG_METADATA_CACHE
    old_bindings = dashboard.fetch_token_uri_bindings
    old_fetch_json = dashboard.fetch_url_json
    old_threshold = dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD
    cache_payload: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.DOG_METADATA_CACHE = Path(tmp) / "dog_metadata.json"
            dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD = 10
            dashboard.fetch_token_uri_bindings = lambda token_ids, _block: {token_id: uris[token_id] for token_id in token_ids}

            def fake_fetch_json(url: str, timeout: int = 45) -> dict[str, Any]:  # noqa: ARG001
                if url.startswith("https://degendogs.club/meta/"):
                    mirror_calls.append(url)
                    return {"attributes": rarity_attributes(Body="Common")}
                content_calls.append(url)
                token_id = int(url.rsplit("-", 1)[1])
                return authoritative[token_id]

            dashboard.fetch_url_json = fake_fetch_json
            first = dashboard.fetch_dog_metadata_rows(3, "0x64")
            second = dashboard.fetch_dog_metadata_rows(3, "0x65")
            cache_payload = json.loads(dashboard.DOG_METADATA_CACHE.read_text(encoding="utf-8"))
        finally:
            dashboard.DOG_METADATA_CACHE = old_cache
            dashboard.fetch_token_uri_bindings = old_bindings
            dashboard.fetch_url_json = old_fetch_json
            dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD = old_threshold

    by_token = {row["token_id"]: row for row in first}
    assert "Body: Rare" in by_token[0]["traits"]
    assert by_token[0]["rarity"] == "#1/3"
    assert second == first
    assert mirror_calls == []
    assert sorted(content_calls) == sorted(uris.values())
    assert cache_payload["schema_version"] == 3
    assert cache_payload["tokens"]["0"]["fetched_at_utc"].endswith("Z")
    assert cache_payload["tokens"]["0"]["token_uri_sha256"] == hashlib.sha256(uris[0].encode()).hexdigest()
    assert len(cache_payload["tokens"]["0"]["content_sha256"]) == 64


def test_nonexistent_token_uri_omits_cached_metadata_and_marks_provenance() -> None:
    dashboard = load_module()
    old_cache = dashboard.DOG_METADATA_CACHE
    metadata = {
        "token_id": 0,
        "name": "Untrusted historical Dog name",
        "attributes": [{"trait_type": "Body", "value": "Forged"}],
    }
    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.DOG_METADATA_CACHE = Path(tmp) / "dog_metadata.json"
            dashboard.write_dog_cache({
                "0": {
                    "token_uri_sha256": "a" * 64,
                    "content_sha256": "b" * 64,
                    "metadata_sha256": dashboard.metadata_sha256(metadata),
                    "verified_block": 99,
                    "fetched_at_utc": (
                        dashboard.datetime.now(dashboard.timezone.utc)
                        .replace(microsecond=0)
                        .isoformat()
                        .replace("+00:00", "Z")
                    ),
                    "metadata": metadata,
                }
            })
            rows = dashboard.fetch_dog_metadata_rows(1, "0x64", token_uris={0: None})
            stored = json.loads(dashboard.DOG_METADATA_CACHE.read_text(encoding="utf-8"))
        finally:
            dashboard.DOG_METADATA_CACHE = old_cache

    assert rows[0]["dog_name"] == "Degen Dog #0"
    assert rows[0]["traits"] == ""
    assert rows[0]["rarity"] == "Unavailable"
    assert rows[0]["metadata_verification_status"] == "onchain_token_uri_unavailable"
    assert stored["tokens"] == {}


def test_metadata_failure_never_fabricates_rarity_rank() -> None:
    dashboard = load_module()
    old_cache = dashboard.DOG_METADATA_CACHE
    old_bindings = dashboard.fetch_token_uri_bindings
    old_record = dashboard.authoritative_metadata_record
    old_threshold = dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD
    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.DOG_METADATA_CACHE = Path(tmp) / "dog_metadata.json"
            dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD = 10
            dashboard.fetch_token_uri_bindings = lambda token_ids, _block: {
                token_id: f"https://ipfs.io/ipfs/dog-{token_id}" for token_id in token_ids
            }

            def fake_record(token_id: int, _block: str, _uri: str | None = None) -> dict[str, Any]:
                if token_id == 1:
                    raise TimeoutError("metadata endpoint unavailable")
                return {
                    "metadata": {
                        "token_id": token_id,
                        "name": f"Degen Dog #{token_id}",
                        "attributes": rarity_attributes(Body="Blue"),
                    }
                }

            dashboard.authoritative_metadata_record = fake_record
            rows = dashboard.fetch_dog_metadata_rows(2, "0x64")
        finally:
            dashboard.DOG_METADATA_CACHE = old_cache
            dashboard.fetch_token_uri_bindings = old_bindings
            dashboard.authoritative_metadata_record = old_record
            dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD = old_threshold

    by_token = {row["token_id"]: row for row in rows}
    assert by_token[0]["metadata_verification_status"] == "onchain_token_uri_verified"
    assert by_token[1]["metadata_verification_status"] == "unavailable"
    assert all(row["rarity"] == "Unavailable" for row in rows)
    assert all(row["rarity_score"] is None for row in rows)
    assert all(row["trait_rarity"] == "" for row in rows)


def test_sparse_base_existing_tokens_receive_scoped_rarity_without_unclaimed_ids() -> None:
    dashboard = load_module()
    old_cache = dashboard.DOG_METADATA_CACHE
    old_record = dashboard.authoritative_metadata_record
    old_threshold = dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD
    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.DOG_METADATA_CACHE = Path(tmp) / "dog_metadata.json"
            dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD = 10

            def fake_record(token_id: int, _block: str, token_uri: str | None = None) -> dict[str, Any]:
                assert token_uri
                return {
                    "metadata": {
                        "token_id": token_id,
                        "name": f"Degen Dog #{token_id}",
                        "attributes": rarity_attributes(Body="Rare" if token_id == 0 else "Common"),
                    }
                }

            dashboard.authoritative_metadata_record = fake_record
            rows = dashboard.fetch_dog_metadata_rows(
                3,
                "0x64",
                token_uris={
                    0: "https://degendogs.club/meta/0",
                    1: None,
                    2: "https://degendogs.club/meta/2",
                },
            )
        finally:
            dashboard.DOG_METADATA_CACHE = old_cache
            dashboard.authoritative_metadata_record = old_record
            dashboard.DOG_METADATA_SEQUENTIAL_THRESHOLD = old_threshold

    by_token = {row["token_id"]: row for row in rows}
    assert by_token[0]["rarity"] == "#1/2"
    assert by_token[2]["rarity"] == "#1/2"
    assert "Body: Rare (50.0%)" in by_token[0]["trait_rarity"]
    assert "Background: None (100.0%)" in by_token[0]["trait_rarity"]
    assert by_token[1]["rarity"] == "Unavailable"
    assert by_token[1]["rarity_score"] is None
    assert by_token[1]["metadata_verification_status"] == "onchain_token_uri_unavailable"


def test_verified_metadata_with_invalid_trait_schema_fails_closed() -> None:
    dashboard = load_module()
    old_cache = dashboard.DOG_METADATA_CACHE
    old_record = dashboard.authoritative_metadata_record
    with tempfile.TemporaryDirectory() as tmp:
        try:
            dashboard.DOG_METADATA_CACHE = Path(tmp) / "dog_metadata.json"
            dashboard.authoritative_metadata_record = lambda token_id, _block, _uri=None: {
                "metadata": {
                    "token_id": token_id,
                    "attributes": [
                        *rarity_attributes(),
                        {"trait_type": "Body", "value": "Forged"},
                    ],
                }
            }
            try:
                dashboard.fetch_dog_metadata_rows(
                    1,
                    "0x64",
                    token_uris={0: "https://degendogs.club/meta/0"},
                )
            except RuntimeError as exc:
                assert "repeats rarity trait" in str(exc)
            else:
                raise AssertionError("duplicate rarity trait reached scoring")
        finally:
            dashboard.DOG_METADATA_CACHE = old_cache
            dashboard.authoritative_metadata_record = old_record


def test_full_builder_cleanup_preserves_only_canonical_live_snapshot_names() -> None:
    dashboard = load_module()
    valid = f"live_snapshot_123_{'a' * 64}_{'b' * 64}.json"
    assert dashboard.is_live_snapshot_bundle_filename(valid)
    for invalid in (
        f"live_snapshot_0_{'a' * 64}_{'b' * 64}.json",
        f"live_snapshot_123_{'A' * 64}_{'b' * 64}.json",
        f"live_snapshot_123_{'a' * 63}_{'b' * 64}.json",
        f"live_snapshot_123_{'a' * 64}_{'b' * 64}.json/extra",
        "live_snapshot_attacker.json",
    ):
        assert not dashboard.is_live_snapshot_bundle_filename(invalid)


def test_queue_authenticated_reorg_marker_environment_is_strict() -> None:
    dashboard = load_module()
    previous = os.environ.get("DEGEN_DOGS_CANONICAL_REORG_FROM_HASH")
    try:
        os.environ.pop("DEGEN_DOGS_CANONICAL_REORG_FROM_HASH", None)
        assert dashboard.canonical_reorg_marker_from_env() == ""
        marker = "0x" + "a" * 64
        os.environ["DEGEN_DOGS_CANONICAL_REORG_FROM_HASH"] = marker.upper().replace("0X", "0x")
        assert dashboard.canonical_reorg_marker_from_env() == marker
        os.environ["DEGEN_DOGS_CANONICAL_REORG_FROM_HASH"] = "0x1234"
        try:
            dashboard.canonical_reorg_marker_from_env()
        except AssertionError as exc:
            assert "canonical reorg marker" in str(exc)
        else:
            raise AssertionError("invalid queue reorg marker reached generated metrics")
    finally:
        if previous is None:
            os.environ.pop("DEGEN_DOGS_CANONICAL_REORG_FROM_HASH", None)
        else:
            os.environ["DEGEN_DOGS_CANONICAL_REORG_FROM_HASH"] = previous


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"build_dashboard_tests=pass count={len(tests)}")
