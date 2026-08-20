#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "build_live_snapshot_bundle.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("build_live_snapshot_bundle", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def assert_raises_contains(function: Any, needle: str) -> None:
    try:
        function()
    except AssertionError as exc:
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"expected AssertionError containing {needle!r}")


def snapshot_hash(block: int) -> str:
    return "0x" + f"{block:064x}"


def write_fixture(root: Path, *, block: int = 123, countdown: str = "01:00:00") -> None:
    block_hash = snapshot_hash(block)
    current = [
        {
            "token_id": 7,
            "latest_block": block,
            "latest_block_time_utc": "2026-08-21 10:00:00",
            "time_remaining": countdown,
        }
    ]
    metrics = [
        {"metric": "latest_block", "value": str(block)},
        {"metric": "snapshot_block_hash", "value": block_hash},
        {
            "metric": "onchain_verification_status",
            "value": "current_snapshot_cross_provider_verified",
        },
        {
            "metric": "onchain_verification_scope",
            "value": "snapshot_hash,contract_code,current_auction,recent_event_logs",
        },
        {"metric": "onchain_chain_id", "value": "8453"},
        {"metric": "snapshot_confirmations", "value": "1"},
        {"metric": "rpc_quorum_size", "value": "2"},
    ]
    sources = {
        "current_auction.json": current,
        "auction_feed.json": [{"dog": "Dog #7", "time_remaining": countdown}],
        "current_auction_bid_history.json": [{"token_id": 7, "bid_eth": 0.01}],
        "mission3_metrics.json": metrics,
    }
    for filename, payload in sources.items():
        write_json(root / "generated" / filename, payload)
        write_json(root / "public" / "generated" / filename, payload)
    status = {
        "schema_version": 1,
        "kind": "refresh_status",
        "latest_generated_block": block,
        "snapshot_block_hash": block_hash,
        "onchain_verification_status": "current_snapshot_cross_provider_verified",
        "onchain_chain_id": 8453,
    }
    write_json(root / "generated" / "refresh_status.json", status)
    write_json(root / "public" / "generated" / "refresh_status.json", status)
    write_json(
        root / "public" / "generated" / "unified_dog_search_index.json",
        [{"dog_id": 7, "mission": 3, "status": "live"}],
    )


def update_checkpoint(root: Path, block: int, *, countdown: str) -> None:
    block_hash = snapshot_hash(block)
    for directory in (root / "generated", root / "public" / "generated"):
        current_path = directory / "current_auction.json"
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current[0]["latest_block"] = block
        current[0]["time_remaining"] = countdown
        write_json(current_path, current)

        feed_path = directory / "auction_feed.json"
        feed = json.loads(feed_path.read_text(encoding="utf-8"))
        feed[0]["time_remaining"] = countdown
        write_json(feed_path, feed)

        metrics_path = directory / "mission3_metrics.json"
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        for row in metrics:
            if row["metric"] == "latest_block":
                row["value"] = str(block)
            elif row["metric"] == "snapshot_block_hash":
                row["value"] = block_hash
        write_json(metrics_path, metrics)

        status_path = directory / "refresh_status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["latest_generated_block"] = block
        status["snapshot_block_hash"] = block_hash
        write_json(status_path, status)


def test_builds_canonical_content_addressed_mirrors_and_archive_revision() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)

        status = bundle_module.build_live_snapshot_bundle(root=root)

        filename = status["live_snapshot_bundle"]
        match = bundle_module.BUNDLE_FILENAME_RE.fullmatch(filename)
        assert match
        assert match.group("block") == "123"
        assert match.group("block_hash") == snapshot_hash(123)[2:]
        assert match.group("content_hash") == status["live_snapshot_bundle_sha256"]
        generated = root / "generated" / filename
        public = root / "public" / "generated" / filename
        assert generated.read_bytes() == public.read_bytes()
        assert len(generated.read_bytes()) == status["live_snapshot_bundle_bytes"]
        assert hashlib.sha256(generated.read_bytes()).hexdigest() == status[
            "live_snapshot_bundle_sha256"
        ]
        parsed = json.loads(generated.read_text(encoding="utf-8"))
        assert generated.read_bytes() == bundle_module._canonical_json_bytes(parsed)
        assert parsed["current_auction"][0]["token_id"] == 7
        unified = root / "public" / "generated" / "unified_dog_search_index.json"
        assert status["unified_dog_search_sha256"] == hashlib.sha256(
            unified.read_bytes()
        ).hexdigest()
        assert status["unified_dog_search_bytes"] == unified.stat().st_size
        assert json.loads((root / "generated" / "refresh_status.json").read_text()) == status
        assert (
            root / "generated" / "refresh_status.json"
        ).read_bytes() == (
            root / "public" / "generated" / "refresh_status.json"
        ).read_bytes()
        assert bundle_module.validate_live_snapshot_bundle(root=root)["filename"] == filename

        # Idempotence must reuse the exact immutable object.
        repeated = bundle_module.build_live_snapshot_bundle(root=root)
        assert repeated == status
        assert len(list((root / "generated").glob("live_snapshot_*.json"))) == 1


