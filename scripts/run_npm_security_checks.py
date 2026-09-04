#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BufferedReader
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit, urlunsplit


NPM_NETWORK_FLAGS = (
    "--fetch-retries=0",
    "--fetch-timeout=15000",
    "--fetch-retry-mintimeout=1000",
    "--fetch-retry-maxtimeout=1000",
    "--json",
)

NPM_POLICY_FLAGS = (
    "--global=false",
    "--package-lock=true",
    "--offline=false",
    "--prefer-offline=false",
    "--prefer-online=true",
    "--strict-ssl=true",
    "--registry=https://registry.npmjs.org/",
    "--include=prod",
    "--include=dev",
    "--include=optional",
    "--include=peer",
)

CHECKS = (
    ("vulnerabilities", ("audit", "--audit-level=high")),
    ("signatures", ("audit", "signatures")),
)

MAX_CAPTURE_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
READER_JOIN_GRACE_SECONDS = 0.5
READER_JOIN_AFTER_KILL_SECONDS = 2.0
PROCESS_EXIT_AFTER_KILL_SECONDS = 2.0
PROJECT_ROOT = Path(__file__).resolve().parent.parent

CHILD_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

TRANSIENT_REGISTRY_PATTERNS = (
    re.compile(
        r"\b(?:EAI_AGAIN|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENETUNREACH|"
        r"EHOSTUNREACH|ERR_SOCKET_TIMEOUT)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:network|socket) timeout\b|\bsocket hang up\b", re.IGNORECASE),
    re.compile(
        r"\b(?:408\s+Request Timeout|429\s+Too Many Requests|500\s+Internal Server Error|"
        r"502\s+Bad Gateway|503\s+Service Unavailable|504\s+Gateway Timeout)\b",
        re.IGNORECASE,
    ),
)


def is_transient_registry_failure(message: str) -> bool:
    return any(pattern.search(message) for pattern in TRANSIENT_REGISTRY_PATTERNS)


def parse_json_object(raw_json: str) -> dict | None:
    try:
        report = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(report, dict):
        return None
    return report


def is_valid_vulnerability_report(audit_json: str) -> bool:
    report = parse_json_object(audit_json)
    if report is None or not isinstance(report.get("vulnerabilities"), dict):
        return False
    metadata = report.get("metadata")
    counts = metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    if not isinstance(counts, dict):
        return False
    return all(type(counts.get(severity)) is int and counts[severity] >= 0 for severity in ("high", "critical"))


def has_blocking_vulnerability(audit_json: str) -> bool:
    report = parse_json_object(audit_json)
    if report is None:
        return False
    metadata = report.get("metadata")
    counts = metadata.get("vulnerabilities") if isinstance(metadata, dict) else None
    if isinstance(counts, dict):
        for severity in ("high", "critical"):
            count = counts.get(severity)
            if isinstance(count, int) and count > 0:
                return True
    vulnerabilities = report.get("vulnerabilities")
    if isinstance(vulnerabilities, dict):
        return any(
            isinstance(details, dict) and details.get("severity") in {"high", "critical"}
            for details in vulnerabilities.values()
        )
    return False


def has_invalid_or_missing_signature(signature_json: str) -> bool:
    report = parse_json_object(signature_json)
    if report is None:
        return False
    return any(isinstance(report.get(field), list) and report[field] for field in ("invalid", "missing"))


def is_valid_signature_report(signature_json: str) -> bool:
    report = parse_json_object(signature_json)
    return report is not None and all(isinstance(report.get(field), list) for field in ("invalid", "missing"))


SIGNATURE_INTEGRITY_PATTERN = re.compile(
    r"\bEINTEGRITY\b|\binvalid(?:\s+registry)?\s+signature\b|\bsignature\s+(?:is\s+)?invalid\b|"
    r"\bintegrity\s+(?:check\s+)?(?:failed|failure|mismatch)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    output_incomplete: bool


def _drain_bounded(stream: BufferedReader, capture: dict[str, object]) -> None:
    data = bytearray()
    truncated = False
    try:
        while chunk := stream.read(READ_CHUNK_BYTES):
            remaining = MAX_CAPTURE_BYTES - len(data)
            if remaining > 0:
                data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    except (OSError, ValueError):
        truncated = True
    finally:
        capture["data"] = bytes(data)
        capture["truncated"] = truncated


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _join_readers(readers: Sequence[threading.Thread], timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(reader.is_alive() for reader in readers)


def _clean_child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in environ.items():
        normalized = key.upper()
        if normalized.startswith("NPM_CONFIG_"):
            continue
        if normalized in {"NODE_ENV", "NODE_OPTIONS", "NODE_PATH"}:
            continue
        clean[key] = value
    return clean


def _sanitize_child_url(match: re.Match[str]) -> str:
    raw_url = match.group(0)
    trailing = ""
    while raw_url and raw_url[-1] in ".,);]}>":
        trailing = raw_url[-1] + trailing
        raw_url = raw_url[:-1]
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return "[redacted malformed URL]" + trailing
    if not parsed.scheme or not parsed.netloc:
        return "[redacted malformed URL]" + trailing
    safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, safe_netloc, parsed.path, "", "")) + trailing


