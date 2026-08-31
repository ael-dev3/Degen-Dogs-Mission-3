#!/usr/bin/env python3
"""Detached verification of one immutable Degen Dogs Pages publication."""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import runner_publication_state as publication_state


SCHEMA_VERSION = 1
CONFIG_EXIT = 64
STATUS_MAX_BYTES = 2 * 1024 * 1024
MIN_BUNDLE_BYTES = 1
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
DEFAULT_BUDGET_SECONDS = 300.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
TELEMETRY_FILENAME = "pages-verifier.jsonl"
USER_AGENT = "Degen-Dogs-Pages-Verifier/1"
SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
PRODUCTION_REPO_DIR = Path("/srv/degen-dogs/repo")
PRODUCTION_LOCK_DIR = Path("/var/cache/degen-dogs")
PRODUCTION_LOG_DIR = Path("/var/log/degen-dogs")
BUNDLE_FIELDS = frozenset({
    "schema_version",
    "kind",
    "latest_generated_block",
    "snapshot_block_hash",
    "current_auction",
    "auction_feed",
    "current_auction_bid_history",
    "mission3_metrics",
})

RAW_HOST = "raw.githubusercontent.com"
PAGES_HOST = "ael-dev3.github.io"
OWNER = "ael-dev3"
REPOSITORY = "Degen-Dogs-Mission-3"
RAW_STATUS_PATH = "public/generated/refresh_status.json"

_SHA40 = re.compile(r"[0-9a-f]{40}\Z")
_SHA64 = re.compile(r"[0-9a-f]{64}\Z")
_BLOCK_HASH = re.compile(r"0x[0-9a-f]{64}\Z")
_BUNDLE_NAME = re.compile(
    r"live_snapshot_(?P<block>[1-9][0-9]*)_"
    r"(?P<block_hash>[0-9a-f]{64})_(?P<digest>[0-9a-f]{64})\.json\Z"
)
_UTC_Z = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")

_RESULTS = {
    "proof_verified",
    "unresolved",
    "abandoned_newer_pending",
    "hard_failure",
}
_ERROR_CODES = {
    None,
    "configuration_invalid",
    "content_encoding",
    "content_length",
    "content_type",
    "http_status",
    "pending_proof_invalid",
    "pending_state_conflict",
    "raw_bundle_json_invalid",
    "raw_bundle_mismatch",
    "raw_bundle_noncanonical",
    "raw_bundle_sha256_mismatch",
    "raw_bundle_size_mismatch",
    "raw_status_json_invalid",
    "raw_status_mismatch",
    "redirect_or_final_url",
    "response_oversize",
    "telemetry_write_failed",
    "tls_failure",
    "transport_failure",
    "transport_timeout",
    "truncated_body",
    "url_policy",
    "verification_timeout",
}


class FetchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES and code is not None else "transport_failure"
        super().__init__(self.code)


class IntegrityError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES and code is not None else "pending_state_conflict"
        super().__init__(self.code)


class TelemetryError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class VerifierConfig:
    invocation_budget_seconds: float = DEFAULT_BUDGET_SECONDS
    pages_poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        values = (
            self.invocation_budget_seconds,
            self.pages_poll_interval_seconds,
            self.request_timeout_seconds,
        )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("timing values must be finite numbers")
        if not 0.1 <= float(self.invocation_budget_seconds) <= DEFAULT_BUDGET_SECONDS:
            raise ValueError("invocation budget is out of bounds")
        if not 0.05 <= float(self.pages_poll_interval_seconds) <= 60.0:
            raise ValueError("poll interval is out of bounds")
        if not 0.1 <= float(self.request_timeout_seconds) <= DEFAULT_REQUEST_TIMEOUT_SECONDS:
            raise ValueError("request timeout is out of bounds")


@dataclasses.dataclass(frozen=True)
class RunResult:
    exit_code: int
    result: str
    error_code: str | None
    generation: int | None
    commit_sha: str | None
    retry_count: int | None
    raw_verified: bool
    pages_verified: bool


@dataclasses.dataclass(frozen=True)
class RemoteTargets:
    raw_status: str
    raw_bundle: str
    pages_status: str
    pages_bundle: str


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise FetchError("redirect_or_final_url")