def test_same_block_dynamic_change_creates_a_new_content_addressed_object() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        first = bundle_module.build_live_snapshot_bundle(root=root)

        # Countdown/USD/profile data can change at the same chain checkpoint.
        # The full content digest in the filename prevents an overwrite.
        update_checkpoint(root, 123, countdown="00:59:30")
        second = bundle_module.build_live_snapshot_bundle(root=root)

        assert second["live_snapshot_bundle"] != first["live_snapshot_bundle"]
        assert second["live_snapshot_bundle_sha256"] != first[
            "live_snapshot_bundle_sha256"
        ]
        assert (root / "generated" / first["live_snapshot_bundle"]).exists()
        assert (root / "generated" / second["live_snapshot_bundle"]).exists()


def test_rejects_split_source_before_advancing_status() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        original_status = (root / "generated" / "refresh_status.json").read_bytes()
        write_json(root / "public" / "generated" / "auction_feed.json", [{"dog": "tampered"}])

        assert_raises_contains(
            lambda: bundle_module.build_live_snapshot_bundle(root=root),
            "differs from generated",
        )
        assert (root / "generated" / "refresh_status.json").read_bytes() == original_status
        assert not list((root / "generated").glob("live_snapshot_*.json"))


def test_immutable_collision_and_tampering_fail_closed() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        status = bundle_module.build_live_snapshot_bundle(root=root)
        generated = root / "generated" / status["live_snapshot_bundle"]
        generated.write_text("{}\n", encoding="utf-8")

        assert_raises_contains(
            lambda: bundle_module.build_live_snapshot_bundle(root=root),
            "immutable live snapshot collision",
        )
        assert_raises_contains(
            lambda: bundle_module.validate_live_snapshot_bundle(root=root),
            "differs from generated",
        )


def test_validator_rejects_unified_index_revision_mismatch() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        bundle_module.build_live_snapshot_bundle(root=root)
        write_json(
            root / "public" / "generated" / "unified_dog_search_index.json",
            [{"dog_id": 999, "mission": 3}],
        )

        assert_raises_contains(
            lambda: bundle_module.validate_live_snapshot_bundle(root=root),
            "unified dog search SHA256 differs",
        )


def test_retention_keeps_the_newest_complete_versions_in_both_mirrors() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, block=100)
        statuses = [
            bundle_module.build_live_snapshot_bundle(
                root=root,
                retain=1,
                retention_grace_seconds=0,
            )
        ]
        for block in (101, 102, 103):
            update_checkpoint(root, block, countdown=f"00:{block}:00")
            statuses.append(
                bundle_module.build_live_snapshot_bundle(
                    root=root,
                    retain=1,
                    retention_grace_seconds=0,
                )
            )

        expected = {
            statuses[-2]["live_snapshot_bundle"],
            statuses[-1]["live_snapshot_bundle"],
        }
        generated = {
            path.name for path in (root / "generated").glob("live_snapshot_*.json")
        }
        public = {
            path.name
            for path in (root / "public" / "generated").glob("live_snapshot_*.json")
        }
        assert generated == expected
        assert public == expected
        bundle_module.validate_live_snapshot_bundle(root=root)


def test_rejects_unsafe_bundle_filename_in_retention_scope() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        bundle_module.build_live_snapshot_bundle(root=root)
        (root / "generated" / "live_snapshot_attacker.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        assert_raises_contains(
            lambda: bundle_module.build_live_snapshot_bundle(root=root),
            "unsafe live snapshot bundle filename",
        )


