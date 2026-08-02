#!/usr/bin/env python3
"""Health checks for the Degen Dogs Mission 3 archive outputs."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive" / "mission3"
DATA_DIR = ARCHIVE / "data"
DEFAULT_DB_PATH = DATA_DIR / "mission3_archive.sqlite"
DEFAULT_GENERATED_DIR = DATA_DIR / "generated"
DEFAULT_PUBLIC_DIR = ROOT / "public" / "generated" / "mission3"
INDEXER_PATH = ROOT / "scripts" / "archive_mission3_index.py"

SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
]


def load_indexer():
    spec = importlib.util.spec_from_file_location("archive_mission3_index", INDEXER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load archive_mission3_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def configured_paths(environment: dict[str, str] | None = None) -> tuple[Path, Path, Path]:
    env = os.environ if environment is None else environment
    db_path = Path(env.get("MISSION3_ARCHIVE_DB") or DEFAULT_DB_PATH).expanduser()
    generated_dir = Path(env.get("MISSION3_OUTPUT_DIR") or DEFAULT_GENERATED_DIR).expanduser()
    return db_path, generated_dir, DEFAULT_PUBLIC_DIR


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def check_timestamp_freshness(
    errors: list[str],
    label: str,
    value: Any,
    *,
    max_age_seconds: int,
    now: datetime | None = None,
) -> None:
    parsed = parse_utc(value)
    if parsed is None:
        errors.append(f"{label} is missing or invalid: {value!r}")
        return
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - parsed).total_seconds()
    if age_seconds < -300:
        errors.append(f"{label} is future-dated by {int(-age_seconds)} seconds")
    elif age_seconds > max_age_seconds:
        errors.append(
            f"{label} is stale: age_seconds={int(age_seconds)} max={max_age_seconds}"
        )


def check_head_lag(errors: list[str], latest_indexed: Any, safe_head: int, *, max_lag_blocks: int) -> None:
    try:
        latest = int(str(latest_indexed))
    except (TypeError, ValueError):
        errors.append(f"archive latest indexed block is invalid: {latest_indexed!r}")
        return
    lag = safe_head - latest
    if lag > max_lag_blocks:
        errors.append(f"archive head lag is too large: lag_blocks={lag} max={max_lag_blocks}")
    elif lag < -64:
        errors.append(f"archive latest indexed block is unexpectedly ahead of safe head: delta_blocks={-lag}")


def file_sha(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_row_count(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def json_row_count(path: Path) -> int:
    data = read_json(path)
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return len(data)
    raise AssertionError(f"JSON output is not list/dict: {path}")


def scan_secrets(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append(display_path(path))
                break
    return sorted(set(hits))


def check_db(errors: list[str], db_path: Path, *, max_age_seconds: int) -> dict[str, Any] | None:
    if not db_path.exists():
        errors.append(f"missing archive database: {display_path(db_path)}")
        return None
    conn = sqlite3.connect(db_path)
    state: dict[str, Any] | None = None
    try:
        quick_check = conn.execute("PRAGMA quick_check").fetchone()
        if not quick_check or str(quick_check[0]).lower() != "ok":
            errors.append(f"database quick_check failed: {quick_check!r}")
        counts = dict(conn.execute("SELECT metric, value FROM mission3_archive_metrics").fetchall())
        numeric_expectations = {
            "raw_logs": 1,
            "auctions_created": 1,
            "bids": 1,
        }
        for metric, minimum in numeric_expectations.items():
            try:
                value = int(str(counts.get(metric, "0")))
            except ValueError:
                errors.append(f"archive metric is not numeric: {metric}={counts.get(metric)!r}")
                continue
            if value < minimum:
                errors.append(f"archive metric too low: {metric}={value} < {minimum}")
        status = str(counts.get("status", ""))
        if status != "success":
            errors.append(f"archive state is not success: {status!r}")
        gaps = int(str(counts.get("unresolved_gaps", "0") or 0))
        if gaps:
            errors.append(f"archive has unresolved gaps: {gaps}")
        row = conn.execute(
            """SELECT latest_indexed_block, latest_indexed_block_time_utc,
                      latest_run_at_utc, status
               FROM mission3_index_state WHERE id = 'mission3'"""
        ).fetchone()
        if row:
            state = {
                "latest_indexed_block": row[0],
                "latest_indexed_block_time_utc": row[1],
                "latest_run_at_utc": row[2],
                "status": row[3],
            }
            check_timestamp_freshness(
                errors,
                "archive latest indexed block time",
                row[1],
                max_age_seconds=max_age_seconds,
            )
            check_timestamp_freshness(
                errors,
                "archive latest run time",
                row[2],
                max_age_seconds=max_age_seconds,
            )
        else:
            errors.append("archive index state row is missing")
        created_tokens = [row[0] for row in conn.execute("SELECT token_id FROM mission3_auction_created ORDER BY token_id")]
        if created_tokens:
            expected = set(range(int(created_tokens[0]), int(created_tokens[-1]) + 1))
            missing = sorted(expected.difference(int(token_id) for token_id in created_tokens))
            if missing:
                preview = ", ".join(str(item) for item in missing[:12])
                suffix = "..." if len(missing) > 12 else ""
                errors.append(f"auction-created token range has {len(missing)} missing token ids: {preview}{suffix}")
    except sqlite3.Error as exc:
        errors.append(f"database health query failed: {exc}")
    finally:
        conn.close()
    return state


def check_generated(
    errors: list[str],
    generated_dir: Path,
    *,
    require_generated: bool,
    max_age_seconds: int,
    db_state: dict[str, Any] | None,
) -> tuple[list[Path], dict[str, Any] | None]:
    manifest_path = generated_dir / "manifest.json"
    checked_paths: list[Path] = []
    if not manifest_path.exists():
        if require_generated:
            errors.append(f"missing archive manifest: {display_path(manifest_path)}")
        return checked_paths, None

    try:
        manifest = read_json(manifest_path)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"archive manifest could not be read: {exc}")
        return [manifest_path], None
    if not isinstance(manifest, dict):
        errors.append("archive manifest is not a JSON object")
        return [manifest_path], None
    if manifest.get("schema_version") != 1 or manifest.get("mission") != 3:
        errors.append("archive manifest schema_version/mission mismatch")
    check_timestamp_freshness(
        errors,
        "archive manifest generation time",
        manifest.get("generated_at_utc"),
        max_age_seconds=max_age_seconds,
    )
    if db_state is not None:
        manifest_state = manifest.get("index_state") or {}
        mismatched_state = [
            key for key, value in db_state.items() if manifest_state.get(key) != value
        ]
        if mismatched_state:
            errors.append(
                "archive manifest index_state differs from archive database state: "
                + ", ".join(mismatched_state)
            )

    checked_paths.append(manifest_path)
    for item in manifest.get("files", []):
        rel = item.get("path")
        if not rel:
            errors.append(f"manifest file entry missing path: {item}")
            continue
        raw_path = Path(str(rel))
        path = raw_path if raw_path.is_absolute() else ROOT / raw_path
        try:
            if not path.resolve().is_relative_to(generated_dir.resolve()):
                errors.append(f"manifest-listed file escapes generated directory: {rel}")
                continue
        except OSError as exc:
            errors.append(f"manifest-listed file path cannot be resolved: {rel}: {exc}")
            continue
        checked_paths.append(path)
        if not path.exists():
            errors.append(f"manifest-listed file missing: {rel}")
            continue
        expected_sha = item.get("sha256")
        if expected_sha and file_sha(path) != expected_sha:
            errors.append(f"sha mismatch for {rel}")
        expected_rows = item.get("rows")
        if isinstance(expected_rows, int):
            try:
                actual_rows = csv_row_count(path) if path.suffix == ".csv" else json_row_count(path)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"row-count check failed for {rel}: {exc}")
                continue
            if actual_rows != expected_rows:
                errors.append(f"row count mismatch for {rel}: manifest={expected_rows} actual={actual_rows}")

    counts = manifest.get("counts") or {}
    for key in ("raw_logs", "auctions_created", "latest_indexed_block", "status"):
        if key not in counts:
            errors.append(f"manifest counts missing {key}")
    return checked_paths, manifest


def check_public(
    errors: list[str],
    generated_dir: Path,
    public_dir: Path,
    *,
    private_manifest: dict[str, Any] | None,
    max_age_seconds: int,
) -> list[Path]:
    expected = {
        "mission3_dog_search_index.json": generated_dir / "mission3_dog_search_index.json",
        "mission3_archive_metrics.json": generated_dir / "mission3_archive_metrics.json",
    }
    checked: list[Path] = []
    public_manifest_path = public_dir / "archive_manifest.json"
    checked.append(public_manifest_path)
    if not public_manifest_path.exists():
        errors.append(f"missing public archive file: {display_path(public_manifest_path)}")
    else:
        try:
            public_manifest = read_json(public_manifest_path)
            if public_manifest.get("schema_version") != 1 or public_manifest.get("mission") != 3 or public_manifest.get("public") is not True:
                errors.append("public archive manifest schema_version/mission/public mismatch")
            check_timestamp_freshness(
                errors,
                "public archive manifest generation time",
                public_manifest.get("generated_at_utc"),
                max_age_seconds=max_age_seconds,
            )
            if private_manifest is not None:
                if public_manifest.get("generated_at_utc") != private_manifest.get("generated_at_utc"):
                    errors.append("public/private archive manifest generation times differ")
                if public_manifest.get("index_state") != private_manifest.get("index_state"):
                    errors.append("public/private archive manifest index states differ")
            serialized = json.dumps(public_manifest, sort_keys=True)
            forbidden = ["archive/mission3/data", "mission3_archive.sqlite", "raw_logs_ndjson", "mission3_raw_logs.ndjson"]
            leaked = [item for item in forbidden if item in serialized]
            if leaked:
                errors.append("public archive manifest exposes internal archive paths: " + ", ".join(leaked))
            for item in public_manifest.get("files", []):
                rel = str(item.get("path", ""))
                if not rel.startswith("generated/mission3/"):
                    errors.append(f"public manifest file path is not public-relative: {rel}")
                    continue
                target = ROOT / "public" / rel
                if not target.exists():
                    errors.append(f"public manifest-listed file missing: public/{rel}")
                elif item.get("sha256") and file_sha(target) != item.get("sha256"):
                    errors.append(f"public manifest sha mismatch for public/{rel}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"public archive manifest check failed: {exc}")
    for name, source in expected.items():
        public_path = public_dir / name
        checked.append(public_path)
        if not source.exists():
            continue
        if not public_path.exists():
            errors.append(f"missing public archive file: {display_path(public_path)}")
            continue
        if file_sha(public_path) != file_sha(source):
            errors.append(f"public archive copy differs from generated source: {display_path(public_path)}")
    return checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Mission 3 archive health.")
    parser.add_argument("--rpc", action="store_true", help="Also verify live Base RPC chain and contract code.")
    parser.add_argument("--allow-missing-generated", action="store_true", help="Do not fail if generated archive outputs do not exist yet.")
    parser.add_argument("--skip-db", action="store_true", help="Skip SQLite DB checks.")
    args = parser.parse_args(argv)

    errors: list[str] = []
    db_path, generated_dir, public_dir = configured_paths()
    max_age_seconds = max(
        300,
        min(int(os.environ.get("MISSION3_ARCHIVE_MAX_AGE_SECONDS", "10800")), 7 * 24 * 60 * 60),
    )
    max_head_lag_blocks = max(
        100,
        min(int(os.environ.get("MISSION3_ARCHIVE_MAX_HEAD_LAG_BLOCKS", "6000")), 1_000_000),
    )
    indexer = load_indexer()
    try:
        indexer.verify_config(check_rpc=args.rpc)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"config verification failed: {exc}")

    db_state = None if args.skip_db else check_db(errors, db_path, max_age_seconds=max_age_seconds)
    checked, private_manifest = check_generated(
        errors,
        generated_dir,
        require_generated=not args.allow_missing_generated,
        max_age_seconds=max_age_seconds,
        db_state=db_state,
    )
    checked.extend(
        check_public(
            errors,
            generated_dir,
            public_dir,
            private_manifest=private_manifest,
            max_age_seconds=max_age_seconds,
        )
    )
    if args.rpc and db_state is not None:
        try:
            check_head_lag(
                errors,
                db_state.get("latest_indexed_block"),
                indexer.verified_safe_head(),
                max_lag_blocks=max_head_lag_blocks,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"archive live head verification failed: {exc}")
    secret_hits = scan_secrets(checked)
    if secret_hits:
        errors.append("possible secret pattern in archive outputs: " + ", ".join(secret_hits))

    if errors:
        print("archive_health=failed")
        for error in errors:
            print(f"- {error}")
        return 1
    print("archive_health=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