def sanitize_child_output(content: str) -> str:
    sanitized = CHILD_URL_PATTERN.sub(_sanitize_child_url, content)
    return sanitized.replace("##[", "# #[")


def run_command(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    environ: Mapping[str, str],
    cwd: Path,
) -> CommandResult:
    popen_options = {"start_new_session": True} if os.name == "posix" else {}
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environ),
        cwd=cwd,
        **popen_options,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_capture: dict[str, object] = {}
    stderr_capture: dict[str, object] = {}
    readers = (
        threading.Thread(target=_drain_bounded, args=(process.stdout, stdout_capture), daemon=True),
        threading.Thread(target=_drain_bounded, args=(process.stderr, stderr_capture), daemon=True),
    )
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process(process)
        try:
            returncode = process.wait(timeout=PROCESS_EXIT_AFTER_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_process(process)
            returncode = process.poll()
            if returncode is None:
                returncode = -signal.SIGKILL
    readers_finished = _join_readers(readers, READER_JOIN_GRACE_SECONDS)
    if not readers_finished:
        _kill_process(process)
        readers_finished = _join_readers(readers, READER_JOIN_AFTER_KILL_SECONDS)
    return CommandResult(
        returncode=returncode,
        stdout=bytes(stdout_capture.get("data", b"")).decode("utf-8", errors="replace"),
        stderr=bytes(stderr_capture.get("data", b"")).decode("utf-8", errors="replace"),
        timed_out=timed_out,
        stdout_truncated=bool(stdout_capture.get("truncated", False)),
        stderr_truncated=bool(stderr_capture.get("truncated", False)),
        output_incomplete=not readers_finished,
    )


def print_captured_stream(
    content: str,
    *,
    stream_name: str,
    truncated: bool,
    output: TextIO,
) -> None:
    remaining_bytes = MAX_CAPTURE_BYTES
    display_truncated = truncated
    for raw_line in content.splitlines():
        rendered = f"[npm {stream_name}] {sanitize_child_output(raw_line)}"
        encoded = rendered.encode("utf-8", errors="replace")
        if len(encoded) + 1 > remaining_bytes:
            visible = encoded[: max(0, remaining_bytes - 1)].decode("utf-8", errors="ignore")
            if visible:
                print(visible, file=output)
            display_truncated = True
            remaining_bytes = 0
            break
        print(rendered, file=output)
        remaining_bytes -= len(encoded) + 1
    if display_truncated:
        print(f"[npm {stream_name}] [truncated at {MAX_CAPTURE_BYTES} bytes]", file=output)


def _resolve_checkout_root(checkout_root: str | os.PathLike[str] | None) -> Path:
    root = PROJECT_ROOT if checkout_root is None else Path(checkout_root)
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(resolved)
    return resolved


def _project_npmrc_exists(checkout_root: Path) -> bool:
    try:
        (checkout_root / ".npmrc").lstat()
    except FileNotFoundError:
        return False
    return True


def _write_empty_config(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)


def run_gate(
    *,
    command_prefix: Sequence[str] = ("npm",),
    attempts: int = 2,
    timeout_seconds: float = 30,
    retry_delay_seconds: float = 3,
    environ: Mapping[str, str] | None = None,
    output: TextIO = sys.stdout,
    checkout_root: str | os.PathLike[str] | None = None,
) -> int:
    if not 1 <= attempts <= 3:
        raise ValueError("attempts must be between 1 and 3")
    if not 1 <= timeout_seconds <= 60:
        raise ValueError("timeout_seconds must be between 1 and 60")
    if not 0 <= retry_delay_seconds <= 10:
        raise ValueError("retry_delay_seconds must be between 0 and 10")
    try:
        resolved_checkout = _resolve_checkout_root(checkout_root)
    except OSError as exc:
        print(
            "::error title=npm security gate failed::"
            f"unable to resolve checkout root ({exc.__class__.__name__})",
            file=output,
        )
        return 1
    if _project_npmrc_exists(resolved_checkout):
        print(
            "::error title=npm security gate failed::project .npmrc is forbidden for the security gate",
            file=output,
        )
        return 1
    result = 0
    child_env = _clean_child_environment(os.environ if environ is None else environ)
    with tempfile.TemporaryDirectory(prefix="degen-dogs-npm-security-") as config_tmp:
        config_root = Path(config_tmp)
        user_config = config_root / "user.npmrc"
        global_config = config_root / "global.npmrc"
        _write_empty_config(user_config)
        _write_empty_config(global_config)
        bound_flags = (
            *NPM_POLICY_FLAGS,
            f"--prefix={resolved_checkout}",
            f"--userconfig={user_config}",
            f"--globalconfig={global_config}",
        )
        for label, npm_args in CHECKS:
            command = [*command_prefix, *npm_args, *NPM_NETWORK_FLAGS, *bound_flags]
            for attempt in range(1, attempts + 1):
                if _project_npmrc_exists(resolved_checkout):
                    print(
                        "::error title=npm security gate failed::"
                        "project .npmrc appeared while the security gate was running",
                        file=output,
                    )
                    result = 1
                    break
                try:
                    completed = run_command(
                        command,
                        timeout_seconds=timeout_seconds,
                        environ=child_env,
                        cwd=resolved_checkout,
                    )
                except OSError as exc:
                    print(
                        "::error title=npm security gate failed::"
                        f"{label}: unable to start npm ({exc.__class__.__name__})",
                        file=output,
                    )
                    result = 1
                    break
                stdout = completed.stdout
                combined = f"{completed.stdout}\n{completed.stderr}"
                print_captured_stream(
                    completed.stdout,
                    stream_name="stdout",
                    truncated=completed.stdout_truncated,
                    output=output,
                )
                print_captured_stream(
                    completed.stderr,
                    stream_name="stderr",
                    truncated=completed.stderr_truncated,
                    output=output,
                )
                if completed.timed_out:
                    print(f"{label}: npm command timed out after {timeout_seconds} seconds", file=output)
                if completed.output_incomplete:
                    print(f"{label}: npm output pipes did not close within the bounded drain window", file=output)
                if completed.stdout_truncated or completed.stderr_truncated:
                    print(
                        f"::error title=npm security gate failed::{label}: "
                        "output exceeded the bounded capture limit",
                        file=output,
                    )
                    result = 1
                    break
                if label == "vulnerabilities" and has_blocking_vulnerability(stdout):
                    print(
                        "::error title=npm security gate failed::"
                        f"{label}: high-severity vulnerability policy violation",
                        file=output,
                    )
                    result = 1
                    break
                if label == "signatures" and (
                    has_invalid_or_missing_signature(stdout) or SIGNATURE_INTEGRITY_PATTERN.search(combined)
                ):
                    print(
                        "::error title=npm security gate failed::"
                        f"{label}: signature integrity policy violation",
                        file=output,
                    )
                    result = 1
                    break
                if completed.returncode == 0 and not completed.output_incomplete:
                    report_is_valid = (
                        is_valid_vulnerability_report(stdout)
                        if label == "vulnerabilities"
                        else is_valid_signature_report(stdout)
                    )
                    if not report_is_valid:
                        print(
                            f"::error title=npm security gate failed::{label}: invalid {label} report from npm",
                            file=output,
                        )
                        result = 1
                        break
                    break
                if completed.timed_out or completed.output_incomplete or is_transient_registry_failure(combined):
                    if attempt < attempts:
                        print(
                            f"{label}: transient registry failure (attempt {attempt}/{attempts}); retrying",
                            file=output,
                        )
                        time.sleep(retry_delay_seconds)
                        continue
                    print(
                        f"::error title=npm registry unavailable::{label} check could not reach npm "
                        f"after {attempts} bounded attempts; CI is failing closed",
                        file=output,
                    )
                    if result == 0:
                        result = 75
                    break
                print(
                    f"::error title=npm security gate failed::{label}: non-transient npm security failure",
                    file=output,
                )
                result = 1
                break
    if result == 0:
        print("npm security checks passed", file=output)
    return result


def bounded_number(name: str, minimum: float, maximum: float, *, integer: bool = False):
    converter = int if integer else float

    def parse(raw: str):
        try:
            value = converter(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be a number") from exc
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"{name} must be between {minimum:g} and {maximum:g}")
        return value

    return parse


def main(
    argv: Sequence[str] | None = None,
    *,
    command_prefix: Sequence[str] = ("npm",),
    environ: Mapping[str, str] | None = None,
    output: TextIO = sys.stdout,
    checkout_root: str | os.PathLike[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run bounded, fail-closed npm security checks")
    parser.add_argument("--attempts", type=bounded_number("attempts", 1, 3, integer=True), default=2)
    parser.add_argument("--timeout-seconds", type=bounded_number("timeout-seconds", 1, 60), default=30)
    parser.add_argument(
        "--retry-delay-seconds",
        type=bounded_number("retry-delay-seconds", 0, 10),
        default=3,
    )
    options = parser.parse_args(argv)
    return run_gate(
        command_prefix=command_prefix,
        attempts=options.attempts,
        timeout_seconds=options.timeout_seconds,
        retry_delay_seconds=options.retry_delay_seconds,
        environ=environ,
        output=output,
        checkout_root=checkout_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