def test_retention_grace_protects_recent_cached_pointer_targets() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, block=100)
        statuses = [
            bundle_module.build_live_snapshot_bundle(
                root=root,
                retain=1,
                retention_grace_seconds=3600,
            )
        ]
        for block in (101, 102):
            update_checkpoint(root, block, countdown=f"00:{block}:00")
            statuses.append(
                bundle_module.build_live_snapshot_bundle(
                    root=root,
                    retain=1,
                    retention_grace_seconds=3600,
                )
            )
        assert len(list((root / "generated").glob("live_snapshot_*.json"))) == 3

        old_time = time.time() - 7200
        oldest = statuses[0]["live_snapshot_bundle"]
        for directory in (root / "generated", root / "public" / "generated"):
            os.utime(directory / oldest, (old_time, old_time))
        update_checkpoint(root, 103, countdown="00:59:00")
        bundle_module.build_live_snapshot_bundle(
            root=root,
            retain=1,
            retention_grace_seconds=3600,
        )
        assert not (root / "generated" / oldest).exists()
        assert not (root / "public" / "generated" / oldest).exists()


def test_rejects_symlinked_output_directory_chain() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        public = root / "public"
        public_real = root / "public_real"
        public.rename(public_real)
        public.symlink_to(public_real, target_is_directory=True)

        assert_raises_contains(
            lambda: bundle_module.build_live_snapshot_bundle(root=root),
            "unsafe directory component",
        )


def test_existing_immutable_symlink_is_never_followed() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root)
        status = bundle_module.build_live_snapshot_bundle(root=root)
        generated = root / "generated" / status["live_snapshot_bundle"]
        public = root / "public" / "generated" / status["live_snapshot_bundle"]
        generated.unlink()
        generated.symlink_to(public)

        assert_raises_contains(
            lambda: bundle_module.build_live_snapshot_bundle(root=root),
            "not a regular file",
        )


def test_retained_mirror_tampering_is_rejected_before_pointer_advance() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, block=100)
        first = bundle_module.build_live_snapshot_bundle(root=root)
        for directory in (root / "generated", root / "public" / "generated"):
            (directory / first["live_snapshot_bundle"]).write_text(
                "{}\n",
                encoding="utf-8",
            )
        update_checkpoint(root, 101, countdown="00:59:00")

        assert_raises_contains(
            lambda: bundle_module.build_live_snapshot_bundle(root=root),
            "retained live snapshot content hash differs",
        )
        status = json.loads(
            (root / "generated" / "refresh_status.json").read_text(encoding="utf-8")
        )
        assert status["live_snapshot_bundle"] == first["live_snapshot_bundle"]


def test_explicit_previous_pointer_survives_a_base_status_rewrite() -> None:
    bundle_module = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write_fixture(root, block=100)
        first = bundle_module.build_live_snapshot_bundle(
            root=root,
            retain=1,
            retention_grace_seconds=0,
        )
        update_checkpoint(root, 101, countdown="00:59:00")
        pointer_fields = {
            "live_snapshot_bundle",
            "live_snapshot_bundle_sha256",
            "live_snapshot_bundle_bytes",
            "live_snapshot_bundle_schema_version",
            "unified_dog_search_sha256",
            "unified_dog_search_bytes",
        }
        for directory in (root / "generated", root / "public" / "generated"):
            path = directory / "refresh_status.json"
            status = json.loads(path.read_text(encoding="utf-8"))
            write_json(path, {key: value for key, value in status.items() if key not in pointer_fields})

        second = bundle_module.build_live_snapshot_bundle(
            root=root,
            retain=1,
            retention_grace_seconds=0,
            previous_bundle=first["live_snapshot_bundle"],
        )
        assert first["live_snapshot_bundle"] != second["live_snapshot_bundle"]
        for directory in (root / "generated", root / "public" / "generated"):
            assert (directory / first["live_snapshot_bundle"]).exists()
            assert (directory / second["live_snapshot_bundle"]).exists()


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_")
    ]
    for test in tests:
        test()
    print(f"build_live_snapshot_bundle_tests=pass count={len(tests)}")
