#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import contextlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "publication_coverage.py"
STATE_MODULE_PATH = ROOT / "scripts" / "runner_publication_state.py"
REAL_GIT = Path("/usr/bin/git") if os.name == "posix" else Path(shutil.which("git") or "")


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("publication_coverage", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_state_module() -> Any:
    spec = importlib.util.spec_from_file_location("runner_publication_state_coverage_test", STATE_MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_git(repo: Path, *arguments: str) -> bytes:
    assert REAL_GIT.is_absolute() and REAL_GIT.is_file(), "test requires a real Git executable"
    return subprocess.check_output(
        [str(REAL_GIT), *arguments],
        cwd=repo,
        stderr=subprocess.STDOUT,
    )


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def snapshot_hash(block: int) -> str:
    return "0x" + f"{block:064x}"


def write_snapshot(
    repo: Path,
    *,
    amount_wei: str,
    block_hash_override: str | None = None,
    canonical_reorg_from_hash: str | None = None,
) -> tuple[bytes, bytes]:
    block = 123
    block_hash = block_hash_override or snapshot_hash(block)
    scope = "snapshot_hash,contract_code,current_auction,recent_event_logs"
    providers = "rpc-a.example,rpc-b.example"
    auction = {
        "token_id": 7,
        "amount_wei": amount_wei,
        "start_time_unix": 1780000000,
        "end_time_unix": 1780003600,
        "bidder_wallet": "0x" + "1" * 40,
        "settled": 0,
        "latest_block": block,
    }
    metrics = [
        {"metric": "latest_block", "value": str(block)},
        {"metric": "snapshot_block_hash", "value": block_hash},
        {"metric": "onchain_chain_id", "value": "8453"},
        {
            "metric": "onchain_verification_status",
            "value": "current_snapshot_cross_provider_verified",
        },
        {"metric": "onchain_verification_scope", "value": scope},
        {"metric": "rpc_quorum_size", "value": "2"},
        {"metric": "rpc_quorum_agreement", "value": "2/2"},
        {"metric": "rpc_quorum_providers", "value": providers},
        {"metric": "snapshot_confirmations", "value": "1"},
        {
            "metric": "canonical_reorg_from_hash",
            "value": canonical_reorg_from_hash or "",
        },
    ]
    bundle = canonical_json(
        {
            "schema_version": 1,
            "kind": "degen_dogs_live_snapshot",
            "latest_generated_block": block,
            "snapshot_block_hash": block_hash,
            "current_auction": [auction],
            "auction_feed": [],
            "current_auction_bid_history": [],
            "mission3_metrics": metrics,
        }
    )
    digest = hashlib.sha256(bundle).hexdigest()
    bundle_name = f"live_snapshot_{block}_{block_hash[2:]}_{digest}.json"
    status_value = {
            "schema_version": 1,
            "kind": "refresh_status",
            "latest_generated_block": block,
            "snapshot_block_hash": block_hash,
            "live_snapshot_bundle": bundle_name,
            "live_snapshot_bundle_sha256": digest,
            "live_snapshot_bundle_bytes": len(bundle),
            "live_snapshot_bundle_schema_version": 1,
            "onchain_chain_id": 8453,
            "onchain_verification_status": "current_snapshot_cross_provider_verified",
            "onchain_verification_scope": scope,
            "rpc_quorum_size": 2,
            "rpc_quorum_agreement": "2/2",
            "rpc_quorum_providers": providers,
            "snapshot_confirmations": 1,
        }
    if canonical_reorg_from_hash is not None:
        status_value["canonical_reorg_from_hash"] = canonical_reorg_from_hash
    status = canonical_json(status_value)
    for prefix in (repo / "generated", repo / "public" / "generated"):
        write_bytes(prefix / bundle_name, bundle)
        write_bytes(prefix / "refresh_status.json", status)
    return status, bundle


def commit_snapshot(
    repo: Path,
    *,
    amount_wei: str,
    message: str,
    block_hash_override: str | None = None,
    canonical_reorg_from_hash: str | None = None,
) -> tuple[str, bytes, bytes]:
    status, bundle = write_snapshot(
        repo,
        amount_wei=amount_wei,
        block_hash_override=block_hash_override,
        canonical_reorg_from_hash=canonical_reorg_from_hash,
    )
    run_git(repo, "add", "generated", "public/generated")
    run_git(repo, "commit", "-q", "-m", message)
    commit = run_git(repo, "rev-parse", "HEAD").decode().strip()
    return commit, status, bundle


def make_repo(root: Path) -> tuple[str, bytes, bytes]:
    run_git(root, "init", "-q")
    run_git(root, "config", "user.name", "Coverage Test")
    run_git(root, "config", "user.email", "coverage@example.invalid")
    return commit_snapshot(root, amount_wei="10000000000000000", message="valid snapshot")


def publication_target(
    *,
    amount_wei: str = "10000000000000000",
    block_hash: str | None = None,
    canonical_reorg_from_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "observation": {
            "confirmed_block_number": 123,
            "confirmed_block_hash": block_hash or snapshot_hash(123),
            "confirmed_block_time_utc": "2026-08-30T12:34:00Z",
            "token_id": "7",
            "amount_wei": amount_wei,
            "start_time_unix": "1780000000",
            "end_time_unix": "1780003600",
            "bidder_wallet": "0x" + "1" * 40,
            "settled": False,
            "event_name": None,
            "event_tx_hash": None,
            "event_log_index": None,
            "event_block_number": None,
            "event_block_hash": None,
            "event_block_time_utc": None,
            "canonical_reorg_from_hash": canonical_reorg_from_hash,
        }
    }


def assert_coverage_error(function: Any, needle: str) -> None:
    try:
        function()
    except Exception as exc:
        assert exc.__class__.__name__ == "CoverageValidationError", repr(exc)
        assert needle in str(exc), str(exc)
        return
    raise AssertionError(f"expected CoverageValidationError containing {needle!r}")


class changed_environment:
    def __init__(self, updates: dict[str, str]) -> None:
        self.updates = updates
        self.previous: dict[str, str | None] = {}

    def __enter__(self) -> None:
        for key, value in self.updates.items():
            self.previous[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, *_unused: object) -> None:
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_immutable_reads_ignore_inherited_git_environment() -> None:
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as attacker_tmp:
        repo = Path(tmp)
        attacker = Path(attacker_tmp)
        commit, _, _ = make_repo(repo)
        run_git(attacker, "init", "-q")
        poison = {
            "GIT_DIR": str(attacker / ".git"),
            "GIT_WORK_TREE": str(attacker),
            "GIT_OBJECT_DIRECTORY": str(attacker / ".git" / "objects"),
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.bare",
            "GIT_CONFIG_VALUE_0": "true",
            "GIT_REPLACE_REF_BASE": "refs/attacker-replacements/",
        }
        with changed_environment(poison):
            proof = coverage.extract_coverage_proof(
                repo,
                source_kind="generated_commit",
                source_commit_sha=commit,
                publication_target=publication_target(),
            )
        assert proof["source_commit_sha"] == commit
        assert proof["auction"]["amount_wei"] == "10000000000000000"


def test_immutable_reads_ignore_git_replace_objects() -> None:
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        original, _, _ = make_repo(repo)
        replacement, _, _ = commit_snapshot(
            repo,
            amount_wei="99999999999999999",
            message="attacker replacement",
        )
        run_git(repo, "replace", original, replacement)

        proof = coverage.extract_coverage_proof(
            repo,
            source_kind="generated_commit",
            source_commit_sha=original,
            publication_target=publication_target(),
        )
        assert proof["source_commit_sha"] == original
        assert proof["auction"]["amount_wei"] == "10000000000000000"


def test_baseline_check_ignores_local_worktree_and_alias_config() -> None:
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as attacker_tmp:
        repo = Path(tmp)
        attacker = Path(attacker_tmp)
        commit, _, _ = make_repo(repo)
        marker = attacker / "alias-ran"
        run_git(repo, "config", "core.worktree", str(attacker))
        run_git(repo, "config", "alias.show", f"!echo unsafe > {marker}")
        run_git(repo, "config", "alias.cat-file", f"!echo unsafe > {marker}")
        run_git(repo, "config", "alias.diff", f"!echo unsafe > {marker}")

        proof = coverage.extract_coverage_proof(
            repo,
            source_kind="baseline_no_diff",
            source_commit_sha=commit,
            publication_target=publication_target(),
        )
        assert proof["source_kind"] == "baseline_no_diff"
        assert not marker.exists()


def test_baseline_check_does_not_execute_local_clean_filters() -> None:
    if os.name != "posix":
        return
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        make_repo(repo)
        attributes = repo / ".gitattributes"
        attributes.write_text(
            "generated/** filter=coverage-evil\n"
            "public/generated/** filter=coverage-evil\n",
            encoding="utf-8",
        )
        run_git(repo, "add", ".gitattributes")
        run_git(repo, "commit", "-q", "-m", "tracked attributes")
        commit = run_git(repo, "rev-parse", "HEAD").decode().strip()
        marker = repo / "filter-ran"
        run_git(
            repo,
            "config",
            "filter.coverage-evil.clean",
            f"sh -c 'touch {marker}; cat'",
        )
        status_path = repo / "generated" / "refresh_status.json"
        status_path.write_bytes(status_path.read_bytes())
        current_mtime = status_path.stat().st_mtime
        os.utime(status_path, (current_mtime + 5, current_mtime + 5))

        proof = coverage.extract_coverage_proof(
            repo,
            source_kind="baseline_no_diff",
            source_commit_sha=commit,
            publication_target=publication_target(),
        )
        assert proof["source_kind"] == "baseline_no_diff"
        assert not marker.exists(), "baseline check executed a repository-configured filter"


def test_posix_immutable_reads_do_not_resolve_git_through_path() -> None:
    if os.name != "posix":
        return
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as fake_tmp:
        repo = Path(tmp)
        fake = Path(fake_tmp)
        commit, _, _ = make_repo(repo)
        marker = fake / "path-git-ran"
        fake_git = fake / "git"
        fake_git.write_text(
            "#!/bin/sh\nprintf invoked > \"$COVERAGE_FAKE_GIT_MARKER\"\nexit 97\n",
            encoding="utf-8",
        )
        fake_git.chmod(0o755)
        poison = {
            "PATH": str(fake) + os.pathsep + os.environ.get("PATH", ""),
            "COVERAGE_FAKE_GIT_MARKER": str(marker),
        }
        with changed_environment(poison):
            proof = coverage.extract_coverage_proof(
                repo,
                source_kind="generated_commit",
                source_commit_sha=commit,
                publication_target=publication_target(),
            )
        assert proof["source_commit_sha"] == commit
        assert not marker.exists()


def test_posix_git_executable_requires_trusted_file_metadata() -> None:
    if os.name != "posix":
        return
    coverage = load_module()
    assert coverage._attest_posix_git_executable(Path("/usr/bin/git")) == "/usr/bin/git"
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "git"
        fake.write_bytes(b"not a trusted executable\n")
        fake.chmod(0o775)
        assert_coverage_error(
            lambda: coverage._attest_posix_git_executable(fake),
            "trusted Git executable",
        )
        fake.unlink()
        fake.symlink_to("/usr/bin/git")
        assert_coverage_error(
            lambda: coverage._attest_posix_git_executable(fake),
            "trusted Git executable",
        )


def test_immutable_reads_reject_repository_alternate_object_databases() -> None:
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as alternate_tmp:
        repo = Path(tmp)
        alternate = Path(alternate_tmp)
        commit, _, _ = make_repo(repo)
        run_git(alternate, "init", "-q")
        alternates = repo / ".git" / "objects" / "info" / "alternates"
        alternates.parent.mkdir(parents=True, exist_ok=True)
        alternates.write_text(
            str((alternate / ".git" / "objects").resolve()).replace("\\", "/") + "\n",
            encoding="utf-8",
        )
        assert_coverage_error(
            lambda: coverage.extract_coverage_proof(
                repo,
                source_kind="generated_commit",
                source_commit_sha=commit,
                publication_target=publication_target(),
            ),
            "alternate object database",
        )


def test_public_artifact_reader_binds_bytes_to_validated_proof() -> None:
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        commit, expected_status, expected_bundle = make_repo(repo)
        proof = coverage.extract_coverage_proof(
            repo,
            source_kind="generated_commit",
            source_commit_sha=commit,
            publication_target=publication_target(),
        )
        status, bundle = coverage.read_proven_publication_artifacts(repo, proof)
        assert status == expected_status
        assert bundle == expected_bundle

        tampered = dict(proof)
        tampered["status_sha256"] = "f" * 64
        try:
            coverage.read_proven_publication_artifacts(repo, tampered)
        except coverage.CoverageValidationError as exc:
            assert "digest" in str(exc)
        else:
            raise AssertionError("artifact reader accepted bytes outside the proof digest")


def test_same_height_older_fork_cannot_synthesize_directional_reorg_evidence() -> None:
    coverage = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.name", "Coverage Test")
        run_git(repo, "config", "user.email", "coverage@example.invalid")
        source_hash = "0x" + "b" * 64
        commit, _, _ = commit_snapshot(
            repo,
            amount_wei="10000000000000000",
            message="same-height older fork",
            block_hash_override=source_hash,
        )
        assert_coverage_error(
            lambda: coverage.extract_coverage_proof(
                repo,
                source_kind="peer_commit",
                source_commit_sha=commit,
                publication_target=publication_target(),
            ),
            "does not cover",
        )


def test_same_height_directional_reorg_requires_immutable_authenticated_marker() -> None:
    coverage = load_module()
    state = load_state_module()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.name", "Coverage Test")
        run_git(repo, "config", "user.email", "coverage@example.invalid")
        target_hash = snapshot_hash(123)
        source_hash = "0x" + "b" * 64
        queue_root = repo / "queue-state"
        old = state.enqueue_latest_observation(
            queue_root,
            publication_target(block_hash=target_hash)["observation"],
            runner_id="windows-wsl",
            run_scope="current",
            created_at_utc="2026-08-30T12:34:56Z",
            lock_context=contextlib.nullcontext(),
        )
        replacement = state.enqueue_latest_observation(
            queue_root,
            publication_target(
                block_hash=source_hash,
                canonical_reorg_from_hash=target_hash,
            )["observation"],
            runner_id="windows-wsl",
            run_scope="current",
            created_at_utc="2026-08-30T12:35:56Z",
            canonical_reorg_quorum=True,
            lock_context=contextlib.nullcontext(),
        )
        assert replacement.generation == old.generation + 1
        assert replacement.record["observation"]["canonical_reorg_from_hash"] == target_hash
        commit, _, _ = commit_snapshot(
            repo,
            amount_wei="10000000000000000",
            message="authenticated same-height reorg",
            block_hash_override=source_hash,
            canonical_reorg_from_hash=target_hash,
        )
        proof = coverage.extract_coverage_proof(
            repo,
            source_kind="peer_commit",
            source_commit_sha=commit,
            publication_target=old.record,
        )
        assert proof["block_hash"] == source_hash
        assert proof["canonical_reorg_from_hash"] == target_hash


def main() -> None:
    tests: Iterator[Any] = iter(
        [
            test_immutable_reads_ignore_inherited_git_environment,
            test_immutable_reads_ignore_git_replace_objects,
            test_baseline_check_ignores_local_worktree_and_alias_config,
            test_baseline_check_does_not_execute_local_clean_filters,
            test_posix_immutable_reads_do_not_resolve_git_through_path,
            test_posix_git_executable_requires_trusted_file_metadata,
            test_immutable_reads_reject_repository_alternate_object_databases,
            test_public_artifact_reader_binds_bytes_to_validated_proof,
            test_same_height_older_fork_cannot_synthesize_directional_reorg_evidence,
            test_same_height_directional_reorg_requires_immutable_authenticated_marker,
        ]
    )
    count = 0
    for test in tests:
        test()
        count += 1
    print(f"publication_coverage_git_hardening_tests=pass count={count}")


if __name__ == "__main__":
    main()
