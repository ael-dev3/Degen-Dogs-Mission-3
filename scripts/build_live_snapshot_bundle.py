#!/usr/bin/env python3
"""Build and validate the immutable dashboard live-snapshot bundle.

The four small, mutually dependent live dashboard datasets are published as a
single content-attested object.  The mutable refresh status points at that
object only after both generated mirrors contain identical bytes.  Older
objects remain available for a bounded period so a cached status document can
still resolve its immutable dependency during a deployment transition.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_SCHEMA_VERSION = 1
BUNDLE_KIND = "degen_dogs_live_snapshot"
DEFAULT_RETAINED_BUNDLES = 64
DEFAULT_RETENTION_GRACE_SECONDS = 24 * 60 * 60
MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_UNIFIED_INDEX_BYTES = 128 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BLOCK_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
BUNDLE_FILENAME_RE = re.compile(
    r"^live_snapshot_(?P<block>[1-9][0-9]*)_(?P<block_hash>[0-9a-f]{64})_"
    r"(?P<content_hash>[0-9a-f]{64})\.json$"
)
# These are exactly the four files fetched together by refreshLiveSurface in
# the generated client.  current_latest_bid.json is deliberately excluded: it
# is a build-time compatibility table, is never fetched by the live client,
# and validate_dashboard_consistency.py proves its value parity with the
# current auction/feed/history sources included here.
SOURCE_FIELDS = (
    "current_auction",
    "auction_feed",
    "current_auction_bid_history",
    "mission3_metrics",
)
BUNDLE_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "latest_generated_block",
        "snapshot_block_hash",
        *SOURCE_FIELDS,
    }
)


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise AssertionError(f"{label} missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AssertionError(f"{label} is not a regular file: {path}")
    return metadata


def _read_bytes(path: Path, label: str, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise AssertionError(f"{label} missing: {path}") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EFTYPE if hasattr(errno, "EFTYPE") else -1}:
            raise AssertionError(f"{label} is not a regular file: {path}") from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AssertionError(f"{label} is not a regular file: {path}")
        if metadata.st_size <= 0:
            raise AssertionError(f"{label} is empty: {path}")
        if metadata.st_size > max_bytes:
            raise AssertionError(
                f"{label} exceeds the {max_bytes}-byte safety limit: {path}"
            )
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(payload) != metadata.st_size
        or after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
    ):
        raise AssertionError(f"{label} changed while it was being read: {path}")
    return payload


def _decode_json(payload: bytes, label: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssertionError(f"{label} is not valid UTF-8 JSON") from exc


def _read_json_pair(root: Path, filename: str) -> tuple[Any, bytes]:
    generated = root / "generated" / filename
    public = root / "public" / "generated" / filename
    generated_bytes = _read_bytes(
        generated,
        f"generated/{filename}",
        max_bytes=MAX_SOURCE_BYTES,
    )
    public_bytes = _read_bytes(
        public,
        f"public/generated/{filename}",
        max_bytes=MAX_SOURCE_BYTES,
    )
    if generated_bytes != public_bytes:
        raise AssertionError(
            f"public/generated/{filename} differs from generated/{filename}"
        )
    return _decode_json(generated_bytes, f"generated/{filename}"), generated_bytes


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _status_json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_owned_root(root: Path) -> Path:
    try:
        canonical = root.resolve(strict=True)
    except FileNotFoundError as exc:
        raise AssertionError(f"live snapshot repository root is missing: {root}") from exc
    metadata = canonical.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise AssertionError("live snapshot repository root is not an owned directory")
    return canonical


def _ensure_output_directory(
    root: Path,
    path: Path,
    *,
    create: bool = True,
) -> None:
    root = _canonical_owned_root(root)
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AssertionError(f"live snapshot output escapes the repository: {path}") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                raise AssertionError(
                    f"live snapshot output directory is missing: {current}"
                ) from None
            try:
                current.mkdir(mode=0o755)
            except FileExistsError:
                pass
            metadata = current.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise AssertionError(
                f"live snapshot output has an unsafe directory component: {current}"
            )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(root: Path, path: Path, payload: bytes) -> None:
    _ensure_output_directory(root, path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_immutable(root: Path, path: Path, payload: bytes) -> None:
    _ensure_output_directory(root, path.parent)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    created: os.stat_result | None = None
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        existing = _read_bytes(
            path,
            "existing immutable live snapshot",
            max_bytes=MAX_BUNDLE_BYTES,
        )
        if existing != payload:
            raise AssertionError(
                f"immutable live snapshot collision for {path.name}; refusing to overwrite"
            )
        return
    try:
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise AssertionError(f"new immutable live snapshot is not regular: {path}")
        os.fchmod(descriptor, 0o644)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while creating immutable live snapshot")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if created is not None:
            try:
                current = path.lstat()
                if (
                    stat.S_ISREG(current.st_mode)
                    and current.st_dev == created.st_dev
                    and current.st_ino == created.st_ino
                ):
                    path.unlink()
                    _fsync_directory(path.parent)
            except FileNotFoundError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _strict_positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise AssertionError(f"{label} must be a positive JSON integer")
    return value


def _metrics_map(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list) or not rows:
        raise AssertionError("mission3_metrics live snapshot source is not a non-empty list")
    metrics: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("metric") in (None, ""):
            raise AssertionError("mission3_metrics live snapshot source contains an invalid row")
        key = str(row["metric"])
        if key in metrics:
            raise AssertionError(f"mission3_metrics contains duplicate metric {key!r}")
        metrics[key] = "" if row.get("value") is None else str(row.get("value"))
    return metrics


def _optional_block_hash(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not BLOCK_HASH_RE.fullmatch(value):
        raise AssertionError(f"{label} is not canonical")
    return value


def _validate_current_auction_coverage_row(row: dict[str, Any]) -> None:
    """Require the exact tuple needed by the private publication proof."""
    amount_wei = row.get("amount_wei")
    start_time_unix = row.get("start_time_unix")
    end_time_unix = row.get("end_time_unix")
    bidder_wallet = row.get("bidder_wallet")
    settled = row.get("settled")
    if not isinstance(amount_wei, str) or not DECIMAL_RE.fullmatch(amount_wei):
        raise AssertionError("current_auction coverage amount_wei is not canonical")
    if type(start_time_unix) is not int or start_time_unix < 1:
        raise AssertionError("current_auction coverage start_time_unix is invalid")
    if type(end_time_unix) is not int or end_time_unix < start_time_unix:
        raise AssertionError("current_auction coverage end_time_unix is invalid")
    if not isinstance(bidder_wallet, str) or not ADDRESS_RE.fullmatch(bidder_wallet):
        raise AssertionError("current_auction coverage bidder_wallet is invalid")
    if type(settled) is not int or settled not in {0, 1}:
        raise AssertionError("current_auction coverage settled flag is invalid")


def _load_source_state(root: Path) -> tuple[dict[str, Any], int, str, str | None]:
    sources: dict[str, Any] = {}
    for field in SOURCE_FIELDS:
        value, _payload = _read_json_pair(root, f"{field}.json")
        if not isinstance(value, list):
            raise AssertionError(f"{field}.json live snapshot source is not a list")
        sources[field] = value

    current_rows = sources["current_auction"]
    if len(current_rows) != 1 or not isinstance(current_rows[0], dict):
        raise AssertionError("current_auction live snapshot source must contain exactly one object")
    _validate_current_auction_coverage_row(current_rows[0])
    if not all(isinstance(row, dict) for field in SOURCE_FIELDS for row in sources[field]):
        raise AssertionError("live snapshot source arrays must contain only objects")

    metrics = _metrics_map(sources["mission3_metrics"])
    latest_text = metrics.get("latest_block", "")
    if not latest_text.isdigit() or latest_text.startswith("0"):
        raise AssertionError("mission3_metrics latest_block is not a canonical positive integer")
    latest_block = int(latest_text)
    if latest_block <= 0:
        raise AssertionError("mission3_metrics latest_block must be positive")
    if type(current_rows[0].get("latest_block")) is not int:
        raise AssertionError("current_auction latest_block must be a JSON integer")
    if current_rows[0]["latest_block"] != latest_block:
        raise AssertionError("current_auction latest_block differs from mission3_metrics")

    snapshot_block_hash = metrics.get("snapshot_block_hash", "")
    if not BLOCK_HASH_RE.fullmatch(snapshot_block_hash):
        raise AssertionError("mission3_metrics snapshot_block_hash is not canonical")
    if metrics.get("onchain_verification_status") != "current_snapshot_cross_provider_verified":
        raise AssertionError("mission3_metrics current snapshot is not cross-provider verified")
    if metrics.get("onchain_chain_id") != "8453":
        raise AssertionError("mission3_metrics live snapshot is not Base mainnet")
    try:
        confirmations = int(metrics.get("snapshot_confirmations", ""))
        quorum_size = int(metrics.get("rpc_quorum_size", ""))
    except ValueError as exc:
        raise AssertionError("mission3_metrics live snapshot verification counts are invalid") from exc
    if confirmations < 1 or quorum_size < 2:
        raise AssertionError("mission3_metrics live snapshot verification is below quorum")
    required_scope = {
        "snapshot_hash",
        "contract_code",
        "current_auction",
        "recent_event_logs",
    }
    actual_scope = {
        part.strip()
        for part in metrics.get("onchain_verification_scope", "").split(",")
        if part.strip()
    }
    if not required_scope.issubset(actual_scope):
        raise AssertionError("mission3_metrics live snapshot verification scope is incomplete")
    canonical_reorg_from_hash = _optional_block_hash(
        metrics.get("canonical_reorg_from_hash"),
        "mission3_metrics canonical_reorg_from_hash",
    )
    return sources, latest_block, snapshot_block_hash, canonical_reorg_from_hash


def _load_status_pair(root: Path) -> dict[str, Any]:
    status, _payload = _read_json_pair(root, "refresh_status.json")
    if not isinstance(status, dict):
        raise AssertionError("refresh_status live snapshot pointer source is not an object")
    return status


def _load_unified_revision(root: Path) -> tuple[str, int]:
    path = root / "public" / "generated" / "unified_dog_search_index.json"
    payload = _read_bytes(
        path,
        "public/generated/unified_dog_search_index.json",
        max_bytes=MAX_UNIFIED_INDEX_BYTES,
    )
    value = _decode_json(payload, "public/generated/unified_dog_search_index.json")
    if not isinstance(value, list):
        raise AssertionError("public unified dog search index is not a JSON list")
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _validate_status_against_sources(
    status: dict[str, Any],
    latest_block: int,
    snapshot_block_hash: str,
    canonical_reorg_from_hash: str | None,
) -> None:
    if status.get("kind") != "refresh_status":
        raise AssertionError("refresh_status kind is invalid before live snapshot publication")
    if type(status.get("latest_generated_block")) is not int:
        raise AssertionError("refresh_status latest_generated_block must be a JSON integer")
    if status["latest_generated_block"] != latest_block:
        raise AssertionError("refresh_status latest_generated_block differs from live snapshot sources")
    if status.get("snapshot_block_hash") != snapshot_block_hash:
        raise AssertionError("refresh_status snapshot_block_hash differs from live snapshot sources")
    status_reorg = _optional_block_hash(
        status.get("canonical_reorg_from_hash"),
        "refresh_status canonical_reorg_from_hash",
    )
    if status_reorg != canonical_reorg_from_hash:
        raise AssertionError(
            "refresh_status canonical_reorg_from_hash differs from live snapshot sources"
        )
    if status.get("onchain_verification_status") != "current_snapshot_cross_provider_verified":
        raise AssertionError("refresh_status current snapshot is not cross-provider verified")
    if status.get("onchain_chain_id") != 8453:
        raise AssertionError("refresh_status live snapshot is not Base mainnet")


def _bundle_filename(
    latest_block: int,
    snapshot_block_hash: str,
    content_hash: str,
) -> str:
    return (
        f"live_snapshot_{latest_block}_{snapshot_block_hash[2:]}_"
        f"{content_hash}.json"
    )


def _validate_retained_bundle_bytes(filename: str, payload: bytes) -> None:
    match = BUNDLE_FILENAME_RE.fullmatch(filename)
    if not match:
        raise AssertionError(f"unsafe retained live snapshot filename: {filename}")
    if hashlib.sha256(payload).hexdigest() != match.group("content_hash"):
        raise AssertionError(f"retained live snapshot content hash differs: {filename}")
    value = _decode_json(payload, f"retained live snapshot {filename}")
    if not isinstance(value, dict) or frozenset(value) != BUNDLE_FIELDS:
        raise AssertionError(f"retained live snapshot schema is invalid: {filename}")
    if _canonical_json_bytes(value) != payload:
        raise AssertionError(f"retained live snapshot is not canonical: {filename}")
    if value.get("schema_version") != BUNDLE_SCHEMA_VERSION or value.get("kind") != BUNDLE_KIND:
        raise AssertionError(f"retained live snapshot identity is invalid: {filename}")
    if value.get("latest_generated_block") != int(match.group("block")):
        raise AssertionError(f"retained live snapshot block differs: {filename}")
    if value.get("snapshot_block_hash") != f"0x{match.group('block_hash')}":
        raise AssertionError(f"retained live snapshot block hash differs: {filename}")


def _prune_old_bundles(
    root: Path,
    current_filename: str,
    previous_filename: str | None,
    retain: int,
    retention_grace_seconds: int,
) -> None:
    if type(retain) is not int or retain < 1 or retain > 4096:
        raise AssertionError("live snapshot retention must be an integer from 1 through 4096")
    if (
        type(retention_grace_seconds) is not int
        or retention_grace_seconds < 0
        or retention_grace_seconds > 7 * 24 * 60 * 60
    ):
        raise AssertionError(
            "live snapshot retention grace must be an integer from 0 through 604800 seconds"
        )
    directories = (root / "generated", root / "public" / "generated")
    names: set[str] = set()
    for directory in directories:
        _ensure_output_directory(root, directory)
        for path in directory.glob("live_snapshot_*.json"):
            if not BUNDLE_FILENAME_RE.fullmatch(path.name):
                raise AssertionError(f"unsafe live snapshot bundle filename: {path.name}")
            _require_regular_file(path, "retained live snapshot")
            names.add(path.name)
    mtimes = {
        name: max(
            (directory / name).lstat().st_mtime
            for directory in directories
            if (directory / name).exists() or (directory / name).is_symlink()
        )
        for name in names
    }
    ordered = sorted(
        names,
        key=lambda name: (
            mtimes[name],
            int(BUNDLE_FILENAME_RE.fullmatch(name).group("block")),  # type: ignore[union-attr]
            name,
        ),
        reverse=True,
    )
    # Always retain the new current object, the exact object referenced by the
    # prior status, and the newest fallback object. In addition, retain the
    # configured count and every object inside the grace window so cached
    # status documents cannot lose their pointer target.
    keep = set(ordered[: max(2, retain)])
    keep.add(current_filename)
    if previous_filename is not None:
        if not BUNDLE_FILENAME_RE.fullmatch(previous_filename):
            raise AssertionError("previous live snapshot bundle filename is unsafe")
        keep.add(previous_filename)
    grace_cutoff = time.time() - retention_grace_seconds
    if retention_grace_seconds:
        for name in names:
            mirror_mtimes = [
                (directory / name).lstat().st_mtime
                for directory in directories
                if (directory / name).exists() or (directory / name).is_symlink()
            ]
            if mirror_mtimes and max(mirror_mtimes) >= grace_cutoff:
                keep.add(name)
    for name in keep:
        generated = directories[0] / name
        public = directories[1] / name
        generated_bytes = _read_bytes(
            generated,
            "retained generated live snapshot",
            max_bytes=MAX_BUNDLE_BYTES,
        )
        public_bytes = _read_bytes(
            public,
            "retained public live snapshot",
            max_bytes=MAX_BUNDLE_BYTES,
        )
        if generated_bytes != public_bytes:
            raise AssertionError(f"retained live snapshot mirrors differ: {name}")
        _validate_retained_bundle_bytes(name, generated_bytes)
    for name in names - keep:
        for directory in directories:
            path = directory / name
            if path.exists() or path.is_symlink():
                _require_regular_file(path, "pruned live snapshot")
                path.unlink()
                _fsync_directory(directory)


def build_live_snapshot_bundle(
    root: Path = ROOT,
    *,
    retain: int = DEFAULT_RETAINED_BUNDLES,
    retention_grace_seconds: int = DEFAULT_RETENTION_GRACE_SECONDS,
    previous_bundle: str | None = None,
) -> dict[str, Any]:
    """Build the immutable bundle and atomically advance both status pointers."""
    root = _canonical_owned_root(Path(root))
    _ensure_output_directory(root, root / "generated")
    _ensure_output_directory(root, root / "public" / "generated")
    sources, latest_block, snapshot_block_hash, canonical_reorg_from_hash = _load_source_state(root)
    status = _load_status_pair(root)
    _validate_status_against_sources(
        status,
        latest_block,
        snapshot_block_hash,
        canonical_reorg_from_hash,
    )
    unified_sha256, unified_bytes = _load_unified_revision(root)

    bundle = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "kind": BUNDLE_KIND,
        "latest_generated_block": latest_block,
        "snapshot_block_hash": snapshot_block_hash,
        **sources,
    }
    bundle_bytes = _canonical_json_bytes(bundle)
    if len(bundle_bytes) > MAX_BUNDLE_BYTES:
        raise AssertionError("canonical live snapshot bundle exceeds its safety limit")
    digest = hashlib.sha256(bundle_bytes).hexdigest()
    filename = _bundle_filename(latest_block, snapshot_block_hash, digest)

    generated_bundle = root / "generated" / filename
    public_bundle = root / "public" / "generated" / filename
    # The public object is installed first.  Its status pointer is installed
    # last, so a crash cannot expose a pointer to an absent public bundle.
    _write_immutable(root, public_bundle, bundle_bytes)
    _write_immutable(root, generated_bundle, bundle_bytes)
    if _read_bytes(
        public_bundle,
        "new public live snapshot bundle",
        max_bytes=MAX_BUNDLE_BYTES,
    ) != _read_bytes(
        generated_bundle,
        "new generated live snapshot bundle",
        max_bytes=MAX_BUNDLE_BYTES,
    ):
        raise AssertionError("live snapshot bundle mirrors differ after atomic writes")

    _prune_old_bundles(
        root,
        filename,
        previous_bundle
        if previous_bundle is not None
        else (
            str(status["live_snapshot_bundle"])
            if status.get("live_snapshot_bundle") is not None
            else None
        ),
        retain,
        retention_grace_seconds,
    )
    updated_status = dict(status)
    updated_status.update(
        {
            "live_snapshot_bundle": filename,
            "live_snapshot_bundle_sha256": digest,
            "live_snapshot_bundle_bytes": len(bundle_bytes),
            "live_snapshot_bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "unified_dog_search_sha256": unified_sha256,
            "unified_dog_search_bytes": unified_bytes,
        }
    )
    status_bytes = _status_json_bytes(updated_status)
    # Write generated first and the browser-facing public pointer last.
    _atomic_write_bytes(root, root / "generated" / "refresh_status.json", status_bytes)
    _atomic_write_bytes(
        root,
        root / "public" / "generated" / "refresh_status.json",
        status_bytes,
    )
    validate_live_snapshot_bundle(root=root, status=updated_status)
    return updated_status


def validate_live_snapshot_bundle(
    root: Path = ROOT,
    *,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless the status pointer and all live bytes are coherent."""
    root = _canonical_owned_root(Path(root))
    _ensure_output_directory(root, root / "generated", create=False)
    _ensure_output_directory(
        root,
        root / "public" / "generated",
        create=False,
    )
    actual_status = _load_status_pair(root)
    if status is not None and actual_status != status:
        raise AssertionError("refresh_status changed during live snapshot validation")
    status = actual_status

    integer_fields = (
        "live_snapshot_bundle_bytes",
        "live_snapshot_bundle_schema_version",
        "unified_dog_search_bytes",
    )
    for field in integer_fields:
        _strict_positive_int(status.get(field), f"refresh_status {field}")
    if status["live_snapshot_bundle_schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise AssertionError("refresh_status live snapshot schema version is unsupported")

    filename = status.get("live_snapshot_bundle")
    if not isinstance(filename, str):
        raise AssertionError("refresh_status live_snapshot_bundle is not a filename")
    filename_match = BUNDLE_FILENAME_RE.fullmatch(filename)
    if not filename_match:
        raise AssertionError("refresh_status live_snapshot_bundle filename is unsafe")
    latest_block = _strict_positive_int(
        status.get("latest_generated_block"),
        "refresh_status latest_generated_block",
    )
    snapshot_block_hash = status.get("snapshot_block_hash")
    if not isinstance(snapshot_block_hash, str) or not BLOCK_HASH_RE.fullmatch(snapshot_block_hash):
        raise AssertionError("refresh_status snapshot_block_hash is not canonical")
    if int(filename_match.group("block")) != latest_block:
        raise AssertionError("live snapshot filename block differs from refresh_status")
    if filename_match.group("block_hash") != snapshot_block_hash[2:]:
        raise AssertionError("live snapshot filename hash differs from refresh_status")

    expected_digest = status.get("live_snapshot_bundle_sha256")
    if not isinstance(expected_digest, str) or not SHA256_RE.fullmatch(expected_digest):
        raise AssertionError("refresh_status live_snapshot_bundle_sha256 is invalid")
    if filename_match.group("content_hash") != expected_digest:
        raise AssertionError("live snapshot filename content hash differs from refresh_status")
    generated_path = root / "generated" / filename
    public_path = root / "public" / "generated" / filename
    generated_bytes = _read_bytes(
        generated_path,
        "generated live snapshot bundle",
        max_bytes=MAX_BUNDLE_BYTES,
    )
    public_bytes = _read_bytes(
        public_path,
        "public live snapshot bundle",
        max_bytes=MAX_BUNDLE_BYTES,
    )
    if generated_bytes != public_bytes:
        raise AssertionError("public live snapshot bundle differs from generated bundle")
    if len(generated_bytes) != status["live_snapshot_bundle_bytes"]:
        raise AssertionError("live snapshot bundle byte size differs from refresh_status")
    if hashlib.sha256(generated_bytes).hexdigest() != expected_digest:
        raise AssertionError("live snapshot bundle SHA256 differs from refresh_status")

    bundle = _decode_json(generated_bytes, "live snapshot bundle")
    if not isinstance(bundle, dict) or frozenset(bundle) != BUNDLE_FIELDS:
        raise AssertionError("live snapshot bundle schema fields are invalid")
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION or bundle.get("kind") != BUNDLE_KIND:
        raise AssertionError("live snapshot bundle schema identity is invalid")
    if bundle.get("latest_generated_block") != latest_block:
        raise AssertionError("live snapshot bundle block differs from refresh_status")
    if bundle.get("snapshot_block_hash") != snapshot_block_hash:
        raise AssertionError("live snapshot bundle hash differs from refresh_status")
    if _canonical_json_bytes(bundle) != generated_bytes:
        raise AssertionError("live snapshot bundle JSON is not canonical")

    sources, source_block, source_hash, source_reorg = _load_source_state(root)
    if source_block != latest_block or source_hash != snapshot_block_hash:
        raise AssertionError("live snapshot source checkpoint differs from refresh_status")
    if _optional_block_hash(
        status.get("canonical_reorg_from_hash"),
        "refresh_status canonical_reorg_from_hash",
    ) != source_reorg:
        raise AssertionError("live snapshot reorg marker differs from refresh_status")
    for field in SOURCE_FIELDS:
        if bundle.get(field) != sources[field]:
            raise AssertionError(f"live snapshot bundle {field} differs from legacy source")

    unified_sha256, unified_bytes = _load_unified_revision(root)
    expected_unified_sha256 = status.get("unified_dog_search_sha256")
    if not isinstance(expected_unified_sha256, str) or not SHA256_RE.fullmatch(
        expected_unified_sha256
    ):
        raise AssertionError("refresh_status unified_dog_search_sha256 is invalid")
    if expected_unified_sha256 != unified_sha256:
        raise AssertionError("unified dog search SHA256 differs from refresh_status")
    if status["unified_dog_search_bytes"] != unified_bytes:
        raise AssertionError("unified dog search byte size differs from refresh_status")

    return {
        "filename": filename,
        "sha256": expected_digest,
        "bytes": len(generated_bytes),
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "latest_generated_block": latest_block,
        "snapshot_block_hash": snapshot_block_hash,
        "unified_dog_search_sha256": unified_sha256,
        "unified_dog_search_bytes": unified_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retain",
        type=int,
        default=DEFAULT_RETAINED_BUNDLES,
        help="number of immutable live snapshots to retain in each mirror",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the existing pointer and bundle without writing",
    )
    parser.add_argument(
        "--retention-grace-seconds",
        type=int,
        default=DEFAULT_RETENTION_GRACE_SECONDS,
        help="minimum age before an otherwise old immutable snapshot may be pruned",
    )
    args = parser.parse_args()
    result = (
        validate_live_snapshot_bundle(root=ROOT)
        if args.validate_only
        else build_live_snapshot_bundle(
            root=ROOT,
            retain=args.retain,
            retention_grace_seconds=args.retention_grace_seconds,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