def build_production_tls_context() -> ssl.SSLContext:
    """Build a fixed system-trust context without environment-selected files."""
    if os.name != "posix":
        raise FetchError("configuration_invalid")
    try:
        details = SYSTEM_CA_BUNDLE.lstat()
    except OSError as exc:
        raise FetchError("configuration_invalid") from exc
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or details.st_nlink != 1
        or stat.S_IMODE(details.st_mode) & 0o022
    ):
        raise FetchError("configuration_invalid")
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cafile=str(SYSTEM_CA_BUNDLE))
    except Exception as exc:
        raise FetchError("configuration_invalid") from exc
    if context.keylog_filename is not None:
        raise FetchError("configuration_invalid")
    return context


def _default_opener() -> Any:
    try:
        context = build_production_tls_context()
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            RejectRedirectHandler(),
        )
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError("configuration_invalid") from exc


class _RequestDeadlineExpired(TimeoutError):
    pass


@contextlib.contextmanager
def _absolute_request_deadline(seconds: float) -> Any:
    """Enforce a real wall-clock deadline in the single-threaded POSIX service."""
    if os.name != "posix":
        yield
        return
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        raise _RequestDeadlineExpired()
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer[0] > 0:
        raise _RequestDeadlineExpired()
    previous_handler = signal.getsignal(signal.SIGALRM)

    def expired(_signum: int, _frame: Any) -> None:
        raise _RequestDeadlineExpired()

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous_handler)


