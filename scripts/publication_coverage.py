#!/usr/bin/env python3
"""Exact causal-coverage proofs for one queued auction observation.

The queue state machine owns durability.  This module owns the separate,
pure question of whether one immutable dashboard snapshot proves that it is
at least as new as the exact observation selected by the drainer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


COVERAGE_PROOF_SCHEMA_VERSION = 1
MAX_STATUS_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
MAX_GIT_PATH_LIST_BYTES = 16 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_BLOCK_HASH = re.compile(r"0x[0-9a-f]{64}\Z")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_BUNDLE_NAME = re.compile(
    r"live_snapshot_(?P<block>[1-9][0-9]*)_(?P<block_hash>[0-9a-f]{64})_"
    r"(?P<digest>[0-9a-f]{64})\.json\Z"
)
_SOURCE_KINDS = {"generated_commit", "baseline_no_diff", "peer_commit"}
_IMMUTABLE_GIT_COMMANDS = {"cat-file", "ls-files", "ls-tree", "rev-parse"}
_PROOF_KEYS = {
    "schema_version",
    "source_kind",
    "source_commit_sha",
    "status_path",
    "status_sha256",
    "bundle_path",
    "bundle_sha256",
    "bundle_bytes",
    "block_number",
    "block_hash",
    "auction",
    "canonical_reorg_from_hash",
    "quorum_attestation",
}
_AUCTION_KEYS = {
    "token_id",
    "amount_wei",
    "start_time_unix",
    "end_time_unix",
    "bidder_wallet",
    "settled",
}
_QUORUM_KEYS = {
    "onchain_chain_id",
    "onchain_verification_status",
    "onchain_verification_scope",
    "rpc_quorum_size",
    "rpc_quorum_agreement",
    "rpc_quorum_providers",
    "snapshot_confirmations",
}
_REQUIRED_SCOPE = {
    "snapshot_hash",
    "contract_code",
    "current_auction",
    "recent_event_logs",
}


class CoverageValidationError(RuntimeError):
    """A snapshot proof is malformed or does not cover its queue target."""


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise CoverageValidationError(f"{label} has an invalid JSON shape")


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise CoverageValidationError(f"{label} is not a positive JSON integer")
    return value


def _canonical_decimal(value: Any, label: str, *, positive: bool = False) -> str:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise CoverageValidationError(f"{label} is not canonical decimal")
    if positive and value == "0":
        raise CoverageValidationError(f"{label} must be positive")
    return value


def _canonical_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _BLOCK_HASH.fullmatch(value):
        raise CoverageValidationError(f"{label} is not a canonical block hash")
    return value


def _optional_canonical_hash(value: Any, label: str) -> str | None:
    if value in (None, ""):
        return None
    return _canonical_hash(value, label)


def _canonical_auction(value: Any, label: str = "coverage auction") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CoverageValidationError(f"{label} is not an object")
    _exact_keys(value, _AUCTION_KEYS, label)
    normalized = {
        "token_id": _canonical_decimal(value["token_id"], f"{label} token_id", positive=True),
        "amount_wei": _canonical_decimal(value["amount_wei"], f"{label} amount_wei"),
        "start_time_unix": _canonical_decimal(
            value["start_time_unix"], f"{label} start_time_unix", positive=True
        ),
        "end_time_unix": _canonical_decimal(
            value["end_time_unix"], f"{label} end_time_unix", positive=True
        ),
        "bidder_wallet": value["bidder_wallet"],
        "settled": value["settled"],
    }
    if not isinstance(normalized["bidder_wallet"], str) or not _ADDRESS.fullmatch(
        normalized["bidder_wallet"]
    ):
        raise CoverageValidationError(f"{label} bidder_wallet is invalid")
    if not isinstance(normalized["settled"], bool):
        raise CoverageValidationError(f"{label} settled is not boolean")
    if int(normalized["end_time_unix"]) < int(normalized["start_time_unix"]):
        raise CoverageValidationError(f"{label} end precedes start")
    return normalized


def _canonical_quorum(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CoverageValidationError("coverage quorum attestation is not an object")
    _exact_keys(value, _QUORUM_KEYS, "coverage quorum attestation")
    if value["onchain_chain_id"] != 8453:
        raise CoverageValidationError("coverage proof is not Base mainnet")
    if value["onchain_verification_status"] != "current_snapshot_cross_provider_verified":
        raise CoverageValidationError("coverage proof is not cross-provider verified")
    scope = value["onchain_verification_scope"]
    if not isinstance(scope, str):
        raise CoverageValidationError("coverage proof verification scope is invalid")
    scope_parts = {part.strip() for part in scope.split(",") if part.strip()}
    if not _REQUIRED_SCOPE.issubset(scope_parts):
        raise CoverageValidationError("coverage proof verification scope is incomplete")
    quorum_size = _positive_int(value["rpc_quorum_size"], "coverage RPC quorum size")
    if quorum_size < 2:
        raise CoverageValidationError("coverage RPC quorum is below two providers")
    agreement = value["rpc_quorum_agreement"]
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", agreement) if isinstance(agreement, str) else None
    if match is None:
        raise CoverageValidationError("coverage RPC agreement is invalid")
    agreed, responders = (int(match.group(1)), int(match.group(2)))
    if agreed < quorum_size or responders < agreed:
        raise CoverageValidationError("coverage RPC agreement is below quorum")
    providers_raw = value["rpc_quorum_providers"]
    if not isinstance(providers_raw, str):
        raise CoverageValidationError("coverage RPC provider set is invalid")
    providers = [part.strip() for part in providers_raw.split(",") if part.strip()]
    if len(providers) != len(set(providers)) or len(providers) < agreed:
        raise CoverageValidationError("coverage RPC provider set does not attest the agreement")
    _positive_int(value["snapshot_confirmations"], "coverage snapshot confirmations")
    return dict(value)


def validate_coverage_proof(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CoverageValidationError("coverage proof is not an object")
    _exact_keys(value, _PROOF_KEYS, "coverage proof")
    if value["schema_version"] != COVERAGE_PROOF_SCHEMA_VERSION:
        raise CoverageValidationError("coverage proof schema version is invalid")
    if value["source_kind"] not in _SOURCE_KINDS:
        raise CoverageValidationError("coverage proof source kind is invalid")
    if not isinstance(value["source_commit_sha"], str) or not _SHA1.fullmatch(
        value["source_commit_sha"]
    ):
        raise CoverageValidationError("coverage proof source commit is invalid")
    if value["status_path"] != "public/generated/refresh_status.json":
        raise CoverageValidationError("coverage proof status path is not fixed")
    if not isinstance(value["status_sha256"], str) or not _SHA256.fullmatch(
        value["status_sha256"]
    ):
        raise CoverageValidationError("coverage proof status digest is invalid")
    for key in ("bundle_sha256",):
        if not isinstance(value[key], str) or not _SHA256.fullmatch(value[key]):
            raise CoverageValidationError("coverage proof bundle digest is invalid")
    bundle_path = value["bundle_path"]
    prefix = "public/generated/"
    if not isinstance(bundle_path, str) or not bundle_path.startswith(prefix):
        raise CoverageValidationError("coverage proof bundle path is invalid")
    bundle_name = bundle_path[len(prefix) :]
    name_match = _BUNDLE_NAME.fullmatch(bundle_name)
    if name_match is None:
        raise CoverageValidationError("coverage proof bundle filename is unsafe")
    bundle_bytes = _positive_int(value["bundle_bytes"], "coverage proof bundle bytes")
    if bundle_bytes > MAX_BUNDLE_BYTES:
        raise CoverageValidationError("coverage proof bundle exceeds the safety limit")
    block_number = _positive_int(value["block_number"], "coverage proof block number")
    block_hash = _canonical_hash(value["block_hash"], "coverage proof block hash")
    if (
        int(name_match.group("block")) != block_number
        or f"0x{name_match.group('block_hash')}" != block_hash
        or name_match.group("digest") != value["bundle_sha256"]
    ):
        raise CoverageValidationError("coverage proof bundle filename is not bound to its checkpoint")
    auction = _canonical_auction(value["auction"])
    reorg = value["canonical_reorg_from_hash"]
    if reorg is not None:
        _canonical_hash(reorg, "coverage proof canonical reorg marker")
    quorum = _canonical_quorum(value["quorum_attestation"])
    normalized = dict(value)
    normalized["auction"] = auction
    normalized["quorum_attestation"] = quorum
    return normalized


def observation_auction_tuple(observation: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _canonical_auction(
            {key: observation[key] for key in _AUCTION_KEYS},
            "publication target auction",
        )
    except KeyError as exc:
        raise CoverageValidationError("publication target lacks its exact auction tuple") from exc


def coverage_proof_covers_observation(
    proof: Mapping[str, Any],
    observation: Mapping[str, Any],
) -> bool:
    """Return the exact monotonic/reorg coverage invariant without side effects."""
    validated = validate_coverage_proof(dict(proof))
    try:
        target_block = _positive_int(
            observation["confirmed_block_number"], "publication target block number"
        )
        target_hash = _canonical_hash(
            observation["confirmed_block_hash"], "publication target block hash"
        )
    except KeyError as exc:
        raise CoverageValidationError("publication target lacks its confirmed checkpoint") from exc
    proof_block = validated["block_number"]
    if proof_block > target_block:
        return True
    if proof_block < target_block:
        return False
    if validated["block_hash"] == target_hash:
        return validated["auction"] == observation_auction_tuple(observation)
    return validated["canonical_reorg_from_hash"] == target_hash


def _attest_posix_git_executable(candidate: Path) -> str:
    descriptor: int | None = None
    try:
        path_stat = candidate.lstat()
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(candidate, flags)
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise CoverageValidationError("trusted Git executable is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    same_file = (
        path_stat.st_dev == descriptor_stat.st_dev
        and path_stat.st_ino == descriptor_stat.st_ino
    )
    if (
        not same_file
        or not stat.S_ISREG(path_stat.st_mode)
        or not stat.S_ISREG(descriptor_stat.st_mode)
        or path_stat.st_uid != 0
        or path_stat.st_nlink != 1
        or path_stat.st_mode & 0o022
        or not path_stat.st_mode & 0o111
    ):
        raise CoverageValidationError("trusted Git executable is unavailable")
    for parent in (candidate.parent, candidate.parent.parent):
        try:
            parent_stat = parent.lstat()
        except OSError as exc:
            raise CoverageValidationError("trusted Git executable is unavailable") from exc
        if (
            not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid != 0
            or parent_stat.st_mode & 0o022
        ):
            raise CoverageValidationError("trusted Git executable is unavailable")
    return str(candidate)


def _trusted_git_executable() -> str:
    """Resolve Git without consulting PATH on POSIX runner hosts."""
    if os.name == "posix":
        return _attest_posix_git_executable(Path("/usr/bin/git"))
    discovered = shutil.which("git.exe") or shutil.which("git")
    if not discovered:
        raise CoverageValidationError("trusted Git executable is unavailable")
    try:
        resolved = Path(discovered).resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise CoverageValidationError("trusted Git executable is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise CoverageValidationError("trusted Git executable is unavailable")
    return str(resolved)


def _hardened_git_environment() -> dict[str, str]:
    # Git's documented environment variables can redirect the repository,
    # object database, index, config, replacement namespace, and executable
    # lookup.  None of those are caller-controlled inputs to an attestation.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
        and not key.upper().startswith("LD_")
        and not key.upper().startswith("DYLD_")
    }
    environment.update(
        {
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    if os.name == "posix":
        environment["PATH"] = "/usr/bin:/bin"
    return environment


def _git_command(repo: Path, arguments: tuple[str, ...]) -> list[str]:
    if not arguments or arguments[0] not in _IMMUTABLE_GIT_COMMANDS:
        raise CoverageValidationError("immutable Git command is not allowlisted")
    command = arguments[0]
    return [
        _trusted_git_executable(),
        "--no-pager",
        "--no-replace-objects",
        "--no-optional-locks",
        "--literal-pathspecs",
        "-c",
        "core.useReplaceRefs=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "diff.external=",
        "-c",
        f"alias.{command}=",
        "-C",
        str(repo),
        f"--work-tree={repo}",
        *arguments,
    ]


def _git_output(repo: Path, *arguments: str, max_bytes: int | None = None) -> bytes:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            _git_command(repo, arguments),
            cwd=repo,
            env=_hardened_git_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        if max_bytes is None:
            payload, _ = process.communicate()
        else:
            payload_buffer = bytearray()
            while True:
                remaining = max_bytes + 1 - len(payload_buffer)
                chunk = process.stdout.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                payload_buffer.extend(chunk)
                if len(payload_buffer) > max_bytes:
                    process.kill()
                    process.wait()
                    raise CoverageValidationError(
                        "immutable publication source has an unsafe size"
                    )
            process.wait()
            payload = bytes(payload_buffer)
        if process.returncode != 0:
            raise CoverageValidationError(
                "cannot read immutable publication source from Git"
            )
    except OSError as exc:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        raise CoverageValidationError("cannot read immutable publication source from Git") from exc
    if max_bytes is not None and (not payload or len(payload) > max_bytes):
        raise CoverageValidationError("immutable publication source has an unsafe size")
    return payload


def _git_object_bytes(repo: Path, commit: str, relative: str, *, max_bytes: int) -> bytes:
    object_name = f"{commit}:{relative}"
    raw_size = _git_output(repo, "cat-file", "-s", object_name, max_bytes=64).decode("ascii", errors="strict").strip()
    if not raw_size.isdigit() or int(raw_size) < 1 or int(raw_size) > max_bytes:
        raise CoverageValidationError(f"immutable {relative} has an unsafe size")
    payload = _git_output(repo, "cat-file", "blob", object_name, max_bytes=max_bytes)
    if len(payload) != int(raw_size):
        raise CoverageValidationError(f"immutable {relative} changed during extraction")
    return payload


def _read_git_control_path(path: Path, *, prefix: bytes | None = None) -> Path:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1 or metadata.st_size > 4096:
            raise CoverageValidationError("publication Git control path is invalid")
        payload = path.read_bytes()
    except OSError as exc:
        raise CoverageValidationError("publication Git control path is invalid") from exc
    if len(payload) != metadata.st_size or b"\0" in payload:
        raise CoverageValidationError("publication Git control path is invalid")
    payload = payload.strip()
    if prefix is not None:
        if not payload.startswith(prefix):
            raise CoverageValidationError("publication Git control path is invalid")
        payload = payload[len(prefix) :].strip()
    try:
        decoded = os.fsdecode(payload)
    except (TypeError, UnicodeDecodeError) as exc:
        raise CoverageValidationError("publication Git control path is invalid") from exc
    if not decoded:
        raise CoverageValidationError("publication Git control path is invalid")
    target = Path(decoded)
    if not target.is_absolute():
        target = path.parent / target
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise CoverageValidationError("publication Git control path is invalid") from exc
    if not resolved.is_dir():
        raise CoverageValidationError("publication Git control path is invalid")
    return resolved


def _repository_git_directories(repo: Path) -> tuple[Path, Path]:
    dot_git = repo / ".git"
    try:
        metadata = dot_git.lstat()
    except OSError as exc:
        raise CoverageValidationError("publication coverage source is not a Git worktree") from exc
    if stat.S_ISDIR(metadata.st_mode):
        git_dir = dot_git.resolve(strict=True)
    elif stat.S_ISREG(metadata.st_mode):
        git_dir = _read_git_control_path(dot_git, prefix=b"gitdir:")
    else:
        raise CoverageValidationError("publication coverage source is not a Git worktree")
    common_pointer = git_dir / "commondir"
    try:
        common_pointer.lstat()
    except FileNotFoundError:
        common_dir = git_dir
    except OSError as exc:
        raise CoverageValidationError("publication Git common directory is invalid") from exc
    else:
        common_dir = _read_git_control_path(common_pointer)
    return git_dir, common_dir


def _reject_unsafe_object_indirection(repo: Path) -> None:
    git_dir, common_dir = _repository_git_directories(repo)
    objects = common_dir / "objects"
    try:
        object_metadata = objects.lstat()
    except OSError as exc:
        raise CoverageValidationError("publication Git object database is invalid") from exc
    if not stat.S_ISDIR(object_metadata.st_mode):
        raise CoverageValidationError("publication Git object database is invalid")
    unsafe_paths = {
        objects / "info" / "alternates",
        common_dir / "info" / "grafts",
        git_dir / "info" / "grafts",
    }
    for unsafe in unsafe_paths:
        try:
            unsafe.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CoverageValidationError(
                "publication Git alternate object database is invalid"
            ) from exc
        raise CoverageValidationError(
            "publication Git alternate object database is not permitted"
        )


def _validated_source_root(repo: str | Path, source_commit_sha: str) -> Path:
    try:
        root = Path(repo).resolve(strict=True)
    except OSError as exc:
        raise CoverageValidationError("publication coverage source is invalid") from exc
    if not root.is_dir():
        raise CoverageValidationError("publication coverage source is invalid")
    _reject_unsafe_object_indirection(root)
    if not isinstance(source_commit_sha, str) or not _SHA1.fullmatch(source_commit_sha):
        raise CoverageValidationError("publication coverage source commit is invalid")
    try:
        resolved = _git_output(
            root,
            "rev-parse",
            "--verify",
            f"{source_commit_sha}^{{commit}}",
            max_bytes=64,
        ).decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CoverageValidationError("publication coverage source commit is invalid") from exc
    if resolved != source_commit_sha:
        raise CoverageValidationError("publication coverage source is not the exact commit")
    return root


def _generated_relative_path(raw: bytes) -> str:
    try:
        relative = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CoverageValidationError("baseline generated path is not UTF-8") from exc
    posix = PurePosixPath(relative)
    if (
        posix.is_absolute()
        or not posix.parts
        or any(part in {"", ".", ".."} for part in posix.parts)
        or not (
            relative.startswith("generated/")
            or relative.startswith("public/generated/")
        )
    ):
        raise CoverageValidationError("baseline generated path is unsafe")
    return relative


def _tree_entries(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    payload = _git_output(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        "--",
        "generated",
        "public/generated",
        max_bytes=MAX_GIT_PATH_LIST_BYTES,
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_raw, type_raw, oid_raw = header.split(b" ", 2)
            mode = mode_raw.decode("ascii", errors="strict")
            object_type = type_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise CoverageValidationError("baseline generated tree is malformed") from exc
        relative = _generated_relative_path(raw_path)
        if (
            relative in entries
            or object_type != "blob"
            or mode not in {"100644", "100755"}
            or not _SHA1.fullmatch(oid)
        ):
            raise CoverageValidationError("baseline generated tree is unsupported")
        entries[relative] = (mode, oid)
    if not entries:
        raise CoverageValidationError("baseline generated tree is empty")
    return entries


def _index_entries(repo: Path) -> dict[str, tuple[str, str]]:
    payload = _git_output(
        repo,
        "ls-files",
        "-z",
        "--stage",
        "--",
        "generated",
        "public/generated",
        max_bytes=MAX_GIT_PATH_LIST_BYTES,
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_raw, oid_raw, stage_raw = header.split(b" ", 2)
            mode = mode_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict")
            stage = stage_raw.decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as exc:
            raise CoverageValidationError("baseline generated index is malformed") from exc
        relative = _generated_relative_path(raw_path)
        if (
            relative in entries
            or stage != "0"
            or mode not in {"100644", "100755"}
            or not _SHA1.fullmatch(oid)
        ):
            raise CoverageValidationError("baseline generated index is unsupported")
        entries[relative] = (mode, oid)
    return entries


def _worktree_blob_oid(repo: Path, relative: str, expected_mode: str) -> str:
    candidate = repo.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except OSError as exc:
        raise CoverageValidationError("baseline generated worktree entry is missing") from exc
    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise CoverageValidationError("baseline generated worktree entry escapes the repository") from exc
    if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise CoverageValidationError("baseline generated worktree entry is not a regular file")
    if os.name == "posix":
        executable = bool(metadata.st_mode & 0o111)
        if executable != (expected_mode == "100755"):
            raise CoverageValidationError("baseline generated worktree mode changed")
    digest = hashlib.sha1()
    digest.update(f"blob {metadata.st_size}\0".encode("ascii"))
    with candidate.open("rb") as handle:
        before = os.fstat(handle.fileno())
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if (
        before.st_size != metadata.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or after.st_ino != before.st_ino
    ):
        raise CoverageValidationError("baseline generated worktree entry changed during validation")
    return digest.hexdigest()


def _baseline_matches_exact_commit(repo: Path, commit: str) -> bool:
    tree = _tree_entries(repo, commit)
    index = _index_entries(repo)
    if index != tree:
        return False
    return all(
        _worktree_blob_oid(repo, relative, mode) == oid
        for relative, (mode, oid) in tree.items()
    )


def read_proven_publication_artifacts(
    repo: str | Path,
    proof: Mapping[str, Any],
) -> tuple[bytes, bytes]:
    """Read exact status and bundle bytes bound by a validated coverage proof."""
    validated = validate_coverage_proof(dict(proof))
    root = _validated_source_root(repo, validated["source_commit_sha"])
    status = _git_object_bytes(
        root,
        validated["source_commit_sha"],
        validated["status_path"],
        max_bytes=MAX_STATUS_BYTES,
    )
    bundle = _git_object_bytes(
        root,
        validated["source_commit_sha"],
        validated["bundle_path"],
        max_bytes=MAX_BUNDLE_BYTES,
    )
    if hashlib.sha256(status).hexdigest() != validated["status_sha256"]:
        raise CoverageValidationError("immutable refresh status digest differs from its proof")
    if (
        len(bundle) != validated["bundle_bytes"]
        or hashlib.sha256(bundle).hexdigest() != validated["bundle_sha256"]
    ):
        raise CoverageValidationError("immutable live bundle differs from its proof")
    return status, bundle


def _decode_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CoverageValidationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise CoverageValidationError(f"{label} is not an object")
    return value


def _metrics_map(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list) or not rows:
        raise CoverageValidationError("coverage bundle mission metrics are invalid")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"metric", "value"}:
            raise CoverageValidationError("coverage bundle mission metric row is invalid")
        metric = row["metric"]
        if not isinstance(metric, str) or not metric or metric in result:
            raise CoverageValidationError("coverage bundle mission metrics are ambiguous")
        result[metric] = "" if row["value"] is None else str(row["value"])
    return result


def _auction_from_current_row(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise CoverageValidationError("coverage bundle current auction row is invalid")
    token_id = row.get("token_id")
    amount_wei = row.get("amount_wei")
    start_unix = row.get("start_time_unix")
    end_unix = row.get("end_time_unix")
    wallet = row.get("bidder_wallet")
    settled = row.get("settled")
    if type(token_id) is not int or token_id < 1:
        raise CoverageValidationError("coverage bundle current token ID is invalid")
    _canonical_decimal(amount_wei, "coverage bundle amount_wei")
    if type(start_unix) is not int or start_unix < 1 or type(end_unix) is not int or end_unix < 1:
        raise CoverageValidationError("coverage bundle auction unix times are invalid")
    if not isinstance(wallet, str) or not _ADDRESS.fullmatch(wallet):
        raise CoverageValidationError("coverage bundle bidder wallet is invalid")
    if type(settled) is not int or settled not in {0, 1}:
        raise CoverageValidationError("coverage bundle settled flag is invalid")
    return _canonical_auction(
        {
            "token_id": str(token_id),
            "amount_wei": amount_wei,
            "start_time_unix": str(start_unix),
            "end_time_unix": str(end_unix),
            "bidder_wallet": wallet,
            "settled": bool(settled),
        }
    )


def extract_coverage_proof(
    repo: str | Path,
    *,
    source_kind: str,
    source_commit_sha: str,
    publication_target: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract and bind one proof from an exact Git commit.

    ``baseline_no_diff`` additionally proves that the checkout still names that commit
    and that its generated mirrors have no tracked worktree delta.
    """
    if source_kind not in _SOURCE_KINDS:
        raise CoverageValidationError("publication coverage source is invalid")
    root = _validated_source_root(repo, source_commit_sha)
    if source_kind == "baseline_no_diff":
        head = _git_output(root, "rev-parse", "HEAD", max_bytes=64).decode("ascii").strip()
        if head != source_commit_sha:
            raise CoverageValidationError("baseline coverage source is not the current HEAD")
        if not _baseline_matches_exact_commit(root, source_commit_sha):
            raise CoverageValidationError("baseline coverage source has generated worktree changes")

    generated_status_bytes = _git_object_bytes(
        root, source_commit_sha, "generated/refresh_status.json", max_bytes=MAX_STATUS_BYTES
    )
    public_status_bytes = _git_object_bytes(
        root, source_commit_sha, "public/generated/refresh_status.json", max_bytes=MAX_STATUS_BYTES
    )
    if generated_status_bytes != public_status_bytes:
        raise CoverageValidationError("immutable refresh status mirrors differ")
    status = _decode_object(public_status_bytes, "immutable refresh status")
    bundle_name = status.get("live_snapshot_bundle")
    name_match = _BUNDLE_NAME.fullmatch(bundle_name) if isinstance(bundle_name, str) else None
    if name_match is None:
        raise CoverageValidationError("immutable refresh status has an unsafe live bundle")
    generated_bundle_bytes = _git_object_bytes(
        root, source_commit_sha, f"generated/{bundle_name}", max_bytes=MAX_BUNDLE_BYTES
    )
    public_bundle_bytes = _git_object_bytes(
        root, source_commit_sha, f"public/generated/{bundle_name}", max_bytes=MAX_BUNDLE_BYTES
    )
    if generated_bundle_bytes != public_bundle_bytes:
        raise CoverageValidationError("immutable live bundle mirrors differ")
    bundle_digest = hashlib.sha256(public_bundle_bytes).hexdigest()
    if (
        status.get("live_snapshot_bundle_sha256") != bundle_digest
        or status.get("live_snapshot_bundle_bytes") != len(public_bundle_bytes)
        or status.get("live_snapshot_bundle_schema_version") != 1
        or name_match.group("digest") != bundle_digest
    ):
        raise CoverageValidationError("immutable live bundle differs from its status pointer")
    bundle = _decode_object(public_bundle_bytes, "immutable live bundle")
    expected_bundle_fields = {
        "schema_version",
        "kind",
        "latest_generated_block",
        "snapshot_block_hash",
        "current_auction",
        "auction_feed",
        "current_auction_bid_history",
        "mission3_metrics",
    }
    _exact_keys(bundle, expected_bundle_fields, "immutable live bundle")
    if bundle.get("schema_version") != 1 or bundle.get("kind") != "degen_dogs_live_snapshot":
        raise CoverageValidationError("immutable live bundle identity is invalid")
    block_number = _positive_int(status.get("latest_generated_block"), "immutable status block")
    block_hash = _canonical_hash(status.get("snapshot_block_hash"), "immutable status block hash")
    if (
        bundle.get("latest_generated_block") != block_number
        or bundle.get("snapshot_block_hash") != block_hash
        or int(name_match.group("block")) != block_number
        or f"0x{name_match.group('block_hash')}" != block_hash
    ):
        raise CoverageValidationError("immutable live bundle checkpoint disagrees with status")
    current_rows = bundle.get("current_auction")
    if not isinstance(current_rows, list) or len(current_rows) != 1:
        raise CoverageValidationError("immutable live bundle lacks exactly one current auction")
    current = current_rows[0]
    if not isinstance(current, dict) or current.get("latest_block") != block_number:
        raise CoverageValidationError("immutable current auction checkpoint disagrees")
    auction = _auction_from_current_row(current)
    metrics = _metrics_map(bundle.get("mission3_metrics"))
    quorum = {
        "onchain_chain_id": status.get("onchain_chain_id"),
        "onchain_verification_status": status.get("onchain_verification_status"),
        "onchain_verification_scope": status.get("onchain_verification_scope"),
        "rpc_quorum_size": status.get("rpc_quorum_size"),
        "rpc_quorum_agreement": status.get("rpc_quorum_agreement"),
        "rpc_quorum_providers": status.get("rpc_quorum_providers"),
        "snapshot_confirmations": status.get("snapshot_confirmations"),
    }
    quorum = _canonical_quorum(quorum)
    metric_expectations = {
        "latest_block": str(block_number),
        "snapshot_block_hash": block_hash,
        "onchain_chain_id": str(quorum["onchain_chain_id"]),
        "onchain_verification_status": quorum["onchain_verification_status"],
        "onchain_verification_scope": quorum["onchain_verification_scope"],
        "rpc_quorum_size": str(quorum["rpc_quorum_size"]),
        "rpc_quorum_agreement": quorum["rpc_quorum_agreement"],
        "rpc_quorum_providers": quorum["rpc_quorum_providers"],
        "snapshot_confirmations": str(quorum["snapshot_confirmations"]),
    }
    if any(metrics.get(key) != value for key, value in metric_expectations.items()):
        raise CoverageValidationError("immutable live bundle quorum metrics disagree with status")
    try:
        target_observation = publication_target["observation"]
    except (KeyError, TypeError) as exc:
        raise CoverageValidationError("publication target lacks its observation") from exc
    if not isinstance(target_observation, Mapping):
        raise CoverageValidationError("publication target observation is invalid")
    reorg_marker = _optional_canonical_hash(
        status.get("canonical_reorg_from_hash"),
        "immutable status canonical reorg marker",
    )
    bundle_reorg_marker = _optional_canonical_hash(
        metrics.get("canonical_reorg_from_hash"),
        "immutable bundle canonical reorg marker",
    )
    if reorg_marker != bundle_reorg_marker:
        raise CoverageValidationError(
            "immutable canonical reorg marker differs between status and live bundle"
        )
    proof = validate_coverage_proof(
        {
            "schema_version": COVERAGE_PROOF_SCHEMA_VERSION,
            "source_kind": source_kind,
            "source_commit_sha": source_commit_sha,
            "status_path": "public/generated/refresh_status.json",
            "status_sha256": hashlib.sha256(public_status_bytes).hexdigest(),
            "bundle_path": f"public/generated/{bundle_name}",
            "bundle_sha256": bundle_digest,
            "bundle_bytes": len(public_bundle_bytes),
            "block_number": block_number,
            "block_hash": block_hash,
            "auction": auction,
            "canonical_reorg_from_hash": reorg_marker,
            "quorum_attestation": quorum,
        }
    )
    if not coverage_proof_covers_observation(proof, target_observation):
        raise CoverageValidationError("immutable publication snapshot does not cover its queue target")
    return proof