class FixedHttpTransport:
    """Fixed-origin, no-redirect, bounded HTTP reader."""

    def __init__(
        self,
        *,
        opener: Any | None = None,
        cache_bust_factory: Callable[[], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._opener = opener if opener is not None else _default_opener()
        self._cache_bust_factory = cache_bust_factory or time.monotonic_ns
        self._monotonic = monotonic
        self._used_cache_busts: set[int] = set()

    def _next_cache_bust(self) -> int:
        try:
            value = self._cache_bust_factory()
        except Exception as exc:
            raise FetchError("transport_failure") from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise FetchError("transport_failure")
        while value in self._used_cache_busts:
            value += 1
        self._used_cache_busts.add(value)
        return value

    def fetch(
        self,
        url: str,
        resource: str,
        max_bytes: int,
        timeout: float,
        *,
        absolute_deadline: float | None = None,
    ) -> bytes:
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1 or max_bytes > MAX_BUNDLE_BYTES:
            raise FetchError("response_oversize")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= DEFAULT_REQUEST_TIMEOUT_SECONDS:
            raise FetchError("transport_timeout")
        try:
            started = float(self._monotonic())
        except Exception as exc:
            raise FetchError("transport_timeout") from exc
        if not math.isfinite(started):
            raise FetchError("transport_timeout")
        if absolute_deadline is None:
            request_deadline = started + float(timeout)
        else:
            if (
                isinstance(absolute_deadline, bool)
                or not isinstance(absolute_deadline, (int, float))
                or not math.isfinite(float(absolute_deadline))
            ):
                raise FetchError("transport_timeout")
            request_deadline = min(started + float(timeout), float(absolute_deadline))
        if urllib.parse.urlsplit(url).query:
            raise FetchError("url_policy")
        request_url = f"{url}?cache_bust={self._next_cache_bust()}"
        validate_remote_url(request_url, resource)
        request = urllib.request.Request(
            request_url,
            method="GET",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Cache-Control": "no-cache, no-store, max-age=0",
                "Pragma": "no-cache",
            },
        )
        expected_content_type = "text/plain" if resource.startswith("raw_") else "application/json"
        try:
            request_timeout = request_deadline - float(self._monotonic())
            if not math.isfinite(request_timeout) or request_timeout <= 0:
                raise FetchError("transport_timeout")
            with _absolute_request_deadline(request_timeout):
                with self._opener.open(request, timeout=request_timeout) as response:
                    status_code = getattr(response, "status", None)
                    if status_code is None and hasattr(response, "getcode"):
                        status_code = response.getcode()
                    if status_code != 200:
                        raise FetchError("http_status")
                    if response.geturl() != request_url:
                        raise FetchError("redirect_or_final_url")
                    content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
                    if content_type != expected_content_type:
                        raise FetchError("content_type")
                    content_encoding = str(response.headers.get("Content-Encoding", "")).strip().lower()
                    if content_encoding not in {"", "identity"}:
                        raise FetchError("content_encoding")
                    declared_text = response.headers.get("Content-Length")
                    declared: int | None = None
                    if declared_text is not None:
                        declared_text = str(declared_text).strip()
                        if re.fullmatch(r"(?:0|[1-9][0-9]*)", declared_text) is None:
                            raise FetchError("content_length")
                        declared = int(declared_text)
                        if declared > max_bytes:
                            raise FetchError("content_length")
                    chunks: list[bytes] = []
                    total = 0
                    reader = getattr(response, "read1", response.read)
                    while True:
                        if self._monotonic() >= request_deadline:
                            raise FetchError("transport_timeout")
                        chunk = reader(min(64 * 1024, max_bytes + 1 - total))
                        if self._monotonic() > request_deadline:
                            raise FetchError("transport_timeout")
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError("response_oversize")
                        chunks.append(chunk)
                    if declared is not None and total != declared:
                        raise FetchError("truncated_body")
                    return b"".join(chunks)
        except FetchError:
            raise
        except urllib.error.HTTPError as exc:
            raise FetchError("http_status") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise FetchError("tls_failure") from exc
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise FetchError("transport_timeout") from exc
            raise FetchError("transport_failure") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise FetchError("transport_timeout") from exc
        except ssl.SSLError as exc:
            raise FetchError("tls_failure") from exc
        except Exception as exc:
            raise FetchError("transport_failure") from exc


def validate_remote_url(url: str, resource: str) -> None:
    if resource not in {"raw_status", "raw_bundle", "pages_status", "pages_bundle"}:
        raise FetchError("url_policy")
    if not isinstance(url, str) or len(url) > 2048 or "%" in url:
        raise FetchError("url_policy")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise FetchError("url_policy") from exc
    expected_host = RAW_HOST if resource.startswith("raw_") else PAGES_HOST
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.netloc != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise FetchError("url_policy")
    query_match = re.fullmatch(r"cache_bust=([0-9]+)", parsed.query)
    if query_match is None:
        raise FetchError("url_policy")
    if resource == "pages_status":
        expected_path = f"/{REPOSITORY}/generated/refresh_status.json"
        valid_path = parsed.path == expected_path
    elif resource == "pages_bundle":
        prefix = f"/{REPOSITORY}/generated/"
        valid_path = parsed.path.startswith(prefix) and _BUNDLE_NAME.fullmatch(parsed.path[len(prefix):]) is not None
    else:
        prefix_match = re.fullmatch(
            rf"/{OWNER}/{REPOSITORY}/(?P<commit>[0-9a-f]{{40}})/(?P<path>.+)",
            parsed.path,
        )
        valid_path = prefix_match is not None
        if prefix_match is not None:
            path = prefix_match.group("path")
            valid_path = (
                path == RAW_STATUS_PATH
                if resource == "raw_status"
                else path.startswith("public/generated/")
                and _BUNDLE_NAME.fullmatch(path[len("public/generated/"):]) is not None
            )
    if not valid_path or any(segment in {"", ".", ".."} for segment in parsed.path.split("/")[1:]):
        raise FetchError("url_policy")


def _strict_positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _parse_utc(value: str) -> dt.datetime:
    if not isinstance(value, str) or _UTC_Z.fullmatch(value) is None:
        raise IntegrityError("pending_state_conflict")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise IntegrityError("pending_state_conflict") from exc


def _format_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decode_strict_json(raw: bytes, error_code: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def constant(_value: str) -> Any:
        raise ValueError("non-finite")

    def finite_float(value: str) -> float:
        decoded = float(value)
        if not math.isfinite(decoded):
            raise ValueError("non-finite")
        return decoded

    try:
        text = raw.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise IntegrityError(error_code) from exc


def validate_pending_proof(record: dict[str, Any]) -> str:
    if record.get("raw_status_path") != RAW_STATUS_PATH:
        raise IntegrityError("pending_proof_invalid")
    raw_bundle_path = record.get("raw_bundle_path")
    prefix = "public/generated/"
    if not isinstance(raw_bundle_path, str) or not raw_bundle_path.startswith(prefix):
        raise IntegrityError("pending_proof_invalid")
    filename = raw_bundle_path[len(prefix):]
    match = _BUNDLE_NAME.fullmatch(filename)
    if match is None:
        raise IntegrityError("pending_proof_invalid")
    size = record.get("expected_bundle_bytes")
    if not _strict_positive_int(size) or not MIN_BUNDLE_BYTES <= size <= MAX_BUNDLE_BYTES:
        raise IntegrityError("pending_proof_invalid")
    block = record.get("expected_block_number")
    block_hash = record.get("expected_block_hash")
    digest = record.get("expected_bundle_sha256")
    if (
        not _strict_positive_int(block)
        or not isinstance(block_hash, str)
        or _BLOCK_HASH.fullmatch(block_hash) is None
        or not isinstance(digest, str)
        or _SHA64.fullmatch(digest) is None
        or match.group("block") != str(block)
        or match.group("block_hash") != block_hash[2:]
        or match.group("digest") != digest
    ):
        raise IntegrityError("pending_proof_invalid")
    commit = record.get("commit_sha")
    if not isinstance(commit, str) or _SHA40.fullmatch(commit) is None:
        raise IntegrityError("pending_proof_invalid")
    return filename


def build_targets(record: dict[str, Any]) -> RemoteTargets:
    filename = validate_pending_proof(record)
    commit = record["commit_sha"]
    raw_base = f"https://{RAW_HOST}/{OWNER}/{REPOSITORY}/{commit}/"
    pages_base = f"https://{PAGES_HOST}/{REPOSITORY}/generated/"
    return RemoteTargets(
        raw_status=raw_base + RAW_STATUS_PATH,
        raw_bundle=raw_base + record["raw_bundle_path"],
        pages_status=pages_base + "refresh_status.json",
        pages_bundle=pages_base + filename,
    )


def validate_raw_status(raw: bytes, record: dict[str, Any], filename: str) -> dict[str, Any]:
    value = _decode_strict_json(raw, "raw_status_json_invalid")
    if not isinstance(value, dict):
        raise IntegrityError("raw_status_json_invalid")
    expected = {
        "kind": "refresh_status",
        "live_snapshot_bundle": filename,
        "live_snapshot_bundle_sha256": record["expected_bundle_sha256"],
        "live_snapshot_bundle_bytes": record["expected_bundle_bytes"],
        "live_snapshot_bundle_schema_version": SCHEMA_VERSION,
        "latest_generated_block": record["expected_block_number"],
        "snapshot_block_hash": record["expected_block_hash"],
        "schema_version": SCHEMA_VERSION,
    }
    if any(value.get(key) != wanted for key, wanted in expected.items()):
        raise IntegrityError("raw_status_mismatch")
    if not _strict_positive_int(value.get("live_snapshot_bundle_bytes")):
        raise IntegrityError("raw_status_mismatch")
    for key in ("live_snapshot_bundle_schema_version", "latest_generated_block", "schema_version"):
        if type(value.get(key)) is not int:
            raise IntegrityError("raw_status_mismatch")
    return value


def validate_raw_bundle(raw: bytes, record: dict[str, Any]) -> dict[str, Any]:
    if len(raw) != record["expected_bundle_bytes"]:
        raise IntegrityError("raw_bundle_size_mismatch")
    if hashlib.sha256(raw).hexdigest() != record["expected_bundle_sha256"]:
        raise IntegrityError("raw_bundle_sha256_mismatch")
    value = _decode_strict_json(raw, "raw_bundle_json_invalid")
    if not isinstance(value, dict):
        raise IntegrityError("raw_bundle_json_invalid")
    try:
        canonical = (
            json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntegrityError("raw_bundle_json_invalid") from exc
    if canonical != raw:
        raise IntegrityError("raw_bundle_noncanonical")
    if frozenset(value) != BUNDLE_FIELDS:
        raise IntegrityError("raw_bundle_mismatch")
    expected = {
        "kind": "degen_dogs_live_snapshot",
        "schema_version": SCHEMA_VERSION,
        "latest_generated_block": record["expected_block_number"],
        "snapshot_block_hash": record["expected_block_hash"],
    }
    if any(value.get(key) != wanted for key, wanted in expected.items()):
        raise IntegrityError("raw_bundle_mismatch")
    if type(value.get("schema_version")) is not int or type(value.get("latest_generated_block")) is not int:
        raise IntegrityError("raw_bundle_mismatch")
    return value


_TELEMETRY_KEYS = {
    "schema_version",
    "timestamp_utc",
    "result",
    "error_code",
    "generation",
    "commit_sha",
    "expected_block_number",
    "retry_count",
    "duration_seconds",
    "raw_verified",
    "pages_verified",
}


def _validate_telemetry_row(row: dict[str, Any]) -> bytes:
    if not isinstance(row, dict) or set(row) != _TELEMETRY_KEYS:
        raise TelemetryError("telemetry shape")
    if row["schema_version"] != SCHEMA_VERSION or row["result"] not in _RESULTS or row["error_code"] not in _ERROR_CODES:
        raise TelemetryError("telemetry enum")
    if not isinstance(row["timestamp_utc"], str) or _UTC_Z.fullmatch(row["timestamp_utc"]) is None:
        raise TelemetryError("telemetry time")
    if not _strict_positive_int(row["generation"]):
        raise TelemetryError("telemetry generation")
    if not isinstance(row["commit_sha"], str) or _SHA40.fullmatch(row["commit_sha"]) is None:
        raise TelemetryError("telemetry commit")
    if not _strict_positive_int(row["expected_block_number"]):
        raise TelemetryError("telemetry block")
    if type(row["retry_count"]) is not int or row["retry_count"] < 0:
        raise TelemetryError("telemetry retry")
    if isinstance(row["duration_seconds"], bool) or not isinstance(row["duration_seconds"], (int, float)):
        raise TelemetryError("telemetry duration")
    if not math.isfinite(float(row["duration_seconds"])) or row["duration_seconds"] < 0:
        raise TelemetryError("telemetry duration")
    if type(row["raw_verified"]) is not bool or type(row["pages_verified"]) is not bool:
        raise TelemetryError("telemetry flags")
    data = (json.dumps(row, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(data) > 4096:
        raise TelemetryError("telemetry size")
    return data


def _validate_log_identity(directory_fd: int, descriptor: int) -> os.stat_result:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(
            TELEMETRY_FILENAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise TelemetryError("telemetry identity") from exc
    for details in (opened, named):
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
        ):
            raise TelemetryError("telemetry metadata")
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise TelemetryError("telemetry identity")
    return opened


def append_private_telemetry(log_dir: os.PathLike[str] | str, row: dict[str, Any]) -> None:
    if os.name != "posix":
        raise TelemetryError("telemetry requires POSIX")
    import fcntl
    import runner_path_security

    data = _validate_telemetry_row(row)
    directory = Path(os.path.abspath(os.fspath(log_dir)))
    try:
        directory_fd = runner_path_security.open_secure_directory(
            directory,
            create=False,
            private=False,
        )
    except Exception as exc:
        raise TelemetryError("telemetry directory") from exc
    descriptor: int | None = None
    try:
        directory_details = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory_details.st_mode)
            or directory_details.st_uid != os.getuid()
            or stat.S_IMODE(directory_details.st_mode) != 0o700
        ):
            raise TelemetryError("telemetry directory metadata")
        created = False
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(
                TELEMETRY_FILENAME,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(
                TELEMETRY_FILENAME,
                flags,
                dir_fd=directory_fd,
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _validate_log_identity(directory_fd, descriptor)
        written = os.write(descriptor, data)
        if written != len(data):
            raise TelemetryError("telemetry short write")
        _validate_log_identity(directory_fd, descriptor)
        os.fsync(descriptor)
        _validate_log_identity(directory_fd, descriptor)
        if created:
            os.fsync(directory_fd)
            _validate_log_identity(directory_fd, descriptor)
    except TelemetryError:
        raise
    except OSError as exc:
        raise TelemetryError("telemetry write") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory_fd)


def _telemetry_row(
    record: dict[str, Any],
    *,
    timestamp_utc: str,
    result: str,
    error_code: str | None,
    duration_seconds: float,
    raw_verified: bool,
    pages_verified: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp_utc": timestamp_utc,
        "result": result,
        "error_code": error_code,
        "generation": record["generation"],
        "commit_sha": record["commit_sha"],
        "expected_block_number": record["expected_block_number"],
        "retry_count": record["retry_count"],
        "duration_seconds": round(max(0.0, duration_seconds), 3),
        "raw_verified": raw_verified,
        "pages_verified": pages_verified,
    }


def _same_attempt(captured: Any, current: Any) -> str:
    if current is None:
        return "absent"
    captured_record = captured.record
    current_record = current.record
    if current_record["generation"] > captured_record["generation"]:
        return "newer"
    if current_record["generation"] < captured_record["generation"]:
        raise IntegrityError("pending_state_conflict")
    if current.proof_fingerprint != captured.proof_fingerprint:
        raise IntegrityError("pending_state_conflict")
    if (
        current_record["retry_count"] < captured_record["retry_count"]
        or current_record["retry_deadline_utc"] < captured_record["retry_deadline_utc"]
    ):
        raise IntegrityError("pending_state_conflict")
    return "same"


def _backoff_seconds(retry_count: int) -> int:
    return min(15 * 60, 60 * (2 ** min(retry_count, 4)))


def run_once(
    lock_dir: os.PathLike[str] | str,
    log_dir: os.PathLike[str] | str,
    *,
    state_api: Any = publication_state,
    transport: Any | None = None,
    telemetry_writer: Callable[[os.PathLike[str] | str, dict[str, Any]], None] = append_private_telemetry,
    utc_now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    config: VerifierConfig = VerifierConfig(),
) -> RunResult:
    start = monotonic()
    client = transport
    raw_verified = False
    pages_verified = False

    def result(exit_code: int, name: str, error: str | None, record: dict[str, Any] | None) -> RunResult:
        return RunResult(
            exit_code=exit_code,
            result=name,
            error_code=error,
            generation=record.get("generation") if record else None,
            commit_sha=record.get("commit_sha") if record else None,
            retry_count=record.get("retry_count") if record else None,
            raw_verified=raw_verified,
            pages_verified=pages_verified,
        )

    try:
        captured = state_api.read_pending_with_digest(Path(lock_dir))
    except Exception:
        return result(1, "hard_failure", "pending_state_conflict", None)
    if captured is None:
        return result(0, "idle", None, None)
    record = dict(captured.record)

    def emit(row_result: str, error_code: str | None) -> bool:
        try:
            timestamp = _format_utc(utc_now())
            telemetry_writer(
                log_dir,
                _telemetry_row(
                    record,
                    timestamp_utc=timestamp,
                    result=row_result,
                    error_code=error_code,
                    duration_seconds=monotonic() - start,
                    raw_verified=raw_verified,
                    pages_verified=pages_verified,
                ),
            )
            return True
        except Exception:
            return False

    def abandoned() -> RunResult:
        if not emit("abandoned_newer_pending", None):
            return result(1, "hard_failure", "telemetry_write_failed", record)
        return result(0, "abandoned_newer_pending", None, record)

    def hard(error_code: str) -> RunResult:
        if not emit("hard_failure", error_code):
            return result(1, "hard_failure", "telemetry_write_failed", record)
        return result(1, "hard_failure", error_code, record)

    def checkpoint() -> RunResult | None:
        try:
            current = state_api.read_pending_with_digest(Path(lock_dir))
            disposition = _same_attempt(captured, current)
        except Exception:
            return hard("pending_state_conflict")
        if disposition == "newer":
            return abandoned()
        if disposition == "absent":
            return result(0, "already_resolved", None, record)
        return None

    try:
        filename = validate_pending_proof(record)
        targets = build_targets(record)
        pushed_at = _parse_utc(record["push_completed_at_utc"])
        retry_deadline = _parse_utc(record["retry_deadline_utc"])
    except IntegrityError as exc:
        return hard(exc.code)
    except Exception:
        return hard("pending_proof_invalid")

    now = utc_now()
    if now.tzinfo is None:
        return hard("pending_state_conflict")
    now = now.astimezone(dt.timezone.utc)
    if pushed_at > now + dt.timedelta(seconds=30):
        return hard("pending_state_conflict")
    if record["retry_count"] > 0:
        if retry_deadline > now + dt.timedelta(minutes=15):
            return hard("pending_state_conflict")
        if now < retry_deadline:
            return result(0, "retry_not_due", None, record)
    budget = float(config.invocation_budget_seconds)
    if record["retry_count"] == 0:
        budget = min(budget, max(0.0, (retry_deadline - now).total_seconds()))
    absolute_deadline = start + budget

    def remaining() -> float:
        return max(0.0, absolute_deadline - monotonic())

    def bounded_sleep() -> RunResult | None:
        stopped = checkpoint()
        if stopped is not None:
            return stopped
        wait = min(float(config.pages_poll_interval_seconds), remaining())
        if wait > 0:
            sleep(wait)
        return checkpoint()

    last_error = "verification_timeout"

    def fetch_until(url: str, resource: str, cap: int) -> tuple[bytes | None, RunResult | None]:
        nonlocal last_error
        while remaining() > 0:
            stopped = checkpoint()
            if stopped is not None:
                return None, stopped
            timeout = min(float(config.request_timeout_seconds), remaining())
            if timeout <= 0:
                break
            try:
                body = client.fetch(
                    url,
                    resource,
                    cap,
                    timeout,
                    absolute_deadline=absolute_deadline,
                )
            except FetchError as exc:
                last_error = exc.code
            except Exception:
                last_error = "transport_failure"
            else:
                stopped = checkpoint()
                if stopped is not None:
                    return None, stopped
                return body, None
            stopped = checkpoint()
            if stopped is not None:
                return None, stopped
            stopped = bounded_sleep()
            if stopped is not None:
                return None, stopped
        return None, None

    def unresolved() -> RunResult:
        if not emit("unresolved", last_error):
            return result(1, "hard_failure", "telemetry_write_failed", record)
        try:
            fresh = state_api.read_pending_with_digest(Path(lock_dir))
            disposition = _same_attempt(captured, fresh)
        except Exception:
            return result(1, "hard_failure", "pending_state_conflict", record)
        if disposition == "newer":
            return abandoned()
        if disposition == "absent":
            return result(0, "already_resolved", None, record)
        replacement = dict(fresh.record)
        fresh_now = utc_now().astimezone(dt.timezone.utc)
        current_deadline = _parse_utc(replacement["retry_deadline_utc"])
        replacement["retry_count"] += 1
        if not (fresh.record["retry_count"] == 0 and current_deadline > fresh_now):
            replacement["retry_deadline_utc"] = _format_utc(
                fresh_now + dt.timedelta(seconds=_backoff_seconds(fresh.record["retry_count"]))
            )

        def retry_race_outcome() -> RunResult:
            try:
                raced = state_api.read_pending_with_digest(Path(lock_dir))
                raced_disposition = _same_attempt(captured, raced)
            except Exception:
                return result(1, "hard_failure", "pending_state_conflict", record)
            if raced_disposition == "newer":
                return abandoned()
            if raced_disposition == "absent":
                return result(0, "already_resolved", None, record)
            if (
                raced.record["retry_count"] >= fresh.record["retry_count"]
                and raced.record["retry_deadline_utc"] >= fresh.record["retry_deadline_utc"]
                and (
                    raced.record["retry_count"] > fresh.record["retry_count"]
                    or raced.record["retry_deadline_utc"] > fresh.record["retry_deadline_utc"]
                )
            ):
                # Another verifier already advanced this exact immutable proof.
                return result(2, "unresolved_retry_scheduled", last_error, raced.record)
            return result(1, "hard_failure", "pending_state_conflict", record)

        try:
            wrote = state_api.cas_write_pending(
                Path(lock_dir),
                fresh.record["generation"],
                fresh.record["commit_sha"],
                replacement,
            )
        except Exception:
            return retry_race_outcome()
        if not wrote:
            return retry_race_outcome()
        return result(2, "unresolved_retry_scheduled", last_error, replacement)

    if remaining() <= 0:
        return unresolved()

    if client is None:
        # Keep idle/not-due invocations independent of remote TLS setup.  A
        # production trust-store failure is allowed to propagate to ``main``
        # as a distinct configuration exit rather than masquerading as state.
        client = FixedHttpTransport(monotonic=monotonic)

    raw_status, stopped = fetch_until(targets.raw_status, "raw_status", STATUS_MAX_BYTES)
    if stopped is not None:
        return stopped
    if raw_status is None:
        return unresolved()
    try:
        validate_raw_status(raw_status, record, filename)
    except IntegrityError as exc:
        return hard(exc.code)

    raw_bundle, stopped = fetch_until(targets.raw_bundle, "raw_bundle", record["expected_bundle_bytes"])
    if stopped is not None:
        return stopped
    if raw_bundle is None:
        return unresolved()
    try:
        validate_raw_bundle(raw_bundle, record)
    except IntegrityError as exc:
        return hard(exc.code)
    raw_verified = True

    while remaining() > 0:
        pages_status, stopped = fetch_until(targets.pages_status, "pages_status", STATUS_MAX_BYTES)
        if stopped is not None:
            return stopped
        if pages_status is None:
            break
        if pages_status != raw_status:
            last_error = "verification_timeout"
            stopped = bounded_sleep()
            if stopped is not None:
                return stopped
            continue
        stopped = checkpoint()
        if stopped is not None:
            return stopped
        pages_bundle, stopped = fetch_until(
            targets.pages_bundle,
            "pages_bundle",
            record["expected_bundle_bytes"],
        )
        if stopped is not None:
            return stopped
        if pages_bundle is None:
            break
        if pages_bundle != raw_bundle:
            last_error = "verification_timeout"
            stopped = bounded_sleep()
            if stopped is not None:
                return stopped
            continue
        pages_verified = True
        break
    if not pages_verified:
        return unresolved()

    if not emit("proof_verified", None):
        return result(1, "hard_failure", "telemetry_write_failed", record)
    stopped = checkpoint()
    if stopped is not None:
        return stopped
    verified_at = _format_utc(utc_now())
    try:
        finalized = state_api.finalize_verified_pending(Path(lock_dir), captured, verified_at)
    except Exception:
        return result(1, "hard_failure", "pending_state_conflict", record)
    if finalized is state_api.PendingFinalizeResult.CLEARED:
        return result(0, "verified_cleared", None, record)
    if finalized is state_api.PendingFinalizeResult.BLOCKED_MATCHING_JOURNAL:
        return result(2, "verified_waiting_for_journal", None, record)
    try:
        current = state_api.read_pending_with_digest(Path(lock_dir))
        disposition = _same_attempt(captured, current)
    except Exception:
        return result(1, "hard_failure", "pending_state_conflict", record)
    if disposition == "newer":
        return abandoned()
    if disposition == "absent":
        return result(0, "already_resolved", None, record)
    return result(1, "hard_failure", "pending_state_conflict", record)


class _SanitizedArgumentError(RuntimeError):
    pass


class _SanitizedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _SanitizedArgumentError()


def _parser() -> argparse.ArgumentParser:
    parser = _SanitizedArgumentParser(description="Verify one fixed pending Pages deployment")
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--poll-interval-seconds", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--request-timeout-seconds", type=float, default=DEFAULT_REQUEST_TIMEOUT_SECONDS)
    return parser


def cli_option_strings() -> set[str]:
    return {option for action in _parser()._actions for option in action.option_strings}


def _production_directory(
    environment_name: str,
    expected: Path,
    *,
    private: bool,
) -> Path:
    value = os.environ.get(environment_name)
    if (
        not value
        or value != os.fspath(expected)
        or "\x00" in value
        or not value.startswith("/")
        or os.path.normpath(value) != value
    ):
        raise ValueError("invalid directory")
    real = os.path.realpath(value)
    if real == "/mnt" or real.startswith("/mnt/"):
        raise ValueError("DrvFS directory")
    if real != value:
        raise ValueError("non-canonical directory")
    import runner_path_security

    try:
        descriptor = runner_path_security.open_secure_directory(
            value,
            create=False,
            private=False,
        )
    except (OSError, runner_path_security.SecurePathError) as exc:
        raise ValueError("unsafe directory traversal") from exc
    try:
        details = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("invalid directory")
    mode = stat.S_IMODE(details.st_mode)
    if private:
        if details.st_uid != os.getuid() or mode != 0o700:
            raise ValueError("unsafe private directory")
    elif details.st_uid not in {0, os.getuid()} or mode & 0o022:
        raise ValueError("unsafe trusted directory")
    return Path(value)


def _production_directories() -> tuple[Path, Path]:
    lock_dir = _production_directory(
        "DEGEN_DOGS_LOCK_DIR",
        PRODUCTION_LOCK_DIR,
        private=True,
    )
    log_dir = _production_directory(
        "DEGEN_DOGS_LOG_DIR",
        PRODUCTION_LOG_DIR,
        private=True,
    )
    for destination in (lock_dir, log_dir):
        if destination == PRODUCTION_REPO_DIR or PRODUCTION_REPO_DIR in destination.parents:
            raise ValueError("state destination overlaps repository")
    if lock_dir == log_dir:
        raise ValueError("state destinations overlap")
    return lock_dir, log_dir


def _summary(value: RunResult) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "result": value.result,
        "error_code": value.error_code,
        "generation": value.generation,
        "commit_sha": value.commit_sha,
        "retry_count": value.retry_count,
        "raw_verified": value.raw_verified,
        "pages_verified": value.pages_verified,
    }


def _print_configuration_error(error_code: str = "configuration_invalid") -> None:
    print(json.dumps({
        "schema_version": SCHEMA_VERSION,
        "result": "configuration_error",
        "error_code": error_code,
    }, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        config = VerifierConfig(
            invocation_budget_seconds=args.budget_seconds,
            pages_poll_interval_seconds=args.poll_interval_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
        )
    except _SanitizedArgumentError:
        _print_configuration_error()
        return CONFIG_EXIT
    except SystemExit as exc:
        return int(exc.code)
    except Exception:
        _print_configuration_error()
        return CONFIG_EXIT
    try:
        if os.name != "posix":
            raise RuntimeError("production_requires_posix")
        lock_dir, log_dir = _production_directories()
    except Exception:
        _print_configuration_error(
            "production_requires_posix" if os.name != "posix" else "configuration_invalid",
        )
        return CONFIG_EXIT
    try:
        outcome = run_once(lock_dir, log_dir, config=config)
    except FetchError as exc:
        if exc.code == "configuration_invalid":
            _print_configuration_error()
            return CONFIG_EXIT
        outcome = RunResult(1, "hard_failure", exc.code, None, None, None, False, False)
    except Exception:
        outcome = RunResult(1, "hard_failure", "pending_state_conflict", None, None, None, False, False)
    print(json.dumps(_summary(outcome), sort_keys=True, separators=(",", ":")))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
