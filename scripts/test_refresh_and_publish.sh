#!/usr/bin/env bash
set -Eeuo pipefail

# Regression test: a failed generator must not poison every later scheduled run.

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_ROOT="$(mktemp -d -t degen-dogs-refresh-test.XXXXXX)"
TEST_REPO="${TEST_ROOT}/repo"
CROSS_DEVICE_ROOT=""

cleanup() {
  local status=$?
  if [[ "$status" != "0" && -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" ]]; then
    find "$TEST_ROOT" -name refresh.log -type f -exec sh -c 'printf "publisher fixture log: %s\n" "$1" >&2; tail -n 80 "$1" >&2' _ {} \;
  fi
  if [[ -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" && "$(basename "$TEST_ROOT")" == degen-dogs-refresh-test.* ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
  if [[ -n "${CROSS_DEVICE_ROOT:-}" && -d "$CROSS_DEVICE_ROOT" &&
    "$(dirname "$CROSS_DEVICE_ROOT")" == "/dev/shm" &&
    "$(basename "$CROSS_DEVICE_ROOT")" == degen-dogs-refresh-cross-device.* ]]; then
    rm -rf -- "$CROSS_DEVICE_ROOT"
  fi
}
trap cleanup EXIT

touch_valid_refresh_status() {
  local repo="$1"
  local refreshed_at="$2"
  python3 - "$repo" "$refreshed_at" <<'PY'
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])
payload = json.loads((repo / "generated/refresh_status.json").read_text(encoding="utf-8"))
payload["last_successful_refresh_time_utc"] = sys.argv[2]
text = json.dumps(payload, sort_keys=True) + "\n"
for relative in ("generated/refresh_status.json", "public/generated/refresh_status.json"):
    (repo / relative).write_text(text, encoding="utf-8")
subprocess.run(
    [sys.executable, "scripts/build_live_snapshot_bundle.py"],
    cwd=repo,
    check=True,
)
PY
}

write_fixture_recovery_journal() {
  local path="$1"
  local repo="$2"
  local baseline="$3"
  local run_id="$4"
  local run_scope="${5:-current}"
  local runner_id="${6:-fixture-local}"
  python3 - "$path" "$repo" "$baseline" "$run_id" "$run_scope" "$runner_id" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "repo_realpath": str(Path(sys.argv[2]).resolve()),
    "branch": "main",
    "baseline_head": sys.argv[3],
    "run_id": sys.argv[4],
    "run_scope": sys.argv[5],
    "runner_id": sys.argv[6],
    "created_at_utc": "2026-08-18T20:00:00Z",
    "publish_paths": [
        "README.md", "index.html", "generated", "public",
        "archive/mission3/data/generated", "archive/data/generated",
        "archive/data/identity/wallet_profiles.json", "archive/dogs",
        "archive/prices/data/generated", "archive/prices/data/raw",
    ],
}
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
}

write_fixture_publication_latest() {
  local lock_dir="$1"
  local generation="$2"
  python3 - "$SOURCE_DIR/runner_publication_state.py" "$lock_dir" "$generation" <<'PY'
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("runner_publication_state_fixture", module_path)
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state
spec.loader.exec_module(state)
root = Path(sys.argv[2])
generation = int(sys.argv[3])
record = state.latest_record(
    generation,
    "windows-wsl",
    "current",
    "2026-08-30T12:34:56Z",
    {
        "confirmed_block_number": 100,
        "confirmed_block_hash": "0x" + "a" * 64,
        "confirmed_block_time_utc": "2026-08-30T12:34:00Z",
        "token_id": "818",
        "amount_wei": "5500000000000000",
        "start_time_unix": "1780000000",
        "end_time_unix": "1780003600",
        "bidder_wallet": "0x" + "1" * 40,
        "settled": False,
        "event_name": "AuctionBid",
        "event_tx_hash": "0x" + "b" * 64,
        "event_log_index": 0,
        "event_block_number": 100,
        "event_block_hash": "0x" + "a" * 64,
        "event_block_time_utc": "2026-08-30T12:34:00Z",
        "canonical_reorg_from_hash": None,
    },
)
state.atomic_write_record(state.state_paths(root).latest, record)
print(state._digest(record))
PY
}

finalize_fixture_publication() {
  local lock_dir="$1"
  local generation="$2"
  local digest="$3"
  python3 - "$SOURCE_DIR/runner_publication_state.py" "$lock_dir" "$generation" "$digest" <<'PY'
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("runner_publication_state_fixture", module_path)
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state
spec.loader.exec_module(state)
if not state.finalize_pushed_handoff(Path(sys.argv[2]), int(sys.argv[3]), sys.argv[4]):
    raise SystemExit("fixture finalization did not acknowledge the exact generation")
PY
}

write_fixture_deferred_journal() {
  local lock_dir="$1"
  local repo="$2"
  local baseline="$3"
  local generation="$4"
  local digest="$5"
  local run_id="$6"
  local phase="$7"
  local commit="${8:-}"
  python3 - "$SOURCE_DIR/runner_publication_state.py" "$lock_dir" "$repo" "$baseline" \
    "$generation" "$digest" "$run_id" "$phase" "$commit" <<'PY'
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

module_path, lock_name, repo_name, baseline, generation_raw, digest, run_id, phase, commit = sys.argv[1:]
spec = importlib.util.spec_from_file_location("runner_publication_state_fixture", Path(module_path))
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state
spec.loader.exec_module(state)
lock_dir = Path(lock_name)
repo = Path(repo_name)
generation = int(generation_raw)
journal = {
    "schema_version": 1,
    "repo_realpath": str(repo.resolve()),
    "branch": "main",
    "baseline_head": baseline,
    "run_id": run_id,
    "runner_id": "windows-wsl",
    "run_scope": "current",
    "created_at_utc": "2026-08-30T12:49:00Z",
    "publish_paths": [
        "README.md", "index.html", "generated", "public",
        "archive/mission3/data/generated", "archive/data/generated",
        "archive/data/identity/wallet_profiles.json", "archive/dogs",
        "archive/prices/data/generated", "archive/prices/data/raw",
    ],
    "alignment_runner_commit": None,
    "alignment_remote_head": None,
    "alignment_result": None,
    "publication_generation": generation,
    "queue_digest": digest,
    "terminal_outcome": None,
    "handoff_phase": "generating",
    "remote_commit": None,
    "raw_status_path": None,
    "raw_bundle_path": None,
    "expected_bundle_sha256": None,
    "expected_bundle_bytes": None,
    "expected_block_number": None,
    "expected_block_hash": None,
    "push_completed_at_utc": None,
    "retry_deadline_utc": None,
    "retry_count": None,
}
state.create_deferred_recovery_journal(lock_dir, journal)
if phase in {"push_ready", "raw_proven"}:
    journal = state.arm_deferred_pushed_handoff(lock_dir, generation, digest, commit)
if phase == "raw_proven":
    status_path = "public/generated/refresh_status.json"
    status = json.loads(subprocess.check_output(["git", "show", f"{commit}:{status_path}"], cwd=repo))
    bundle_path = f"public/generated/{status['live_snapshot_bundle']}"
    bundle = subprocess.check_output(["git", "show", f"{commit}:{bundle_path}"], cwd=repo)
    assert len(bundle) == status["live_snapshot_bundle_bytes"]
    assert hashlib.sha256(bundle).hexdigest() == status["live_snapshot_bundle_sha256"]
    pending = {
        "schema_version": 1,
        "generation": generation,
        "queue_digest": digest,
        "commit_sha": commit,
        "raw_status_path": status_path,
        "raw_bundle_path": bundle_path,
        "expected_bundle_sha256": status["live_snapshot_bundle_sha256"],
        "expected_bundle_bytes": status["live_snapshot_bundle_bytes"],
        "expected_block_number": status["latest_generated_block"],
        "expected_block_hash": status["snapshot_block_hash"],
        "push_completed_at_utc": "2026-08-30T12:50:00Z",
        "retry_deadline_utc": "2026-08-30T13:00:00Z",
        "retry_count": 0,
    }
    checkpoint = {
        "schema_version": 1,
        "outcome": "pushed",
        "generation": generation,
        "queue_digest": digest,
        "commit_sha": commit,
        "push_completed_at_utc": "2026-08-30T12:50:00Z",
    }
    state.prepare_pushed_handoff(lock_dir, journal, pending, checkpoint)
elif phase == "terminal_no_diff":
    terminal = dict(journal)
    terminal["terminal_outcome"] = "no_diff"
    terminal["handoff_phase"] = "terminal"
    checkpoint = {
        "schema_version": 1,
        "outcome": "no_diff",
        "generation": generation,
        "queue_digest": digest,
        "commit_sha": None,
        "push_completed_at_utc": None,
    }
    state.record_terminal_outcome(lock_dir, terminal, checkpoint)
elif phase != "generating" and phase != "push_ready":
    raise SystemExit(f"unsupported fixture handoff phase: {phase}")
PY
}

run_deferred_fixture() {
  local lock_dir="$1"
  local generation="$2"
  local digest="$3"
  local log_dir="$4"
  local result_marker="$5"
  local raw_marker="${6:-}"
  local pages_marker="${7:-}"
  local refresh_lock="$lock_dir/refresh.lock"
  local status=0
  mkdir -m 700 -p "$lock_dir"
  : >"$refresh_lock"
  chmod 600 "$refresh_lock"
  exec {FIXTURE_DEFERRED_FD}<>"$refresh_lock"
  flock -n "$FIXTURE_DEFERRED_FD"
  if HOME="$TEST_ROOT/home" \
    VALIDATOR_MARKER="$SUCCESS_MARKER" \
    FIXTURE_RESULT_MARKER="$result_marker" \
    FIXTURE_RAW_VERIFY_MARKER="$raw_marker" \
    FIXTURE_PAGES_VERIFY_MARKER="$pages_marker" \
    FIXTURE_RAW_VERIFY_FAIL="${FIXTURE_RAW_VERIFY_FAIL:-0}" \
    DEGEN_DOGS_RUNNER_ID="windows-wsl" \
    DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
    DEGEN_DOGS_LOG_DIR="$log_dir" \
    DEGEN_DOGS_LOCK_DIR="$lock_dir" \
    DEGEN_DOGS_REFRESH_LOCK_PATH="$refresh_lock" \
    DEGEN_DOGS_LOCK_HELD=1 \
    DEGEN_DOGS_LOCK_FD="$FIXTURE_DEFERRED_FD" \
    DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1 \
    DEGEN_DOGS_PUBLICATION_GENERATION="$generation" \
    DEGEN_DOGS_PUBLICATION_DIGEST="$digest" \
    DEGEN_DOGS_SKIP_PULL=1 \
    DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
    DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
    DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
    DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
    "$SUCCESS_REPO/scripts/refresh_and_publish.sh"; then
    status=0
  else
    status=$?
  fi
  flock -u "$FIXTURE_DEFERRED_FD"
  exec {FIXTURE_DEFERRED_FD}>&-
  return "$status"
}

mkdir -p "$TEST_REPO/scripts" "$TEST_REPO/generated" "$TEST_REPO/node_modules" "$TEST_ROOT/home"
cp "$SOURCE_DIR/refresh_and_publish.sh" "$TEST_REPO/scripts/refresh_and_publish.sh"
cp "$SOURCE_DIR/runner_publication_state.py" "$TEST_REPO/scripts/runner_publication_state.py"
cp "$SOURCE_DIR/runner_permissions.sh" "$TEST_REPO/scripts/runner_permissions.sh"
cp "$SOURCE_DIR/runner_path_security.py" "$TEST_REPO/scripts/runner_path_security.py"
chmod +x "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'npm ci --ignore-scripts' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'artifact_rel_pattern.fullmatch(rel)' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'literal-pathspecs add --pathspec-from-file' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q -- '--force-with-lease="$lease"' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'Refresh-Runner-ID: ${RUNNER_ID}' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'Refresh-Run-Scope: ${RUN_SCOPE}' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'update_recovery_run_scope "full"' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'live_bundle_name.fullmatch(path.name)' "$TEST_REPO/scripts/refresh_and_publish.sh"
python3 - "$TEST_REPO/scripts/refresh_and_publish.sh" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1]).read_text(encoding="utf-8")
required = (
    "os.O_DIRECTORY | os.O_NOFOLLOW",
    "follow_symlinks=False",
    "src_dir_fd=source_parent_fd",
    "dst_dir_fd=destination_parent_fd",
)
missing = [fragment for fragment in required if fragment not in source]
if missing or "shutil.move(" in source:
    raise SystemExit(
        "quarantine must use no-follow directory descriptors and fd-relative rename; "
        f"missing={missing!r} path_move={'shutil.move(' in source}"
    )
PY

printf '%s\n' '{"scripts":{"refresh:current":"node scripts/fail_generation.js"}}' > "$TEST_REPO/package.json"
printf '%s\n' 'baseline' > "$TEST_REPO/generated/value.txt"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync('generated/value.txt', 'partial\\n');" \
  "fs.writeFileSync('generated/runner-created.json', '{}\\n');" \
  "process.exit(23);" > "$TEST_REPO/scripts/fail_generation.js"
touch "$TEST_REPO/node_modules/.package-lock.json"

git -C "$TEST_REPO" init -q -b main
git -C "$TEST_REPO" config user.name "Degen Dogs Test"
git -C "$TEST_REPO" config user.email "degen-dogs-test@example.invalid"
git -C "$TEST_REPO" add package.json scripts generated
git -C "$TEST_REPO" commit -qm "test baseline"

# Caller-provided run IDs are journal/commit provenance and must be rejected
# before any mutation if they cannot be represented by the recovery schema.
set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REFRESH_RUN_ID="invalid run id" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/invalid-run-id-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/invalid-run-id-locks" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" || "$(<"$TEST_REPO/generated/value.txt")" != "baseline" ]]; then
  echo "invalid recovery run ID was accepted or reached the generator" >&2
  exit 1
fi

# A recent index lock is not proven stale and must remain untouched. This also
# proves the generator cannot start while another git mutation may be active.
: >"$TEST_REPO/.git/index.lock"
chmod 600 "$TEST_REPO/.git/index.lock"
set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/recent-index-lock-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/recent-index-lock-locks" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" || ! -f "$TEST_REPO/.git/index.lock" ]]; then
  echo "recent git index lock was accepted or removed" >&2
  exit 1
fi
if [[ "$(<"$TEST_REPO/generated/value.txt")" != "baseline" ]]; then
  echo "generator ran despite a recent git index lock" >&2
  exit 1
fi
rm -- "$TEST_REPO/.git/index.lock"

# Reject a symlink in any shared-lock ancestor before creating anything through
# it or starting the generator.
LOCK_ATTACK_PARENT="$TEST_ROOT/lock-attack-parent"
LOCK_ATTACK_TARGET="$TEST_ROOT/lock-attack-target"
mkdir "$LOCK_ATTACK_PARENT" "$LOCK_ATTACK_TARGET"
ln -s "$LOCK_ATTACK_TARGET" "$LOCK_ATTACK_PARENT/redirect"
set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/lock-attack-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/lock-attack-safe-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$LOCK_ATTACK_PARENT/redirect/nested/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" ]]; then
  echo "nested symlink refresh-lock ancestor was accepted" >&2
  exit 1
fi
if [[ -e "$LOCK_ATTACK_TARGET/nested" ]]; then
  echo "refresh lock created files through a nested symlink ancestor" >&2
  exit 1
fi
if [[ "$(<"$TEST_REPO/generated/value.txt")" != "baseline" ]]; then
  echo "generator ran after unsafe refresh-lock rejection" >&2
  exit 1
fi

QUARANTINE_ATTACK_TARGET="$TEST_ROOT/quarantine-attack-target"
mkdir -p "$TEST_REPO/.local/recovery" "$QUARANTINE_ATTACK_TARGET"
chmod 700 "$TEST_REPO/.local" "$TEST_REPO/.local/recovery" "$QUARANTINE_ATTACK_TARGET"
ln -s "$QUARANTINE_ATTACK_TARGET" "$TEST_REPO/.local/recovery/fixture-quarantine"
set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/locks" \
DEGEN_DOGS_REFRESH_RUN_ID="fixture-quarantine" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e

if [[ "$status" == "0" ]]; then
  echo "expected the fixture generator to fail" >&2
  exit 1
fi
if [[ "$(<"$TEST_REPO/generated/value.txt")" != "baseline" ]]; then
  echo "tracked generated artifact was not restored" >&2
  exit 1
fi
if [[ -e "$TEST_REPO/generated/runner-created.json" ]]; then
  echo "runner-created untracked artifact was not removed" >&2
  exit 1
fi
if [[ -e "$QUARANTINE_ATTACK_TARGET/generated/runner-created.json" ]]; then
  echo "predictable quarantine run-id symlink received a runner artifact" >&2
  exit 1
fi
if [[ -z "$(find "$TEST_REPO/.local/recovery" -type f -path '*/generated/runner-created.json' -print -quit)" ]]; then
  echo "runner-created untracked artifact was not preserved in recovery quarantine" >&2
  exit 1
fi
if [[ -n "$(git -C "$TEST_REPO" status --porcelain --untracked-files=all -- generated)" ]]; then
  echo "generated path remains dirty after rollback" >&2
  git -C "$TEST_REPO" status --short --untracked-files=all -- generated >&2
  exit 1
fi
if ! grep -q "partial generated artifacts rolled back" "$TEST_ROOT/logs/refresh.log"; then
  echo "rollback completion was not logged" >&2
  exit 1
fi

printf '%s\n' '{"scripts":{"refresh:current":"node scripts/fail_generation.js","data":"node scripts/fail_full_generation.js"}}' > "$TEST_REPO/package.json"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync('generated/value.txt', 'incremental-partial\\n');" \
  "fs.writeFileSync('generated/runner-created.json', '{}\\n');" \
  "process.exit(75);" > "$TEST_REPO/scripts/fail_generation.js"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync('generated/full-fallback-marker.json', '{}\\n');" \
  "console.log('full builder fixture ran');" \
  "process.exit(24);" > "$TEST_REPO/scripts/fail_full_generation.js"
git -C "$TEST_REPO" add package.json scripts
git -C "$TEST_REPO" commit -qm "test full-refresh fallback"

set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/locks" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e

if [[ "$status" == "0" ]]; then
  echo "expected the full-builder fixture to fail after exit-75 fallback" >&2
  exit 1
fi
if [[ "$(<"$TEST_REPO/generated/value.txt")" != "baseline" ]]; then
  echo "tracked artifact was not restored after full-builder fallback failure" >&2
  exit 1
fi
if [[ -e "$TEST_REPO/generated/runner-created.json" || -e "$TEST_REPO/generated/full-fallback-marker.json" ]]; then
  echo "runner-created artifacts remain after full-builder fallback failure" >&2
  exit 1
fi
if [[ "$(find "$TEST_REPO/.local/recovery" -type f -name '*.json' | wc -l | tr -d ' ')" -lt 3 ]]; then
  echo "full-fallback untracked artifacts were not preserved in recovery quarantine" >&2
  exit 1
fi

# systemd exposes the repository and lock/cache directory as separate writable
# bind mounts. A failing generator must still quarantine its untracked output
# atomically on the repository mount while keeping the recovery journal on the
# independent cache mount.
if [[ -d /dev/shm && -w /dev/shm ]]; then
  CROSS_DEVICE_ROOT="$(mktemp -d /dev/shm/degen-dogs-refresh-cross-device.XXXXXX)"
  chmod 700 "$CROSS_DEVICE_ROOT"
  if python3 - "$TEST_REPO" "$CROSS_DEVICE_ROOT" <<'PY'
import os
import sys

raise SystemExit(0 if os.stat(sys.argv[1]).st_dev != os.stat(sys.argv[2]).st_dev else 1)
PY
  then
    set +e
    HOME="$TEST_ROOT/home" \
    DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
    DEGEN_DOGS_LOG_DIR="$TEST_ROOT/cross-device-logs" \
    DEGEN_DOGS_LOCK_DIR="$CROSS_DEVICE_ROOT" \
    DEGEN_DOGS_REFRESH_RUN_ID="cross-device-fixture" \
    DEGEN_DOGS_SKIP_PULL=1 \
    DEGEN_DOGS_SKIP_PUSH=1 \
    "$TEST_REPO/scripts/refresh_and_publish.sh"
    status=$?
    set -e
    if [[ "$status" == "0" ]]; then
      echo "expected the cross-device fixture generator to fail" >&2
      exit 1
    fi
    if [[ "$(<"$TEST_REPO/generated/value.txt")" != "baseline" ]] || \
      [[ -e "$TEST_REPO/generated/runner-created.json" ]] || \
      [[ -e "$CROSS_DEVICE_ROOT/publisher-recovery.json" ]]; then
      echo "cross-device rollback did not restore a clean baseline" >&2
      exit 1
    fi
    if [[ -z "$(find "$TEST_REPO/.local/recovery" -type f \
      -path '*/cross-device-fixture.*/generated/runner-created.json' -print -quit)" ]]; then
      echo "cross-device rollback did not preserve the runner-created artifact on the repository mount" >&2
      exit 1
    fi
    if grep -q "refusing cross-device quarantine move" "$TEST_ROOT/cross-device-logs/refresh.log" || \
      ! grep -q "partial generated artifacts rolled back" "$TEST_ROOT/cross-device-logs/refresh.log"; then
      echo "cross-device rollback did not complete without an EXDEV warning" >&2
      exit 1
    fi
  fi
fi
if [[ -n "$(git -C "$TEST_REPO" status --porcelain --untracked-files=all -- generated)" ]]; then
  echo "generated path remains dirty after full-builder fallback rollback" >&2
  exit 1
fi
if ! grep -q "bounded current refresh requires full reconciliation" "$TEST_ROOT/logs/refresh.log"; then
  echo "exit 75 did not trigger the full builder fallback" >&2
  exit 1
fi
if ! grep -q "full builder fixture ran" "$TEST_ROOT/logs/refresh.log"; then
  echo "full builder was not executed after exit 75" >&2
  exit 1
fi

# A successful snapshot must stage tracked artifact deletions, leave no partial
# publish-path diff behind, and run the observed/onchain dashboard validator.
SUCCESS_REPO="${TEST_ROOT}/success-repo"
SUCCESS_MARKER="${TEST_ROOT}/validator-ran"
mkdir -p \
  "$SUCCESS_REPO/scripts" \
  "$SUCCESS_REPO/scripts/runtime-bin" \
  "$SUCCESS_REPO/generated" \
  "$SUCCESS_REPO/public/generated" \
  "$SUCCESS_REPO/node_modules"
cp "$SOURCE_DIR/refresh_and_publish.sh" "$SUCCESS_REPO/scripts/refresh_and_publish.sh"
cp "$SOURCE_DIR/runner_publication_state.py" "$SUCCESS_REPO/scripts/runner_publication_state.py"
cp "$SOURCE_DIR/runner_permissions.sh" "$SUCCESS_REPO/scripts/runner_permissions.sh"
cp "$SOURCE_DIR/runner_path_security.py" "$SUCCESS_REPO/scripts/runner_path_security.py"
cp "$SOURCE_DIR/refresh_telemetry.py" "$SUCCESS_REPO/scripts/refresh_telemetry_validator.py"
cp "$SOURCE_DIR/runtime-bin/python3" "$SUCCESS_REPO/scripts/runtime-bin/python3"
cp "$SOURCE_DIR/build_live_snapshot_bundle.py" "$SUCCESS_REPO/scripts/build_live_snapshot_bundle.py"
chmod +x "$SUCCESS_REPO/scripts/refresh_and_publish.sh"
printf '%s\n' \
  '{"scripts":{' \
  '"refresh:current":"node scripts/success_generation.js && python3 scripts/build_live_snapshot_bundle.py",' \
  '"data":"node scripts/success_generation.js && python3 scripts/build_live_snapshot_bundle.py",' \
  '"archive:mission3:index":"node scripts/archive_generation.js",' \
  '"archive:mission3:health":"node -e \"\"",' \
  '"validate:dashboard":"node scripts/validate_marker.js",' \
  '"archive:prices:validate":"node -e \"\"",' \
  '"check:historical-dogs":"node -e \"\"",' \
  '"build":"node -e \"\""' \
  '}}' >"$SUCCESS_REPO/package.json"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.rmSync('generated/obsolete.json');" \
  "fs.rmSync('public/generated/obsolete.json');" \
  "fs.writeFileSync('generated/auction_feed.csv', 'id\\n2\\n');" \
  "fs.writeFileSync('generated/auction_feed.json', '[{\\\"id\\\":2}]\\n');" \
  "fs.writeFileSync('public/generated/auction_feed.csv', 'id\\n2\\n');" \
  "fs.writeFileSync('public/generated/auction_feed.json', '[{\\\"id\\\":2}]\\n');" \
  "const validStatus = fs.readFileSync('scripts/fixture_valid_refresh_status.json');" \
  "fs.writeFileSync('generated/refresh_status.json', validStatus);" \
  "fs.writeFileSync('public/generated/refresh_status.json', validStatus);" >"$SUCCESS_REPO/scripts/success_generation.js"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.mkdirSync('archive/mission3/data/generated', {recursive:true});" \
  "fs.writeFileSync('archive/mission3/data/generated/archive_scope_fixture.json', '{\\\"state\\\":\\\"regenerated\\\"}\\n');" \
  >"$SUCCESS_REPO/scripts/archive_generation.js"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync(process.env.VALIDATOR_MARKER, 'validated\\n');" >"$SUCCESS_REPO/scripts/validate_marker.js"
printf '%s\n' '# fixture compiles' >"$SUCCESS_REPO/scripts/build_dashboard.py"
printf '%s\n' \
  '#!/usr/bin/env python3' \
  'from __future__ import annotations' \
  'import json' \
  'import os' \
  'import re' \
  'import subprocess' \
  'import sys' \
  'from pathlib import Path' \
  'def immutable_raw_status_url(commit_sha: str) -> str:' \
  '    if re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:' \
  '        raise RuntimeError("fixture raw proof rejected a noncanonical commit")' \
  '    return f"https://raw.githubusercontent.com/ael-dev3/Degen-Dogs-Mission-3/{commit_sha}/public/generated/refresh_status.json"' \
  'def fetch_verified_remote_snapshot(source: str, status_url: str, expected_status: dict[str, object], expected_bundle: bytes) -> None:' \
  '    if source != "raw_commit" or not expected_bundle or not isinstance(expected_status, dict):' \
  '        raise RuntimeError("fixture raw proof received invalid exact content")' \
  '    if os.environ.get("FIXTURE_RAW_VERIFY_FAIL") == "1":' \
  '        raise RuntimeError("fixture forced immutable raw proof failure")' \
  '    marker = os.environ.get("FIXTURE_RAW_VERIFY_MARKER")' \
  '    if marker:' \
  '        Path(marker).write_text(status_url + "\n" + str(expected_status.get("live_snapshot_bundle")) + "\n", encoding="utf-8")' \
  'def main() -> int:' \
  '    if len(sys.argv) > 1 and sys.argv[1] == "record-refresh" and os.environ.get("FIXTURE_RESULT_MARKER"):' \
  '        Path(os.environ["FIXTURE_RESULT_MARKER"]).write_text(os.environ.get("DEGEN_DOGS_REFRESH_RESULT", "") + "\n" + os.environ.get("DEGEN_DOGS_COMMIT_SHA", "") + "\n", encoding="utf-8")' \
  '        return 0' \
  '    if "validate-status" in sys.argv[1:]:' \
  '        root = Path(sys.argv[sys.argv.index("--root") + 1]) if "--root" in sys.argv else Path(__file__).resolve().parents[1]' \
  '        validator = Path(__file__).with_name("refresh_telemetry_validator.py")' \
  '        return subprocess.run([sys.executable, str(validator), "--root", str(root), "validate-status"], check=False).returncode' \
  '    if len(sys.argv) > 1 and sys.argv[1] == "verify-live":' \
  '        marker = os.environ.get("FIXTURE_PAGES_VERIFY_MARKER")' \
  '        if marker:' \
  '            Path(marker).write_text("pages verification invoked\n", encoding="utf-8")' \
  '        if os.environ.get("FIXTURE_LIVE_TIMEOUT") == "1":' \
  '            env_path = Path(sys.argv[sys.argv.index("--env-file") + 1])' \
  '            env_path.write_text("export DEGEN_DOGS_LIVE_VERIFY_RESULT='"'"'timeout'"'"'\nexport DEGEN_DOGS_RAW_COMMIT_VERIFIED='"'"'True'"'"'\nexport DEGEN_DOGS_LIVE_VERIFY_ERROR='"'"'github_pages mismatch'"'"'\n", encoding="utf-8")' \
  '            return 2' \
  '    return 0' \
  'if __name__ == "__main__":' \
  '    raise SystemExit(main())' >"$SUCCESS_REPO/scripts/refresh_telemetry.py"
chmod +x "$SUCCESS_REPO/scripts/refresh_telemetry.py"
printf '%s\n' 'table,file,rows' 'auction_feed,generated/auction_feed.csv,1' >"$SUCCESS_REPO/generated/manifest.csv"
printf '%s\n' '{}' >"$SUCCESS_REPO/generated/manifest.json"
cp "$SUCCESS_REPO/generated/manifest.csv" "$SUCCESS_REPO/public/generated/manifest.csv"
cp "$SUCCESS_REPO/generated/manifest.json" "$SUCCESS_REPO/public/generated/manifest.json"
printf '%s\n' 'id' '1' >"$SUCCESS_REPO/generated/auction_feed.csv"
printf '%s\n' '[{"id":1}]' >"$SUCCESS_REPO/generated/auction_feed.json"
cp "$SUCCESS_REPO/generated/auction_feed.csv" "$SUCCESS_REPO/public/generated/auction_feed.csv"
cp "$SUCCESS_REPO/generated/auction_feed.json" "$SUCCESS_REPO/public/generated/auction_feed.json"
cp "$SOURCE_DIR/../generated/refresh_status.json" "$SUCCESS_REPO/generated/refresh_status.json"
cp "$SOURCE_DIR/../public/generated/refresh_status.json" "$SUCCESS_REPO/public/generated/refresh_status.json"
cp "$SOURCE_DIR/../generated/current_auction.json" "$SUCCESS_REPO/generated/current_auction.json"
cp "$SOURCE_DIR/../public/generated/current_auction.json" "$SUCCESS_REPO/public/generated/current_auction.json"
cp "$SOURCE_DIR/../generated/current_auction_bid_history.json" "$SUCCESS_REPO/generated/current_auction_bid_history.json"
cp "$SOURCE_DIR/../public/generated/current_auction_bid_history.json" "$SUCCESS_REPO/public/generated/current_auction_bid_history.json"
cp "$SOURCE_DIR/../generated/auction_feed.json" "$SUCCESS_REPO/generated/auction_feed.json"
cp "$SOURCE_DIR/../public/generated/auction_feed.json" "$SUCCESS_REPO/public/generated/auction_feed.json"
cp "$SOURCE_DIR/../generated/mission3_metrics.json" "$SUCCESS_REPO/generated/mission3_metrics.json"
cp "$SOURCE_DIR/../public/generated/mission3_metrics.json" "$SUCCESS_REPO/public/generated/mission3_metrics.json"
cp "$SOURCE_DIR/../generated/refresh_status.json" "$SUCCESS_REPO/scripts/fixture_valid_refresh_status.json"
cp "$SOURCE_DIR/../public/generated/unified_dog_search_index.json" "$SUCCESS_REPO/public/generated/unified_dog_search_index.json"
cp "$SOURCE_DIR"/../generated/live_snapshot_*.json "$SUCCESS_REPO/generated/"
cp "$SOURCE_DIR"/../public/generated/live_snapshot_*.json "$SUCCESS_REPO/public/generated/"
printf '%s\n' \
  'metric,value' \
  'site_url,https://ael-dev3.github.io/Degen-Dogs-Mission-3/' \
  'latest_block,123456' \
  'current_auction_token_id,789' >"$SUCCESS_REPO/generated/mission3_metrics.csv"
printf '%s\n' '{"retired":true}' >"$SUCCESS_REPO/generated/obsolete.json"
printf '%s\n' '{"retired":true}' >"$SUCCESS_REPO/public/generated/obsolete.json"
printf '%s\n' '<table data-table="auction_feed"></table><table data-table="mission3_metrics">site_url latest_block</table>' >"$SUCCESS_REPO/index.html"
printf '%s\n' '# fixture' >"$SUCCESS_REPO/README.md"
touch "$SUCCESS_REPO/node_modules/.package-lock.json"
git -C "$SUCCESS_REPO" init -q -b main
git -C "$SUCCESS_REPO" config user.name "Degen Dogs Test"
git -C "$SUCCESS_REPO" config user.email "degen-dogs-test@example.invalid"
git -C "$SUCCESS_REPO" add .
git -C "$SUCCESS_REPO" commit -qm "successful baseline"

# Exercise the same pinned runtime selection as production before the first
# success-repository publisher invocation, including archive recovery below.
python3 -m venv "$SUCCESS_REPO/.venv"
"$SUCCESS_REPO/.venv/bin/python3" -m pip install --require-hashes -r "$SOURCE_DIR/../requirements.txt"

# A proven-stale, safely permissioned, owned index lock left by a crash must be removed
# before preflight so it cannot block staging or rollback later in the run.
: >"$SUCCESS_REPO/.git/index.lock"
# Git commonly creates this as 0644 under umask 022. This fixture must reflect
# the real crash artifact rather than only the runner's stricter umask 077.
chmod 644 "$SUCCESS_REPO/.git/index.lock"
touch -t 200001010000 "$SUCCESS_REPO/.git/index.lock"

HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/success-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/success-locks" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

[[ "$(<"$SUCCESS_MARKER")" == "validated" ]]
if [[ -e "$SUCCESS_REPO/.git/index.lock" ]]; then
  echo "proven-stale git index lock was not removed" >&2
  exit 1
fi
if ! grep -q "removed proven-stale git index lock before publisher preflight" "$TEST_ROOT/success-logs/refresh.log"; then
  echo "stale git index lock recovery was not logged" >&2
  exit 1
fi
if git -C "$SUCCESS_REPO" cat-file -e HEAD^:generated/obsolete.json 2>/dev/null && \
  git -C "$SUCCESS_REPO" cat-file -e HEAD:generated/obsolete.json 2>/dev/null; then
  echo "tracked generated deletion was not committed" >&2
  exit 1
fi
if git -C "$SUCCESS_REPO" cat-file -e HEAD:public/generated/obsolete.json 2>/dev/null; then
  echo "tracked public deletion was not committed" >&2
  exit 1
fi
LIVE_BUNDLE_FIXTURE="$(python3 - "$SUCCESS_REPO/generated/refresh_status.json" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8"))["live_snapshot_bundle"])
PY
)"
if ! git -C "$SUCCESS_REPO" cat-file -e "HEAD:generated/$LIVE_BUNDLE_FIXTURE" 2>/dev/null || \
  ! git -C "$SUCCESS_REPO" cat-file -e "HEAD:public/generated/$LIVE_BUNDLE_FIXTURE" 2>/dev/null; then
  echo "status-referenced immutable live snapshot mirrors were not included in the publisher inventory" >&2
  exit 1
fi
if ! git -C "$SUCCESS_REPO" cat-file -e HEAD:generated/mission3_metrics.json 2>/dev/null || \
  ! git -C "$SUCCESS_REPO" cat-file -e HEAD:public/generated/mission3_metrics.json 2>/dev/null; then
  echo "peer-validation fixture omitted a mission3_metrics mirror" >&2
  exit 1
fi
if [[ -n "$(git -C "$SUCCESS_REPO" status --porcelain --untracked-files=all -- README.md index.html generated public)" ]]; then
  echo "successful publisher left an unstaged publish-path diff" >&2
  git -C "$SUCCESS_REPO" status --short --untracked-files=all -- README.md index.html generated public >&2
  exit 1
fi

# A power loss after mutation cannot run the Bash EXIT trap. The next locked
# run must authenticate the private baseline journal, restore tracked files,
# quarantine runner-created untracked artifacts, and continue automatically.
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync('generated/auction_feed.csv', 'id\\n2\\n');" \
  "fs.writeFileSync('generated/auction_feed.json', '[{\\\"id\\\":2}]\\n');" \
  "fs.writeFileSync('public/generated/auction_feed.csv', 'id\\n2\\n');" \
  "fs.writeFileSync('public/generated/auction_feed.json', '[{\\\"id\\\":2}]\\n');" \
  "const validStatus = fs.readFileSync('scripts/fixture_valid_refresh_status.json');" \
  "fs.writeFileSync('generated/refresh_status.json', validStatus);" \
  "fs.writeFileSync('public/generated/refresh_status.json', validStatus);" >"$SUCCESS_REPO/scripts/success_generation.js"
git -C "$SUCCESS_REPO" add scripts/success_generation.js
git -C "$SUCCESS_REPO" commit -qm "fixture: stable recovery generator"
CRASH_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
CRASH_LOCK_DIR="$TEST_ROOT/crash-recovery-locks"
CRASH_CONFIG_LOCK_DIR="$TEST_ROOT/crash-config-locks"
mkdir -m 700 "$CRASH_LOCK_DIR"
python3 - "$CRASH_LOCK_DIR/publisher-recovery.json" "$SUCCESS_REPO" "$CRASH_BASELINE" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "repo_realpath": str(Path(sys.argv[2]).resolve()),
    "branch": "main",
    "baseline_head": sys.argv[3],
    "run_id": "crash-fixture",
    "runner_id": "fixture-local",
    "run_scope": "current",
    "created_at_utc": "2026-08-09T00:00:00Z",
    "publish_paths": [
        "README.md",
        "index.html",
        "generated",
        "public",
        "archive/mission3/data/generated",
        "archive/data/generated",
        "archive/data/identity/wallet_profiles.json",
        "archive/dogs",
        "archive/prices/data/generated",
        "archive/prices/data/raw",
    ],
}
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
printf '%s\n' '{"partial":true}' >"$SUCCESS_REPO/generated/auction_feed.json"
printf '%s\n' '{"runner_crash":true}' >"$SUCCESS_REPO/generated/crash-only.json"

HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/crash-recovery-logs" \
DEGEN_DOGS_LOCK_DIR="$CRASH_CONFIG_LOCK_DIR" \
MISSION3_REFRESH_LOCK_PATH="$CRASH_LOCK_DIR/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

if [[ -e "$CRASH_LOCK_DIR/publisher-recovery.json" ]] || \
  [[ "$(<"$SUCCESS_REPO/generated/auction_feed.json")" != '[{"id":2}]' ]] || \
  [[ -e "$SUCCESS_REPO/generated/crash-only.json" ]]; then
  echo "authenticated interrupted-generation recovery did not restore the baseline" >&2
  exit 1
fi
if ! grep -q "interrupted publisher recovery completed" "$TEST_ROOT/crash-recovery-logs/refresh.log" || \
  ! find "$SUCCESS_REPO/.local/recovery" -type f -name crash-only.json -print -quit | grep -q .; then
  echo "interrupted-generation recovery was not logged or quarantined" >&2
  exit 1
fi
if [[ -n "$(git -C "$SUCCESS_REPO" status --porcelain --untracked-files=all -- README.md index.html generated public)" ]]; then
  echo "interrupted-generation recovery left publish artifacts dirty" >&2
  exit 1
fi

# A post-commit push failure must atomically rewind only the commit created by
# this runner. Otherwise one transient outage leaves main permanently ahead and
# every later scheduled refresh refuses to run.
REJECT_REMOTE="$TEST_ROOT/reject-remote.git"
git -C "$TEST_ROOT" init -q --bare --initial-branch=main "$REJECT_REMOTE"
git -C "$SUCCESS_REPO" remote add origin "$REJECT_REMOTE"
git -C "$SUCCESS_REPO" push -q -u origin main

# A crash after commit but before push is journal-authenticated too. If the
# remote still equals the recorded baseline, rewind that one allowlisted child
# commit and regenerate instead of leaving main permanently local-ahead.
INTERRUPTED_COMMIT_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
printf '%s\n' '[{"id":99}]' >"$SUCCESS_REPO/generated/auction_feed.json"
printf '%s\n' '[{"id":99}]' >"$SUCCESS_REPO/public/generated/auction_feed.json"
git -C "$SUCCESS_REPO" add generated/auction_feed.json public/generated/auction_feed.json
git -C "$SUCCESS_REPO" commit -qm "[cron] simulated interrupted publisher commit" \
  -m "Refresh-Run-ID: post-commit-crash-fixture" \
  -m "Refresh-Runner-ID: fixture-local" \
  -m "Refresh-Runner-ID: fixture-conflict" \
  -m "Refresh-Run-Scope: current"
python3 - "$CRASH_LOCK_DIR/publisher-recovery.json" "$SUCCESS_REPO" "$INTERRUPTED_COMMIT_BASELINE" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "repo_realpath": str(Path(sys.argv[2]).resolve()),
    "branch": "main",
    "baseline_head": sys.argv[3],
    "run_id": "post-commit-crash-fixture",
    "runner_id": "fixture-local",
    "run_scope": "current",
    "created_at_utc": "2026-08-09T00:00:00Z",
    "publish_paths": [
        "README.md", "index.html", "generated", "public",
        "archive/mission3/data/generated", "archive/data/generated",
        "archive/data/identity/wallet_profiles.json", "archive/dogs",
        "archive/prices/data/generated", "archive/prices/data/raw",
    ],
}
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY

UNATTRIBUTED_HEAD="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
UNATTRIBUTED_JOURNAL_HASH="$(git -C "$SUCCESS_REPO" hash-object "$CRASH_LOCK_DIR/publisher-recovery.json")"
UNATTRIBUTED_STATUS_BEFORE="$TEST_ROOT/unattributed-status-before"
UNATTRIBUTED_STATUS_AFTER="$TEST_ROOT/unattributed-status-after"
git -C "$SUCCESS_REPO" status --porcelain=v1 -z --untracked-files=all >"$UNATTRIBUTED_STATUS_BEFORE"
set +e
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/post-commit-refusal-logs" \
DEGEN_DOGS_LOCK_DIR="$CRASH_CONFIG_LOCK_DIR" \
MISSION3_REFRESH_LOCK_PATH="$CRASH_LOCK_DIR/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
git -C "$SUCCESS_REPO" status --porcelain=v1 -z --untracked-files=all >"$UNATTRIBUTED_STATUS_AFTER"
if [[ "$status" == "0" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$UNATTRIBUTED_HEAD" ]] || \
  ! cmp -s "$UNATTRIBUTED_STATUS_BEFORE" "$UNATTRIBUTED_STATUS_AFTER" || \
  [[ "$(git -C "$SUCCESS_REPO" hash-object "$CRASH_LOCK_DIR/publisher-recovery.json")" != "$UNATTRIBUTED_JOURNAL_HASH" ]]; then
  echo "refused journal attribution mutated HEAD, worktree, or recovery provenance" >&2
  exit 1
fi

git -C "$SUCCESS_REPO" commit --amend -q \
  -m "[cron] simulated interrupted publisher commit" \
  -m "Refresh-Runner-ID: fixture-local" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: post-commit-crash-fixture"

RECOVERY_COMMIT_MESSAGE="$(git -C "$SUCCESS_REPO" show -s --format=%B HEAD)"
if [[ "$(grep -Fxc 'Refresh-Run-ID: post-commit-crash-fixture' <<<"$RECOVERY_COMMIT_MESSAGE")" != "1" ]] || \
  [[ "$(grep -Fxc 'Refresh-Runner-ID: fixture-local' <<<"$RECOVERY_COMMIT_MESSAGE")" != "1" ]] || \
  [[ "$(grep -Fxc 'Refresh-Run-Scope: current' <<<"$RECOVERY_COMMIT_MESSAGE")" != "1" ]]; then
  echo "amended recovery fixture does not have exactly one canonical provenance trailer of each kind" >&2
  exit 1
fi

HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/post-commit-recovery-logs" \
DEGEN_DOGS_LOCK_DIR="$CRASH_CONFIG_LOCK_DIR" \
MISSION3_REFRESH_LOCK_PATH="$CRASH_LOCK_DIR/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

if [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$INTERRUPTED_COMMIT_BASELINE" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$INTERRUPTED_COMMIT_BASELINE" ]] || \
  [[ -e "$CRASH_LOCK_DIR/publisher-recovery.json" ]] || \
  ! grep -q "recovering unpushed interrupted publisher commit" "$TEST_ROOT/post-commit-recovery-logs/refresh.log"; then
  echo "post-commit crash journal did not safely rewind and reconcile" >&2
  exit 1
fi

# If another authenticated publisher wins after our commit but before recovery,
# its equal-or-newer verified snapshot supersedes the interrupted local child.
# Recovery must fast-forward, clear the journal, and exit successfully without
# regenerating a timestamp-only duplicate commit.
PEER_RECOVERY_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-18T20:01:00Z"
git -C "$SUCCESS_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$SUCCESS_REPO" commit -qm "[cron] interrupted local publisher" \
  -m "Refresh-Runner-ID: fixture-local" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: peer-recovery-local"
LOCAL_RECOVERY_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
PEER_RECOVERY_LOCKS="$TEST_ROOT/peer-recovery-locks"
write_fixture_recovery_journal \
  "$PEER_RECOVERY_LOCKS/publisher-recovery.json" \
  "$SUCCESS_REPO" \
  "$PEER_RECOVERY_BASELINE" \
  "peer-recovery-local"

PEER_RECOVERY_REPO="$TEST_ROOT/peer-recovery-repo"
git clone -q --branch main "$REJECT_REMOTE" "$PEER_RECOVERY_REPO"
git -C "$PEER_RECOVERY_REPO" config user.name "Degen Dogs Peer"
git -C "$PEER_RECOVERY_REPO" config user.email "degen-dogs-peer@example.invalid"
touch_valid_refresh_status "$PEER_RECOVERY_REPO" "2026-08-18T20:02:00Z"
git -C "$PEER_RECOVERY_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$PEER_RECOVERY_REPO" commit -qm "[cron] peer publisher winner" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: peer-recovery-winner"
PEER_RECOVERY_COMMIT="$(git -C "$PEER_RECOVERY_REPO" rev-parse HEAD)"
git -C "$PEER_RECOVERY_REPO" push -q origin main

rm -f -- "$SUCCESS_MARKER"
PEER_RECOVERY_RESULT="$TEST_ROOT/peer-recovery-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$PEER_RECOVERY_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/peer-recovery-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/peer-recovery-config-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$PEER_RECOVERY_LOCKS/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

if [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$PEER_RECOVERY_COMMIT" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$PEER_RECOVERY_COMMIT" ]] || \
  [[ -e "$PEER_RECOVERY_LOCKS/publisher-recovery.json" ]] || \
  [[ -e "$SUCCESS_MARKER" ]] || \
  [[ ! -e "$PEER_RECOVERY_RESULT" ]] || \
  [[ "$(sed -n '1p' "$PEER_RECOVERY_RESULT")" != "success_superseded_by_peer" ]] || \
  [[ "$(sed -n '2p' "$PEER_RECOVERY_RESULT")" != "$PEER_RECOVERY_COMMIT" ]]; then
  echo "authenticated peer recovery did not fast-forward and acknowledge without regeneration" >&2
  echo "local_interrupted_commit=${LOCAL_RECOVERY_COMMIT}" >&2
  exit 1
fi
if ! grep -q "interrupted publisher was safely superseded by peer commit" "$TEST_ROOT/peer-recovery-logs/refresh.log"; then
  echo "authenticated peer recovery supersession was not logged" >&2
  exit 1
fi

# A current-scope interrupted commit is only eligible for a peer no-op when the
# effective caller+journal scope remains current. A stronger full or archive
# caller must align to the peer and regenerate the requested wider surface.
for STRONGER_SCOPE in full archive; do
  STRONGER_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
  touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-18T20:02:30Z"
  git -C "$SUCCESS_REPO" add generated public/generated
  git -C "$SUCCESS_REPO" commit -qm "[cron] interrupted current publisher before ${STRONGER_SCOPE} caller" \
    -m "Refresh-Runner-ID: fixture-local" \
    -m "Refresh-Run-Scope: current" \
    -m "Refresh-Run-ID: current-journal-${STRONGER_SCOPE}-local"
  STRONGER_LOCAL_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
  STRONGER_LOCKS="$TEST_ROOT/current-journal-${STRONGER_SCOPE}-locks"
  write_fixture_recovery_journal \
    "$STRONGER_LOCKS/publisher-recovery.json" \
    "$SUCCESS_REPO" \
    "$STRONGER_BASELINE" \
    "current-journal-${STRONGER_SCOPE}-local"

  STRONGER_PEER_REPO="$TEST_ROOT/current-journal-${STRONGER_SCOPE}-peer-repo"
  git clone -q --branch main "$REJECT_REMOTE" "$STRONGER_PEER_REPO"
  git -C "$STRONGER_PEER_REPO" config user.name "Degen Dogs Stronger Scope Peer"
  git -C "$STRONGER_PEER_REPO" config user.email "degen-dogs-stronger-peer@example.invalid"
  touch_valid_refresh_status "$STRONGER_PEER_REPO" "2026-08-18T20:02:45Z"
  git -C "$STRONGER_PEER_REPO" add generated public/generated
  git -C "$STRONGER_PEER_REPO" commit -qm "[cron] bounded peer before ${STRONGER_SCOPE} recovery" \
    -m "Refresh-Runner-ID: fixture-peer" \
    -m "Refresh-Run-Scope: current" \
    -m "Refresh-Run-ID: current-journal-${STRONGER_SCOPE}-peer"
  STRONGER_PEER_COMMIT="$(git -C "$STRONGER_PEER_REPO" rev-parse HEAD)"
  git -C "$STRONGER_PEER_REPO" push -q origin main

  rm -f -- "$SUCCESS_MARKER"
  STRONGER_RESULT="$TEST_ROOT/current-journal-${STRONGER_SCOPE}-result"
  STRONGER_LOGS="$TEST_ROOT/current-journal-${STRONGER_SCOPE}-logs"
  if [[ "$STRONGER_SCOPE" == "full" ]]; then
    STRONGER_ENV=(DEGEN_DOGS_FULL_REFRESH=1)
  else
    STRONGER_ENV=(DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1)
  fi
  env \
    HOME="$TEST_ROOT/home" \
    VALIDATOR_MARKER="$SUCCESS_MARKER" \
    FIXTURE_RESULT_MARKER="$STRONGER_RESULT" \
    DEGEN_DOGS_RUNNER_ID="fixture-local" \
    DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
    DEGEN_DOGS_LOG_DIR="$STRONGER_LOGS" \
    DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/current-journal-${STRONGER_SCOPE}-config-locks" \
    DEGEN_DOGS_REFRESH_LOCK_PATH="$STRONGER_LOCKS/refresh.lock" \
    DEGEN_DOGS_SKIP_PULL=1 \
    DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
    DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
    DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
    DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
    DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
    "${STRONGER_ENV[@]}" \
    "$SUCCESS_REPO/scripts/refresh_and_publish.sh"

  STRONGER_REPAIR_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
  if [[ "$STRONGER_REPAIR_COMMIT" == "$STRONGER_PEER_COMMIT" ]] || \
    [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$STRONGER_REPAIR_COMMIT" ]] || \
    [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD^)" != "$STRONGER_PEER_COMMIT" ]] || \
    [[ "$(git -C "$SUCCESS_REPO" show -s --format=%B HEAD)" != *"Refresh-Run-Scope: ${STRONGER_SCOPE}"* ]] || \
    [[ "$(sed -n '1p' "$STRONGER_RESULT")" != "success_pushed" ]] || \
    [[ ! -e "$SUCCESS_MARKER" ]] || \
    [[ -e "$STRONGER_LOCKS/publisher-recovery.json" ]]; then
    echo "current journal plus ${STRONGER_SCOPE} caller was incorrectly superseded or scope-downgraded" >&2
    echo "local_interrupted_commit=${STRONGER_LOCAL_COMMIT}" >&2
    exit 1
  fi
  if ! grep -q "regenerating because peer coverage was not proven" "$STRONGER_LOGS/refresh.log"; then
    echo "current journal plus ${STRONGER_SCOPE} caller did not align before wider regeneration" >&2
    exit 1
  fi
done

# Simulate a hard interruption after the losing child was rewound and the
# remote winner was fast-forwarded, but before the alignment journal could be
# removed. Then an arbitrary commit must intervene before a runner-labeled
# descendant advances main. Recovery must align to that descendant and rebuild
# from it; it must not accept the labeled descendant as peer supersession.
ALIGN_REMOTE="$TEST_ROOT/alignment-remote.git"
git clone -q --bare "$REJECT_REMOTE" "$ALIGN_REMOTE"
ALIGN_REPO="$TEST_ROOT/alignment-repo"
ALIGN_PEER_REPO="$TEST_ROOT/alignment-peer-repo"
git clone -q --branch main "$ALIGN_REMOTE" "$ALIGN_REPO"
git clone -q --branch main "$ALIGN_REMOTE" "$ALIGN_PEER_REPO"
git -C "$ALIGN_REPO" config user.name "Degen Dogs Interrupted Alignment"
git -C "$ALIGN_REPO" config user.email "degen-dogs-alignment@example.invalid"
git -C "$ALIGN_PEER_REPO" config user.name "Degen Dogs Alignment Peer"
git -C "$ALIGN_PEER_REPO" config user.email "degen-dogs-alignment-peer@example.invalid"
ALIGN_BASELINE="$(git -C "$ALIGN_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$ALIGN_REPO" "2026-08-18T20:03:00Z"
git -C "$ALIGN_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$ALIGN_REPO" commit -qm "[cron] alignment interrupted local" \
  -m "Refresh-Runner-ID: fixture-local" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: alignment-local"
ALIGN_LOCAL_COMMIT="$(git -C "$ALIGN_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$ALIGN_PEER_REPO" "2026-08-18T20:04:00Z"
git -C "$ALIGN_PEER_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$ALIGN_PEER_REPO" commit -qm "[cron] alignment peer winner" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: alignment-peer"
ALIGN_PEER_COMMIT="$(git -C "$ALIGN_PEER_REPO" rev-parse HEAD)"
git -C "$ALIGN_PEER_REPO" push -q origin main
git -C "$ALIGN_REPO" fetch -q origin main
ALIGN_LOCKS="$TEST_ROOT/alignment-locks"
write_fixture_recovery_journal \
  "$ALIGN_LOCKS/publisher-recovery.json" \
  "$ALIGN_REPO" \
  "$ALIGN_BASELINE" \
  "alignment-local"
python3 - "$ALIGN_LOCKS/publisher-recovery.json" "$ALIGN_LOCAL_COMMIT" "$ALIGN_PEER_COMMIT" <<'PY'
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["alignment_runner_commit"] = sys.argv[2]
payload["alignment_remote_head"] = sys.argv[3]
payload["alignment_result"] = "peer_supersedes"
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
git -C "$ALIGN_REPO" update-ref refs/heads/main "$ALIGN_BASELINE" "$ALIGN_LOCAL_COMMIT"
git -C "$ALIGN_REPO" restore --source="$ALIGN_BASELINE" --staged --worktree -- .
git -C "$ALIGN_REPO" merge -q --ff-only "$ALIGN_PEER_COMMIT"
printf '%s\n' 'uninspected intervening history' >"$ALIGN_PEER_REPO/uninspected-intervening.txt"
git -C "$ALIGN_PEER_REPO" add uninspected-intervening.txt
git -C "$ALIGN_PEER_REPO" commit -qm "manual intervening history"
ALIGN_INTERVENING_COMMIT="$(git -C "$ALIGN_PEER_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$ALIGN_PEER_REPO" "2026-08-18T20:05:00Z"
git -C "$ALIGN_PEER_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$ALIGN_PEER_REPO" commit -qm "[cron] labeled peer after intervening history" \
  -m "Refresh-Runner-ID: fixture-peer-2" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: alignment-peer-latest"
ALIGN_LABELED_DESCENDANT_COMMIT="$(git -C "$ALIGN_PEER_REPO" rev-parse HEAD)"
git -C "$ALIGN_PEER_REPO" push -q origin main

ALIGN_RESULT="$TEST_ROOT/alignment-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$ALIGN_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$ALIGN_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/alignment-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/alignment-config-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$ALIGN_LOCKS/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$ALIGN_REPO/scripts/refresh_and_publish.sh"

ALIGN_RESTART_COMMIT="$(git -C "$ALIGN_REPO" rev-parse HEAD)"
if [[ "$ALIGN_RESTART_COMMIT" == "$ALIGN_LABELED_DESCENDANT_COMMIT" ]] || \
  [[ "$(git --git-dir="$ALIGN_REMOTE" rev-parse main)" != "$ALIGN_RESTART_COMMIT" ]] || \
  [[ "$(git -C "$ALIGN_REPO" rev-parse HEAD^)" != "$ALIGN_LABELED_DESCENDANT_COMMIT" ]] || \
  [[ "$(git -C "$ALIGN_PEER_REPO" rev-parse "$ALIGN_LABELED_DESCENDANT_COMMIT^")" != "$ALIGN_INTERVENING_COMMIT" ]] || \
  [[ -e "$ALIGN_LOCKS/publisher-recovery.json" ]] || \
  [[ "$(sed -n '1p' "$ALIGN_RESULT")" != "success_pushed" ]] || \
  [[ "$(sed -n '2p' "$ALIGN_RESULT")" != "$ALIGN_RESTART_COMMIT" ]]; then
  echo "labeled descendant after intervening history was accepted as peer supersession" >&2
  exit 1
fi
if ! grep -q "advanced interrupted publisher alignment target from ${ALIGN_PEER_COMMIT} to ${ALIGN_LABELED_DESCENDANT_COMMIT}" \
  "$TEST_ROOT/alignment-logs/refresh.log" || \
  ! grep -q "completed interrupted publisher alignment" "$TEST_ROOT/alignment-logs/refresh.log"; then
  echo "interrupted remote descendant did not align before bounded regeneration" >&2
  exit 1
fi

# A publisher-shaped remote commit with a suffix-only "verified" claim is not
# a valid peer winner. Canonical refresh-status validation must detect its
# disagreement with mission3_metrics/current_auction, align safely, regenerate,
# and publish a repaired child instead of acknowledging false success.
INVALID_PEER_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-18T20:05:00Z"
git -C "$SUCCESS_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$SUCCESS_REPO" commit -qm "[cron] interrupted local before invalid peer" \
  -m "Refresh-Runner-ID: fixture-local" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: invalid-peer-local"
INVALID_LOCAL_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
INVALID_PEER_LOCKS="$TEST_ROOT/invalid-peer-locks"
write_fixture_recovery_journal \
  "$INVALID_PEER_LOCKS/publisher-recovery.json" \
  "$SUCCESS_REPO" \
  "$INVALID_PEER_BASELINE" \
  "invalid-peer-local"

INVALID_PEER_REPO="$TEST_ROOT/invalid-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$INVALID_PEER_REPO"
git -C "$INVALID_PEER_REPO" config user.name "Degen Dogs Invalid Peer"
git -C "$INVALID_PEER_REPO" config user.email "degen-dogs-invalid-peer@example.invalid"
python3 - "$INVALID_PEER_REPO" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

repo = Path(sys.argv[1])
payload = json.loads((repo / "generated/refresh_status.json").read_text(encoding="utf-8"))
payload["last_successful_refresh_time_utc"] = "2026-08-18T20:06:00Z"
payload["onchain_verification_status"] = "not_verified"
text = json.dumps(payload, sort_keys=True) + "\n"
for relative in ("generated/refresh_status.json", "public/generated/refresh_status.json"):
    (repo / relative).write_text(text, encoding="utf-8")
PY
git -C "$INVALID_PEER_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$INVALID_PEER_REPO" commit -qm "[cron] invalid claimed peer snapshot" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: invalid-peer-winner"
INVALID_PEER_COMMIT="$(git -C "$INVALID_PEER_REPO" rev-parse HEAD)"
git -C "$INVALID_PEER_REPO" push -q origin main

rm -f -- "$SUCCESS_MARKER"
INVALID_PEER_RESULT="$TEST_ROOT/invalid-peer-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$INVALID_PEER_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/invalid-peer-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/invalid-peer-config-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$INVALID_PEER_LOCKS/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

INVALID_REPAIR_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
if [[ "$INVALID_REPAIR_COMMIT" == "$INVALID_PEER_COMMIT" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$INVALID_REPAIR_COMMIT" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD^)" != "$INVALID_PEER_COMMIT" ]] || \
  [[ -e "$INVALID_PEER_LOCKS/publisher-recovery.json" ]] || \
  [[ "$(sed -n '1p' "$INVALID_PEER_RESULT")" != "success_pushed" ]] || \
  [[ ! -e "$SUCCESS_MARKER" ]]; then
  echo "invalid peer snapshot was acknowledged instead of canonically rejected and regenerated" >&2
  echo "local_interrupted_commit=${INVALID_LOCAL_COMMIT}" >&2
  exit 1
fi
if ! grep -q "regenerating because peer coverage was not proven" "$TEST_ROOT/invalid-peer-logs/refresh.log"; then
  echo "invalid peer snapshot did not take the regeneration path" >&2
  exit 1
fi

# Equal block height alone is not peer coverage. Build a fully self-consistent
# peer snapshot at the same block whose auction feed changes its immutable live
# bundle digest; recovery must align and regenerate rather than return a no-op.
DIGEST_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-18T20:06:30Z"
git -C "$SUCCESS_REPO" add generated public/generated
git -C "$SUCCESS_REPO" commit -qm "[cron] interrupted local before digest-divergent peer" \
  -m "Refresh-Runner-ID: fixture-local" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: digest-peer-local"
DIGEST_LOCAL_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
DIGEST_LOCKS="$TEST_ROOT/digest-peer-locks"
write_fixture_recovery_journal \
  "$DIGEST_LOCKS/publisher-recovery.json" \
  "$SUCCESS_REPO" \
  "$DIGEST_BASELINE" \
  "digest-peer-local"

DIGEST_PEER_REPO="$TEST_ROOT/digest-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$DIGEST_PEER_REPO"
git -C "$DIGEST_PEER_REPO" config user.name "Degen Dogs Digest Peer"
git -C "$DIGEST_PEER_REPO" config user.email "degen-dogs-digest-peer@example.invalid"
printf '%s\n' 'id' '77' >"$DIGEST_PEER_REPO/generated/auction_feed.csv"
printf '%s\n' '[{"id":77}]' >"$DIGEST_PEER_REPO/generated/auction_feed.json"
cp "$DIGEST_PEER_REPO/generated/auction_feed.csv" "$DIGEST_PEER_REPO/public/generated/auction_feed.csv"
cp "$DIGEST_PEER_REPO/generated/auction_feed.json" "$DIGEST_PEER_REPO/public/generated/auction_feed.json"
touch_valid_refresh_status "$DIGEST_PEER_REPO" "2026-08-18T20:06:45Z"
git -C "$DIGEST_PEER_REPO" add generated public/generated
git -C "$DIGEST_PEER_REPO" commit -qm "[cron] self-consistent equal-block digest-divergent peer" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: digest-peer-winner"
DIGEST_PEER_COMMIT="$(git -C "$DIGEST_PEER_REPO" rev-parse HEAD)"
python3 - "$SUCCESS_REPO" "$DIGEST_LOCAL_COMMIT" "$DIGEST_PEER_REPO" "$DIGEST_PEER_COMMIT" <<'PY'
from __future__ import annotations

import json
import subprocess
import sys


def status(repo: str, commit: str) -> dict[str, object]:
    return json.loads(
        subprocess.check_output(
            ["git", "-C", repo, "show", f"{commit}:generated/refresh_status.json"],
            text=True,
        )
    )


local = status(sys.argv[1], sys.argv[2])
peer = status(sys.argv[3], sys.argv[4])
if local["latest_generated_block"] != peer["latest_generated_block"]:
    raise SystemExit("digest-divergent fixture does not use an equal block")
if local["live_snapshot_bundle_sha256"] == peer["live_snapshot_bundle_sha256"]:
    raise SystemExit("digest-divergent fixture did not change the live bundle digest")
PY
git -C "$DIGEST_PEER_REPO" push -q origin main

rm -f -- "$SUCCESS_MARKER"
DIGEST_RESULT="$TEST_ROOT/digest-peer-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$DIGEST_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/digest-peer-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/digest-peer-config-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$DIGEST_LOCKS/refresh.lock" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

DIGEST_REPAIR_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
if [[ "$DIGEST_REPAIR_COMMIT" == "$DIGEST_PEER_COMMIT" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$DIGEST_REPAIR_COMMIT" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD^)" != "$DIGEST_PEER_COMMIT" ]] || \
  [[ "$(sed -n '1p' "$DIGEST_RESULT")" != "success_pushed" ]] || \
  [[ ! -e "$SUCCESS_MARKER" ]] || \
  [[ -e "$DIGEST_LOCKS/publisher-recovery.json" ]]; then
  echo "equal-block digest-divergent peer was acknowledged instead of regenerated" >&2
  echo "local_interrupted_commit=${DIGEST_LOCAL_COMMIT}" >&2
  exit 1
fi
if ! grep -q "regenerating because peer coverage was not proven" "$TEST_ROOT/digest-peer-logs/refresh.log"; then
  echo "equal-block digest-divergent peer did not take the regeneration path" >&2
  exit 1
fi

# An interrupted archive/full-surface job may contain valid offchain deltas
# that a newer bounded current snapshot does not cover. Its journaled scope
# must union with a caller full request, force archive/full regeneration,
# and publish a child instead of reporting peer supersession.
ARCHIVE_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
mkdir -p "$SUCCESS_REPO/archive/mission3/data/generated"
printf '%s\n' '{"state":"interrupted-local"}' \
  >"$SUCCESS_REPO/archive/mission3/data/generated/archive_scope_fixture.json"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-18T20:07:00Z"
git -C "$SUCCESS_REPO" add archive/mission3/data/generated/archive_scope_fixture.json \
  generated/refresh_status.json public/generated/refresh_status.json
git -C "$SUCCESS_REPO" commit -qm "[cron] interrupted archive publisher" \
  -m "Refresh-Runner-ID: fixture-archive" \
  -m "Refresh-Run-Scope: archive" \
  -m "Refresh-Run-ID: archive-peer-local"
ARCHIVE_LOCAL_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
ARCHIVE_LOCKS="$TEST_ROOT/archive-peer-locks"
write_fixture_recovery_journal \
  "$ARCHIVE_LOCKS/publisher-recovery.json" \
  "$SUCCESS_REPO" \
  "$ARCHIVE_BASELINE" \
  "archive-peer-local" \
  "archive" \
  "fixture-archive"

ARCHIVE_PEER_REPO="$TEST_ROOT/archive-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$ARCHIVE_PEER_REPO"
git -C "$ARCHIVE_PEER_REPO" config user.name "Degen Dogs Current Peer"
git -C "$ARCHIVE_PEER_REPO" config user.email "degen-dogs-current-peer@example.invalid"
touch_valid_refresh_status "$ARCHIVE_PEER_REPO" "2026-08-18T20:08:00Z"
git -C "$ARCHIVE_PEER_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$ARCHIVE_PEER_REPO" commit -qm "[cron] bounded current peer" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: archive-peer-winner"
ARCHIVE_PEER_COMMIT="$(git -C "$ARCHIVE_PEER_REPO" rev-parse HEAD)"
git -C "$ARCHIVE_PEER_REPO" push -q origin main

rm -f -- "$SUCCESS_MARKER"
ARCHIVE_RESULT="$TEST_ROOT/archive-peer-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$ARCHIVE_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/archive-peer-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/archive-peer-config-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$ARCHIVE_LOCKS/refresh.lock" \
DEGEN_DOGS_FULL_REFRESH=1 \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

ARCHIVE_REPAIR_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
if [[ "$ARCHIVE_REPAIR_COMMIT" == "$ARCHIVE_PEER_COMMIT" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$ARCHIVE_REPAIR_COMMIT" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD^)" != "$ARCHIVE_PEER_COMMIT" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" show -s --format=%B HEAD)" != *"Refresh-Run-Scope: archive_full"* ]] || \
  [[ "$(<"$SUCCESS_REPO/archive/mission3/data/generated/archive_scope_fixture.json")" != '{"state":"regenerated"}' ]] || \
  [[ "$(sed -n '1p' "$ARCHIVE_RESULT")" != "success_pushed" ]] || \
  [[ -e "$ARCHIVE_LOCKS/publisher-recovery.json" ]]; then
  echo "archive/full interrupted delta was silently superseded or scope-downgraded" >&2
  echo "local_interrupted_commit=${ARCHIVE_LOCAL_COMMIT}" >&2
  exit 1
fi
if ! grep -q "running Mission 3 archive incremental index" "$TEST_ROOT/archive-peer-logs/refresh.log"; then
  echo "archive scope was not restored from the interrupted publisher journal" >&2
  exit 1
fi

# Scope union is symmetric: a full interrupted run recovered by an archive
# caller must also publish archive_full, while validating the old full commit
# against the journal's original runner identity and scope.
FULL_JOURNAL_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-18T20:09:00Z"
git -C "$SUCCESS_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$SUCCESS_REPO" commit -qm "[cron] interrupted full publisher" \
  -m "Refresh-Runner-ID: fixture-full" \
  -m "Refresh-Run-Scope: full" \
  -m "Refresh-Run-ID: full-archive-union-local"
FULL_JOURNAL_LOCAL_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
FULL_JOURNAL_LOCKS="$TEST_ROOT/full-archive-union-locks"
write_fixture_recovery_journal \
  "$FULL_JOURNAL_LOCKS/publisher-recovery.json" \
  "$SUCCESS_REPO" \
  "$FULL_JOURNAL_BASELINE" \
  "full-archive-union-local" \
  "full" \
  "fixture-full"

FULL_JOURNAL_PEER_REPO="$TEST_ROOT/full-archive-union-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$FULL_JOURNAL_PEER_REPO"
git -C "$FULL_JOURNAL_PEER_REPO" config user.name "Degen Dogs Current Peer"
git -C "$FULL_JOURNAL_PEER_REPO" config user.email "degen-dogs-current-peer@example.invalid"
touch_valid_refresh_status "$FULL_JOURNAL_PEER_REPO" "2026-08-18T20:10:00Z"
git -C "$FULL_JOURNAL_PEER_REPO" add generated/refresh_status.json public/generated/refresh_status.json
git -C "$FULL_JOURNAL_PEER_REPO" commit -qm "[cron] bounded current peer" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: full-archive-union-peer"
FULL_JOURNAL_PEER_COMMIT="$(git -C "$FULL_JOURNAL_PEER_REPO" rev-parse HEAD)"
git -C "$FULL_JOURNAL_PEER_REPO" push -q origin main

FULL_JOURNAL_RESULT="$TEST_ROOT/full-archive-union-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$FULL_JOURNAL_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/full-archive-union-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/full-archive-union-config-locks" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$FULL_JOURNAL_LOCKS/refresh.lock" \
DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1 \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

FULL_JOURNAL_REPAIR_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
if [[ "$FULL_JOURNAL_REPAIR_COMMIT" == "$FULL_JOURNAL_PEER_COMMIT" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$FULL_JOURNAL_REPAIR_COMMIT" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD^)" != "$FULL_JOURNAL_PEER_COMMIT" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" show -s --format=%B HEAD)" != *"Refresh-Run-Scope: archive_full"* ]] || \
  [[ "$(sed -n '1p' "$FULL_JOURNAL_RESULT")" != "success_pushed" ]] || \
  [[ -e "$FULL_JOURNAL_LOCKS/publisher-recovery.json" ]]; then
  echo "full/archive recovery did not preserve the unioned archive_full scope" >&2
  echo "local_interrupted_commit=${FULL_JOURNAL_LOCAL_COMMIT}" >&2
  exit 1
fi

# Exit 75 promotes a bounded run to the full builder. The journal and eventual
# commit must be promoted too, otherwise a later peer collision could silently
# classify full-surface output as a bounded current no-op.
printf '%s\n' 'process.exit(75);' >"$SUCCESS_REPO/scripts/fallback_generation.js"
printf '%s\n' \
  '{"scripts":{' \
  '"refresh:current":"node scripts/fallback_generation.js",' \
  '"data":"node scripts/success_generation.js && python3 scripts/build_live_snapshot_bundle.py",' \
  '"validate:dashboard":"node scripts/validate_marker.js",' \
  '"archive:prices:validate":"node -e \"\"",' \
  '"check:historical-dogs":"node -e \"\"",' \
  '"build":"node -e \"\""' \
  '}}' >"$SUCCESS_REPO/package.json"
printf '%s\n' 'id' '1' >"$SUCCESS_REPO/generated/auction_feed.csv"
printf '%s\n' '[{"id":1}]' >"$SUCCESS_REPO/generated/auction_feed.json"
cp "$SUCCESS_REPO/generated/auction_feed.csv" "$SUCCESS_REPO/public/generated/auction_feed.csv"
cp "$SUCCESS_REPO/generated/auction_feed.json" "$SUCCESS_REPO/public/generated/auction_feed.json"
git -C "$SUCCESS_REPO" add package.json scripts/fallback_generation.js \
  generated/auction_feed.csv generated/auction_feed.json \
  public/generated/auction_feed.csv public/generated/auction_feed.json
git -C "$SUCCESS_REPO" commit -qm "fixture: prepare successful exit-75 fallback"
git -C "$SUCCESS_REPO" push -q
FALLBACK_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"

rm -f -- "$SUCCESS_MARKER"
FALLBACK_RESULT="$TEST_ROOT/fallback-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$FALLBACK_RESULT" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/fallback-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/fallback-locks" \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

FALLBACK_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
if [[ "$FALLBACK_COMMIT" == "$FALLBACK_BASELINE" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" show -s --format=%B HEAD)" != *"Refresh-Run-Scope: full"* ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$FALLBACK_COMMIT" ]] || \
  [[ "$(sed -n '1p' "$FALLBACK_RESULT")" != "success_pushed" ]] || \
  [[ -e "$TEST_ROOT/fallback-locks/publisher-recovery.json" ]]; then
  echo "exit-75 fallback did not promote and publish full-refresh scope" >&2
  exit 1
fi
if ! grep -q "falling back to npm run data" "$TEST_ROOT/fallback-logs/refresh.log"; then
  echo "exit-75 fallback path was not exercised" >&2
  exit 1
fi

# Exercise the narrower active-active race: both publishers observe the same
# baseline, then the peer advances main from inside our pre-push hook after the
# lease was prepared. The rejected CAS must fetch/classify the peer winner,
# fast-forward to it, and exit zero rather than preserving a wedged journal.
printf '%s\n' \
  '#!/usr/bin/env python3' \
  'from __future__ import annotations' \
  'import json' \
  'from build_live_snapshot_bundle import build_live_snapshot_bundle' \
  'from pathlib import Path' \
  'status = json.loads(Path("generated/refresh_status.json").read_text(encoding="utf-8"))' \
  'status["last_successful_refresh_time_utc"] = "2026-08-18T20:03:00Z"' \
  'for path in (' \
  '    Path("generated/refresh_status.json"),' \
  '    Path("public/generated/refresh_status.json"),' \
  '):' \
  '    path.write_text(json.dumps(status, sort_keys=True) + "\n", encoding="utf-8")' \
  'for path in (Path("generated/auction_feed.csv"), Path("public/generated/auction_feed.csv")):' \
  '    path.write_text("id\n3\n", encoding="utf-8")' \
  'for path in (Path("generated/auction_feed.json"), Path("public/generated/auction_feed.json")):' \
  '    path.write_text("[{\"id\":3}]\n", encoding="utf-8")' \
  'build_live_snapshot_bundle()' \
  >"$SUCCESS_REPO/scripts/race_generation.py"
chmod +x "$SUCCESS_REPO/scripts/race_generation.py"
printf '%s\n' \
  '{"scripts":{' \
  '"refresh:current":"python3 scripts/race_generation.py",' \
  '"validate:dashboard":"node scripts/validate_marker.js",' \
  '"archive:prices:validate":"node -e \"\"",' \
  '"check:historical-dogs":"node -e \"\"",' \
  '"build":"node -e \"\""' \
  '}}' >"$SUCCESS_REPO/package.json"
git -C "$SUCCESS_REPO" add package.json scripts/race_generation.py
git -C "$SUCCESS_REPO" commit -qm "fixture: prepare CAS collision generator"
git -C "$SUCCESS_REPO" push -q
CAS_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"

CAS_PEER_REPO="$TEST_ROOT/cas-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$CAS_PEER_REPO"
git -C "$CAS_PEER_REPO" config user.name "Degen Dogs CAS Peer"
git -C "$CAS_PEER_REPO" config user.email "degen-dogs-cas-peer@example.invalid"
printf '%s\n' 'id' '3' >"$CAS_PEER_REPO/generated/auction_feed.csv"
printf '%s\n' '[{"id":3}]' >"$CAS_PEER_REPO/generated/auction_feed.json"
cp "$CAS_PEER_REPO/generated/auction_feed.csv" "$CAS_PEER_REPO/public/generated/auction_feed.csv"
cp "$CAS_PEER_REPO/generated/auction_feed.json" "$CAS_PEER_REPO/public/generated/auction_feed.json"
touch_valid_refresh_status "$CAS_PEER_REPO" "2026-08-18T20:04:00Z"
git -C "$CAS_PEER_REPO" add generated public/generated
git -C "$CAS_PEER_REPO" commit -qm "[cron] CAS peer publisher winner" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: cas-peer-winner"
CAS_PEER_COMMIT="$(git -C "$CAS_PEER_REPO" rev-parse HEAD)"
git -C "$CAS_PEER_REPO" push -q origin "${CAS_PEER_COMMIT}:refs/heads/cas-peer-fixture"

CAS_HOOK_MARKER="$TEST_ROOT/cas-hook-armed"
: >"$CAS_HOOK_MARKER"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [[ -n "${FIXTURE_CAS_HOOK_MARKER:-}" && -e "$FIXTURE_CAS_HOOK_MARKER" ]]; then' \
  '  rm -- "$FIXTURE_CAS_HOOK_MARKER"' \
  '  git --git-dir="$FIXTURE_CAS_REMOTE" update-ref refs/heads/main "$FIXTURE_CAS_PEER_COMMIT" "$FIXTURE_CAS_BASELINE"' \
  'fi' \
  >"$SUCCESS_REPO/.git/hooks/pre-push"
chmod +x "$SUCCESS_REPO/.git/hooks/pre-push"

rm -f -- "$SUCCESS_MARKER"
CAS_RESULT_MARKER="$TEST_ROOT/cas-result"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$CAS_RESULT_MARKER" \
FIXTURE_CAS_HOOK_MARKER="$CAS_HOOK_MARKER" \
FIXTURE_CAS_REMOTE="$REJECT_REMOTE" \
FIXTURE_CAS_PEER_COMMIT="$CAS_PEER_COMMIT" \
FIXTURE_CAS_BASELINE="$CAS_BASELINE" \
DEGEN_DOGS_RUNNER_ID="fixture-local" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/cas-collision-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/cas-collision-locks" \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

if [[ -e "$CAS_HOOK_MARKER" ]] || \
  [[ ! -e "$SUCCESS_MARKER" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$CAS_PEER_COMMIT" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$CAS_PEER_COMMIT" ]] || \
  [[ -e "$TEST_ROOT/cas-collision-locks/publisher-recovery.json" ]] || \
  [[ ! -e "$CAS_RESULT_MARKER" ]] || \
  [[ "$(sed -n '1p' "$CAS_RESULT_MARKER")" != "success_superseded_by_peer" ]] || \
  [[ "$(sed -n '2p' "$CAS_RESULT_MARKER")" != "$CAS_PEER_COMMIT" ]]; then
  echo "compare-and-swap collision was not classified as a clean peer supersession" >&2
  exit 1
fi
if ! grep -q "git push compare-and-swap attempt=1/1" "$TEST_ROOT/cas-collision-logs/refresh.log" || \
  ! grep -q "compare-and-swap push completed by supersession" "$TEST_ROOT/cas-collision-logs/refresh.log"; then
  echo "compare-and-swap collision classification was not logged" >&2
  exit 1
fi
rm -- "$SUCCESS_REPO/.git/hooks/pre-push"

# Restore the ordinary generator for the remaining rejection/live-verification
# fixtures and publish that fixture-only code transition normally.
printf '%s\n' \
  '{"scripts":{' \
  '"refresh:current":"node scripts/success_generation.js",' \
  '"archive:mission3:index":"node scripts/archive_generation.js",' \
  '"archive:mission3:health":"node -e \"\"",' \
  '"validate:dashboard":"node scripts/validate_marker.js",' \
  '"archive:prices:validate":"node -e \"\"",' \
  '"check:historical-dogs":"node -e \"\"",' \
  '"build":"node -e \"\""' \
  '}}' >"$SUCCESS_REPO/package.json"
git -C "$SUCCESS_REPO" rm -q scripts/race_generation.py
printf '%s\n' 'id' '2' >"$SUCCESS_REPO/generated/auction_feed.csv"
printf '%s\n' '[{"id":2}]' >"$SUCCESS_REPO/generated/auction_feed.json"
cp "$SUCCESS_REPO/generated/auction_feed.csv" "$SUCCESS_REPO/public/generated/auction_feed.csv"
cp "$SUCCESS_REPO/generated/auction_feed.json" "$SUCCESS_REPO/public/generated/auction_feed.json"
git -C "$SUCCESS_REPO" add package.json generated/auction_feed.csv generated/auction_feed.json \
  public/generated/auction_feed.csv public/generated/auction_feed.json
git -C "$SUCCESS_REPO" commit -qm "fixture: restore ordinary generator"
git -C "$SUCCESS_REPO" push -q

printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync('generated/auction_feed.csv', 'id\\n3\\n');" \
  "fs.writeFileSync('generated/auction_feed.json', '[{\\\"id\\\":3}]\\n');" \
  "fs.writeFileSync('public/generated/auction_feed.csv', 'id\\n3\\n');" \
  "fs.writeFileSync('public/generated/auction_feed.json', '[{\\\"id\\\":3}]\\n');" >"$SUCCESS_REPO/scripts/success_generation.js"
git -C "$SUCCESS_REPO" add scripts/success_generation.js
git -C "$SUCCESS_REPO" commit -qm "fixture: generate a new snapshot"
git -C "$SUCCESS_REPO" push -q
PUSH_FAILURE_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
printf '%s\n' '#!/usr/bin/env bash' 'exit 1' >"$REJECT_REMOTE/hooks/pre-receive"
chmod +x "$REJECT_REMOTE/hooks/pre-receive"

set +e
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/push-failure-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/push-failure-locks" \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" ]]; then
  echo "expected fixture remote to reject the runner push" >&2
  exit 1
fi
if [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$PUSH_FAILURE_BASELINE" ]]; then
  echo "failed runner push left a local-ahead commit" >&2
  exit 1
fi
if [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$PUSH_FAILURE_BASELINE" ]]; then
  echo "rejected runner commit unexpectedly reached the remote" >&2
  exit 1
fi
if [[ -n "$(git -C "$SUCCESS_REPO" status --porcelain --untracked-files=all -- README.md index.html generated public)" ]]; then
  echo "push-failure rollback left publish artifacts dirty" >&2
  git -C "$SUCCESS_REPO" status --short --untracked-files=all -- README.md index.html generated public >&2
  exit 1
fi
if ! grep -q "rewound unpushed runner commit" "$TEST_ROOT/push-failure-logs/refresh.log"; then
  echo "push-failure commit rewind was not logged" >&2
  exit 1
fi

# A successful push followed only by a bounded Pages timeout is a successful
# publication awaiting deployment, not a data-generation failure. It must not
# trigger the health watchdog's rapid regenerate-and-repush loop.
rm -- "$REJECT_REMOTE/hooks/pre-receive"
HOME="$TEST_ROOT/home" \
FIXTURE_LIVE_TIMEOUT=1 \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/live-timeout-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/live-timeout-locks" \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
if [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" ]]; then
  echo "live-timeout fixture did not preserve the successfully pushed commit" >&2
  exit 1
fi
if ! grep -q "pushed snapshot is awaiting GitHub Pages" "$TEST_ROOT/live-timeout-logs/refresh.log" || \
  ! grep -q "finished status=0" "$TEST_ROOT/live-timeout-logs/refresh.log"; then
  echo "post-push live timeout was not recorded as a successful deferred deployment" >&2
  exit 1
fi

# Queue mode must stop after exact immutable-commit raw proof and a durable
# pushed handoff. The inherited refresh lock remains held by the caller; Bash
# must neither poll Pages nor acknowledge latest.json nor clear the fixed
# authenticated journal before the future drainer finalizes it.
printf '%s\n' \
  "const fs = require('fs');" \
  "const status = JSON.parse(fs.readFileSync('generated/refresh_status.json', 'utf8'));" \
  "status.last_successful_refresh_time_utc = '2026-08-30T20:11:00Z';" \
  "const text = JSON.stringify(status) + '\\n';" \
  "fs.writeFileSync('generated/refresh_status.json', text);" \
  "fs.writeFileSync('public/generated/refresh_status.json', text);" >"$SUCCESS_REPO/scripts/success_generation.js"
git -C "$SUCCESS_REPO" add scripts/success_generation.js
git -C "$SUCCESS_REPO" commit -qm "fixture: generate deferred pushed snapshot"
git -C "$SUCCESS_REPO" push -q

DEFER_PUSH_LOCKS="$TEST_ROOT/deferred-push-locks"
DEFER_PUSH_REFRESH="$TEST_ROOT/deferred-push-refresh-lock/refresh.lock"
DEFER_PUSH_RESULT="$TEST_ROOT/deferred-push-result"
DEFER_PUSH_RAW="$TEST_ROOT/deferred-push-raw-proof"
DEFER_PUSH_PAGES="$TEST_ROOT/deferred-push-pages-proof"
mkdir -m 700 -p "$DEFER_PUSH_LOCKS" "$(dirname "$DEFER_PUSH_REFRESH")"
DEFER_PUSH_DIGEST="$(write_fixture_publication_latest "$DEFER_PUSH_LOCKS" 41)"
: >"$DEFER_PUSH_REFRESH"
chmod 600 "$DEFER_PUSH_REFRESH"
exec {DEFER_PUSH_FD}<>"$DEFER_PUSH_REFRESH"
flock -n "$DEFER_PUSH_FD"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$DEFER_PUSH_RESULT" \
FIXTURE_RAW_VERIFY_MARKER="$DEFER_PUSH_RAW" \
FIXTURE_PAGES_VERIFY_MARKER="$DEFER_PUSH_PAGES" \
DEGEN_DOGS_RUNNER_ID="windows-wsl" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/deferred-push-logs" \
DEGEN_DOGS_LOCK_DIR="$DEFER_PUSH_LOCKS" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$DEFER_PUSH_REFRESH" \
DEGEN_DOGS_LOCK_HELD=1 \
DEGEN_DOGS_LOCK_FD="$DEFER_PUSH_FD" \
DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1 \
DEGEN_DOGS_PUBLICATION_GENERATION=41 \
DEGEN_DOGS_PUBLICATION_DIGEST="$DEFER_PUSH_DIGEST" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
flock -u "$DEFER_PUSH_FD"
exec {DEFER_PUSH_FD}>&-

python3 - "$DEFER_PUSH_LOCKS" "$DEFER_PUSH_DIGEST" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
pending = json.loads((root / "publication/pending.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
assert journal["publication_generation"] == pending["generation"] == checkpoint["generation"] == 41
assert journal["queue_digest"] == pending["queue_digest"] == checkpoint["queue_digest"] == sys.argv[2]
assert journal["terminal_outcome"] == checkpoint["outcome"] == "pushed"
assert journal["handoff_phase"] == "raw_proven"
assert journal["remote_commit"] == pending["commit_sha"] == checkpoint["commit_sha"]
assert (root / "publication/latest.json").exists()
PY
if [[ "$(sed -n '1p' "$DEFER_PUSH_RESULT")" != "success_pushed" ]] || \
  [[ ! -e "$DEFER_PUSH_RAW" ]] || [[ -e "$DEFER_PUSH_PAGES" ]] || \
  [[ -e "$(dirname "$DEFER_PUSH_REFRESH")/publisher-recovery.json" ]]; then
  echo "deferred pushed outcome did not retain the fixed raw-proven handoff without polling Pages" >&2
  exit 1
fi

# Simulate a power loss after the remote accepted the commit but before raw
# proof became durable. Recovery must prove the exact immutable status+bundle
# again, rebuild pending/checkpoint, retain the journal, and return to the
# drainer instead of unlinking at the old remote-equals-local branch.
rm -- "$DEFER_PUSH_LOCKS/publication/pending.json" "$DEFER_PUSH_LOCKS/publication/pushed.json"
python3 - "$DEFER_PUSH_LOCKS/publisher-recovery.json" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["handoff_phase"] = "push_ready"
for key in (
    "raw_status_path", "raw_bundle_path", "expected_bundle_sha256",
    "expected_bundle_bytes", "expected_block_number", "expected_block_hash",
    "push_completed_at_utc", "retry_deadline_utc", "retry_count",
):
    value[key] = None
temporary = path.with_name(".publisher-recovery.fixture.tmp")
temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
os.chmod(path, 0o600)
PY
rm -f -- "$DEFER_PUSH_RAW" "$DEFER_PUSH_RESULT"
exec {DEFER_PUSH_FD}<>"$DEFER_PUSH_REFRESH"
flock -n "$DEFER_PUSH_FD"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$DEFER_PUSH_RESULT" \
FIXTURE_RAW_VERIFY_MARKER="$DEFER_PUSH_RAW" \
FIXTURE_PAGES_VERIFY_MARKER="$DEFER_PUSH_PAGES" \
DEGEN_DOGS_RUNNER_ID="windows-wsl" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/deferred-push-recovery-logs" \
DEGEN_DOGS_LOCK_DIR="$DEFER_PUSH_LOCKS" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$DEFER_PUSH_REFRESH" \
DEGEN_DOGS_LOCK_HELD=1 \
DEGEN_DOGS_LOCK_FD="$DEFER_PUSH_FD" \
DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1 \
DEGEN_DOGS_PUBLICATION_GENERATION=41 \
DEGEN_DOGS_PUBLICATION_DIGEST="$DEFER_PUSH_DIGEST" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
flock -u "$DEFER_PUSH_FD"
exec {DEFER_PUSH_FD}>&-
if [[ ! -e "$DEFER_PUSH_RAW" ]] || [[ -e "$DEFER_PUSH_PAGES" ]] || \
  [[ ! -e "$DEFER_PUSH_LOCKS/publication/pending.json" ]] || \
  [[ ! -e "$DEFER_PUSH_LOCKS/publication/pushed.json" ]] || \
  [[ ! -e "$DEFER_PUSH_LOCKS/publisher-recovery.json" ]] || \
  [[ "$(sed -n '1p' "$DEFER_PUSH_RESULT")" != "success_pushed" ]]; then
  echo "remote-equals-local deferred recovery did not rebuild and retain the landed handoff" >&2
  exit 1
fi
finalize_fixture_publication "$DEFER_PUSH_LOCKS" 41 "$DEFER_PUSH_DIGEST"
if [[ -e "$DEFER_PUSH_LOCKS/publication/latest.json" || -e "$DEFER_PUSH_LOCKS/publisher-recovery.json" ]]; then
  echo "exact deferred pushed finalization did not CAS the queue before journal unlink" >&2
  exit 1
fi

# No-diff is a terminal queued generation with no fabricated commit or push
# timestamp. It checkpoints durably and leaves queue/journal acknowledgement
# to the same finalizer boundary.
python3 - "$SUCCESS_REPO/package.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["scripts"]["refresh:current"] = "node -e \"\""
path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
PY
git -C "$SUCCESS_REPO" add package.json
git -C "$SUCCESS_REPO" commit -qm "fixture: prepare deferred no-diff generation"
git -C "$SUCCESS_REPO" push -q
DEFER_NO_DIFF_LOCKS="$TEST_ROOT/deferred-no-diff-locks"
DEFER_NO_DIFF_REFRESH="$DEFER_NO_DIFF_LOCKS/refresh.lock"
DEFER_NO_DIFF_RESULT="$TEST_ROOT/deferred-no-diff-result"
DEFER_NO_DIFF_PAGES="$TEST_ROOT/deferred-no-diff-pages-proof"
mkdir -m 700 -p "$DEFER_NO_DIFF_LOCKS"
DEFER_NO_DIFF_DIGEST="$(write_fixture_publication_latest "$DEFER_NO_DIFF_LOCKS" 42)"
: >"$DEFER_NO_DIFF_REFRESH"
chmod 600 "$DEFER_NO_DIFF_REFRESH"
exec {DEFER_NO_DIFF_FD}<>"$DEFER_NO_DIFF_REFRESH"
flock -n "$DEFER_NO_DIFF_FD"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$DEFER_NO_DIFF_RESULT" \
FIXTURE_PAGES_VERIFY_MARKER="$DEFER_NO_DIFF_PAGES" \
DEGEN_DOGS_RUNNER_ID="windows-wsl" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/deferred-no-diff-logs" \
DEGEN_DOGS_LOCK_DIR="$DEFER_NO_DIFF_LOCKS" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$DEFER_NO_DIFF_REFRESH" \
DEGEN_DOGS_LOCK_HELD=1 \
DEGEN_DOGS_LOCK_FD="$DEFER_NO_DIFF_FD" \
DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1 \
DEGEN_DOGS_PUBLICATION_GENERATION=42 \
DEGEN_DOGS_PUBLICATION_DIGEST="$DEFER_NO_DIFF_DIGEST" \
DEGEN_DOGS_SKIP_PULL=1 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
flock -u "$DEFER_NO_DIFF_FD"
exec {DEFER_NO_DIFF_FD}>&-
python3 - "$DEFER_NO_DIFF_LOCKS" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
assert journal["terminal_outcome"] == checkpoint["outcome"] == "no_diff"
assert journal["remote_commit"] is None and checkpoint["commit_sha"] is None
assert checkpoint["push_completed_at_utc"] is None
assert not (root / "publication/pending.json").exists()
assert (root / "publication/latest.json").exists()
PY
if [[ "$(sed -n '1p' "$DEFER_NO_DIFF_RESULT")" != "success_no_diff" ]] || \
  [[ -e "$DEFER_NO_DIFF_PAGES" ]]; then
  echo "deferred no-diff outcome was not checkpointed without Pages polling" >&2
  exit 1
fi
finalize_fixture_publication "$DEFER_NO_DIFF_LOCKS" 42 "$DEFER_NO_DIFF_DIGEST"

# A verified peer winner is another terminal queue outcome. Exercise the
# interrupted-local-child recovery path because it also covers the existing
# alignment journal rewrite: deferred metadata must survive that rewrite,
# checkpoint the peer SHA, and remain for exact drainer finalization.
DEFER_PEER_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-30T12:50:00Z"
git -C "$SUCCESS_REPO" add generated public/generated
git -C "$SUCCESS_REPO" commit -qm "[cron] deferred local interrupted child" \
  -m "Refresh-Runner-ID: windows-wsl" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: deferred-peer-local"
DEFER_PEER_LOCAL="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"

DEFER_PEER_REPO="$TEST_ROOT/deferred-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$DEFER_PEER_REPO"
git -C "$DEFER_PEER_REPO" config user.name "Degen Dogs Deferred Peer"
git -C "$DEFER_PEER_REPO" config user.email "degen-dogs-deferred-peer@example.invalid"
touch_valid_refresh_status "$DEFER_PEER_REPO" "2026-08-30T12:51:00Z"
git -C "$DEFER_PEER_REPO" add generated public/generated
git -C "$DEFER_PEER_REPO" commit -qm "[cron] deferred peer winner" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: deferred-peer-winner"
DEFER_PEER_COMMIT="$(git -C "$DEFER_PEER_REPO" rev-parse HEAD)"
git -C "$DEFER_PEER_REPO" push -q origin main

DEFER_PEER_LOCKS="$TEST_ROOT/deferred-peer-locks"
DEFER_PEER_REFRESH="$DEFER_PEER_LOCKS/refresh.lock"
DEFER_PEER_RESULT="$TEST_ROOT/deferred-peer-result"
DEFER_PEER_PAGES="$TEST_ROOT/deferred-peer-pages-proof"
mkdir -m 700 -p "$DEFER_PEER_LOCKS"
DEFER_PEER_DIGEST="$(write_fixture_publication_latest "$DEFER_PEER_LOCKS" 43)"
python3 - "$SOURCE_DIR/runner_publication_state.py" "$DEFER_PEER_LOCKS" "$SUCCESS_REPO" \
  "$DEFER_PEER_BASELINE" "$DEFER_PEER_DIGEST" <<'PY'
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("runner_publication_state_fixture", Path(sys.argv[1]))
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state
spec.loader.exec_module(state)
journal = {
    "schema_version": 1,
    "repo_realpath": str(Path(sys.argv[3]).resolve()),
    "branch": "main",
    "baseline_head": sys.argv[4],
    "run_id": "deferred-peer-local",
    "runner_id": "windows-wsl",
    "run_scope": "current",
    "created_at_utc": "2026-08-30T12:49:00Z",
    "publish_paths": [
        "README.md", "index.html", "generated", "public",
        "archive/mission3/data/generated", "archive/data/generated",
        "archive/data/identity/wallet_profiles.json", "archive/dogs",
        "archive/prices/data/generated", "archive/prices/data/raw",
    ],
    "alignment_runner_commit": None,
    "alignment_remote_head": None,
    "alignment_result": None,
    "publication_generation": 43,
    "queue_digest": sys.argv[5],
    "terminal_outcome": None,
    "handoff_phase": "generating",
    "remote_commit": None,
    "raw_status_path": None,
    "raw_bundle_path": None,
    "expected_bundle_sha256": None,
    "expected_bundle_bytes": None,
    "expected_block_number": None,
    "expected_block_hash": None,
    "push_completed_at_utc": None,
    "retry_deadline_utc": None,
    "retry_count": None,
}
state.create_deferred_recovery_journal(Path(sys.argv[2]), journal)
PY
: >"$DEFER_PEER_REFRESH"
chmod 600 "$DEFER_PEER_REFRESH"
exec {DEFER_PEER_FD}<>"$DEFER_PEER_REFRESH"
flock -n "$DEFER_PEER_FD"
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
FIXTURE_RESULT_MARKER="$DEFER_PEER_RESULT" \
FIXTURE_PAGES_VERIFY_MARKER="$DEFER_PEER_PAGES" \
DEGEN_DOGS_RUNNER_ID="windows-wsl" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/deferred-peer-logs" \
DEGEN_DOGS_LOCK_DIR="$DEFER_PEER_LOCKS" \
DEGEN_DOGS_REFRESH_LOCK_PATH="$DEFER_PEER_REFRESH" \
DEGEN_DOGS_LOCK_HELD=1 \
DEGEN_DOGS_LOCK_FD="$DEFER_PEER_FD" \
DEGEN_DOGS_DEFER_PAGES_VERIFICATION=1 \
DEGEN_DOGS_PUBLICATION_GENERATION=43 \
DEGEN_DOGS_PUBLICATION_DIGEST="$DEFER_PEER_DIGEST" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_GIT_RETRY_ATTEMPTS=1 \
DEGEN_DOGS_GIT_RETRY_BASE_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_MAX_SECONDS=0 \
DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS=0 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
flock -u "$DEFER_PEER_FD"
exec {DEFER_PEER_FD}>&-
python3 - "$DEFER_PEER_LOCKS" "$DEFER_PEER_COMMIT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
assert journal["terminal_outcome"] == checkpoint["outcome"] == "peer_superseded"
assert journal["remote_commit"] == checkpoint["commit_sha"] == sys.argv[2]
assert checkpoint["push_completed_at_utc"] is None
assert (root / "publication/latest.json").exists()
assert not (root / "publication/pending.json").exists()
PY
if [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$DEFER_PEER_COMMIT" ]] || \
  [[ "$(sed -n '1p' "$DEFER_PEER_RESULT")" != "success_superseded_by_peer" ]] || \
  [[ -e "$DEFER_PEER_PAGES" ]]; then
  echo "deferred peer supersession did not checkpoint and retain the exact queue handoff" >&2
  exit 1
fi
finalize_fixture_publication "$DEFER_PEER_LOCKS" 43 "$DEFER_PEER_DIGEST"

# A landed deferred push is not a successful publication until exact raw proof
# and both durable handoff records exist. Force the raw boundary to fail and
# assert telemetry remains non-success while the commit/journal stay recoverable.
python3 - "$SUCCESS_REPO/package.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["scripts"]["refresh:current"] = "node scripts/success_generation.js && python3 scripts/build_live_snapshot_bundle.py"
path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
PY
git -C "$SUCCESS_REPO" add package.json
git -C "$SUCCESS_REPO" commit -qm "fixture: prepare deferred raw-proof failure"
git -C "$SUCCESS_REPO" push -q
DEFER_RAW_FAIL_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
DEFER_RAW_FAIL_LOCKS="$TEST_ROOT/deferred-raw-fail-locks"
DEFER_RAW_FAIL_RESULT="$TEST_ROOT/deferred-raw-fail-result"
DEFER_RAW_FAIL_RAW="$TEST_ROOT/deferred-raw-fail-raw"
DEFER_RAW_FAIL_PAGES="$TEST_ROOT/deferred-raw-fail-pages"
DEFER_RAW_FAIL_DIGEST="$(write_fixture_publication_latest "$DEFER_RAW_FAIL_LOCKS" 50)"
if FIXTURE_RAW_VERIFY_FAIL=1 run_deferred_fixture \
  "$DEFER_RAW_FAIL_LOCKS" 50 "$DEFER_RAW_FAIL_DIGEST" \
  "$TEST_ROOT/deferred-raw-fail-logs" "$DEFER_RAW_FAIL_RESULT" \
  "$DEFER_RAW_FAIL_RAW" "$DEFER_RAW_FAIL_PAGES"; then
  echo "forced deferred raw-proof failure returned success" >&2
  exit 1
fi
DEFER_RAW_FAIL_LANDED="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
python3 - "$DEFER_RAW_FAIL_LOCKS" "$DEFER_RAW_FAIL_LANDED" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
assert journal["handoff_phase"] == "push_ready"
assert journal["remote_commit"] == sys.argv[2]
assert not (root / "publication/pending.json").exists()
assert not (root / "publication/pushed.json").exists()
assert (root / "publication/latest.json").exists()
PY
if [[ "$DEFER_RAW_FAIL_LANDED" == "$DEFER_RAW_FAIL_BASELINE" ]] || \
  [[ "$DEFER_RAW_FAIL_LANDED" != "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" ]] || \
  [[ "$(sed -n '1p' "$DEFER_RAW_FAIL_RESULT")" == "success_pushed" ]] || \
  [[ -e "$DEFER_RAW_FAIL_RAW" || -e "$DEFER_RAW_FAIL_PAGES" ]]; then
  echo "failed deferred raw proof reported success, rewound the landed commit, or lost recovery state" >&2
  exit 1
fi

# A newer queue observation may arrive while generation N is still recovering.
# The child must authenticate N from its fixed journal, recover N first, and
# leave N+1 in latest.json for the drainer's next iteration.
NPLUS_GENERATING_LOCKS="$TEST_ROOT/nplus-generating-locks"
NPLUS_GENERATING_RESULT="$TEST_ROOT/nplus-generating-result"
NPLUS_GENERATING_DIGEST="$(write_fixture_publication_latest "$NPLUS_GENERATING_LOCKS" 60)"
write_fixture_deferred_journal \
  "$NPLUS_GENERATING_LOCKS" "$SUCCESS_REPO" "$DEFER_RAW_FAIL_LANDED" \
  60 "$NPLUS_GENERATING_DIGEST" "nplus-generating" generating
NPLUS_GENERATING_NEW_DIGEST="$(write_fixture_publication_latest "$NPLUS_GENERATING_LOCKS" 61)"
run_deferred_fixture \
  "$NPLUS_GENERATING_LOCKS" 60 "$NPLUS_GENERATING_DIGEST" \
  "$TEST_ROOT/nplus-generating-logs" "$NPLUS_GENERATING_RESULT"
python3 - "$NPLUS_GENERATING_LOCKS" "$NPLUS_GENERATING_DIGEST" "$NPLUS_GENERATING_NEW_DIGEST" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
latest = json.loads((root / "publication/latest.json").read_text(encoding="utf-8"))
assert journal["publication_generation"] == checkpoint["generation"] == 60
assert journal["queue_digest"] == checkpoint["queue_digest"] == sys.argv[2]
assert checkpoint["outcome"] == "no_diff"
assert latest["generation"] == 61
PY
if [[ "$(sed -n '1p' "$NPLUS_GENERATING_RESULT")" != "success_no_diff" ]]; then
  echo "newer latest prevented authenticated generating-phase recovery" >&2
  exit 1
fi
finalize_fixture_publication "$NPLUS_GENERATING_LOCKS" 60 "$NPLUS_GENERATING_DIGEST"

NPLUS_PUSH_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-30T13:10:00Z"
git -C "$SUCCESS_REPO" add generated public/generated
git -C "$SUCCESS_REPO" commit -qm "[cron] N+1 recovery landed commit" \
  -m "Refresh-Runner-ID: windows-wsl" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: nplus-landed"
NPLUS_PUSH_COMMIT="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
git -C "$SUCCESS_REPO" push -q

for NPLUS_PHASE in push_ready raw_proven; do
  if [[ "$NPLUS_PHASE" == "push_ready" ]]; then
    NPLUS_GENERATION=62
    NPLUS_NEW_GENERATION=63
  else
    NPLUS_GENERATION=64
    NPLUS_NEW_GENERATION=65
  fi
  NPLUS_LOCKS="$TEST_ROOT/nplus-${NPLUS_PHASE}-locks"
  NPLUS_RESULT="$TEST_ROOT/nplus-${NPLUS_PHASE}-result"
  NPLUS_RAW="$TEST_ROOT/nplus-${NPLUS_PHASE}-raw"
  NPLUS_PAGES="$TEST_ROOT/nplus-${NPLUS_PHASE}-pages"
  NPLUS_DIGEST="$(write_fixture_publication_latest "$NPLUS_LOCKS" "$NPLUS_GENERATION")"
  write_fixture_deferred_journal \
    "$NPLUS_LOCKS" "$SUCCESS_REPO" "$NPLUS_PUSH_BASELINE" \
    "$NPLUS_GENERATION" "$NPLUS_DIGEST" "nplus-landed" \
    "$NPLUS_PHASE" "$NPLUS_PUSH_COMMIT"
  if [[ "$NPLUS_PHASE" == "raw_proven" ]]; then
    rm -- "$NPLUS_LOCKS/publication/pushed.json"
  fi
  write_fixture_publication_latest "$NPLUS_LOCKS" "$NPLUS_NEW_GENERATION" >/dev/null
  run_deferred_fixture \
    "$NPLUS_LOCKS" "$NPLUS_GENERATION" "$NPLUS_DIGEST" \
    "$TEST_ROOT/nplus-${NPLUS_PHASE}-logs" "$NPLUS_RESULT" "$NPLUS_RAW" "$NPLUS_PAGES"
  python3 - "$NPLUS_LOCKS" "$NPLUS_GENERATION" "$NPLUS_NEW_GENERATION" "$NPLUS_PUSH_COMMIT" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
generation = int(sys.argv[2])
new_generation = int(sys.argv[3])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
pending = json.loads((root / "publication/pending.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
latest = json.loads((root / "publication/latest.json").read_text(encoding="utf-8"))
assert journal["publication_generation"] == pending["generation"] == checkpoint["generation"] == generation
assert journal["remote_commit"] == pending["commit_sha"] == checkpoint["commit_sha"] == sys.argv[4]
assert journal["handoff_phase"] == "raw_proven"
assert latest["generation"] == new_generation
PY
  if [[ "$(sed -n '1p' "$NPLUS_RESULT")" != "success_pushed" ]] || \
    [[ ! -e "$NPLUS_RAW" || -e "$NPLUS_PAGES" ]]; then
    echo "newer latest prevented ${NPLUS_PHASE} landed-handoff recovery" >&2
    exit 1
  fi
  finalize_fixture_publication "$NPLUS_LOCKS" "$NPLUS_GENERATION" "$NPLUS_DIGEST"
done

# Missing latest.json is evidence of Task 4 queue CAS only after the journal
# has durable handoff state. Generating and push-ready journals must fail
# before repository or journal mutation; raw-proven and terminal remain valid.
MISSING_GENERATING_LOCKS="$TEST_ROOT/missing-latest-generating-locks"
MISSING_GENERATING_DIGEST="$(write_fixture_publication_latest "$MISSING_GENERATING_LOCKS" 72)"
MISSING_GENERATING_HEAD="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
write_fixture_deferred_journal \
  "$MISSING_GENERATING_LOCKS" "$SUCCESS_REPO" "$MISSING_GENERATING_HEAD" \
  72 "$MISSING_GENERATING_DIGEST" "missing-latest-generating" generating
rm -- "$MISSING_GENERATING_LOCKS/publication/latest.json"
if run_deferred_fixture \
  "$MISSING_GENERATING_LOCKS" 72 "$MISSING_GENERATING_DIGEST" \
  "$TEST_ROOT/missing-latest-generating-logs" "$TEST_ROOT/missing-latest-generating-result"; then
  echo "missing latest was accepted for a generating deferred journal" >&2
  exit 1
fi
python3 - "$MISSING_GENERATING_LOCKS" "$MISSING_GENERATING_HEAD" "$SUCCESS_REPO" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
assert journal["handoff_phase"] == "generating"
assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=sys.argv[3], text=True).strip() == sys.argv[2]
assert not (root / "publication/pushed.json").exists()
PY

MISSING_PUSH_READY_LOCKS="$TEST_ROOT/missing-latest-push-ready-locks"
MISSING_PUSH_READY_DIGEST="$(write_fixture_publication_latest "$MISSING_PUSH_READY_LOCKS" 73)"
write_fixture_deferred_journal \
  "$MISSING_PUSH_READY_LOCKS" "$SUCCESS_REPO" "$NPLUS_PUSH_BASELINE" \
  73 "$MISSING_PUSH_READY_DIGEST" "nplus-landed" push_ready "$NPLUS_PUSH_COMMIT"
rm -- "$MISSING_PUSH_READY_LOCKS/publication/latest.json"
if run_deferred_fixture \
  "$MISSING_PUSH_READY_LOCKS" 73 "$MISSING_PUSH_READY_DIGEST" \
  "$TEST_ROOT/missing-latest-push-ready-logs" "$TEST_ROOT/missing-latest-push-ready-result"; then
  echo "missing latest was accepted for a push-ready deferred journal" >&2
  exit 1
fi
python3 - "$MISSING_PUSH_READY_LOCKS" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
assert journal["handoff_phase"] == "push_ready"
assert not (root / "publication/pending.json").exists()
assert not (root / "publication/pushed.json").exists()
PY

MISSING_TERMINAL_LOCKS="$TEST_ROOT/missing-latest-terminal-locks"
MISSING_TERMINAL_RESULT="$TEST_ROOT/missing-latest-terminal-result"
MISSING_TERMINAL_DIGEST="$(write_fixture_publication_latest "$MISSING_TERMINAL_LOCKS" 74)"
write_fixture_deferred_journal \
  "$MISSING_TERMINAL_LOCKS" "$SUCCESS_REPO" "$NPLUS_PUSH_COMMIT" \
  74 "$MISSING_TERMINAL_DIGEST" "missing-latest-terminal" terminal_no_diff
rm -- "$MISSING_TERMINAL_LOCKS/publication/latest.json" "$MISSING_TERMINAL_LOCKS/publication/pushed.json"
run_deferred_fixture \
  "$MISSING_TERMINAL_LOCKS" 74 "$MISSING_TERMINAL_DIGEST" \
  "$TEST_ROOT/missing-latest-terminal-logs" "$MISSING_TERMINAL_RESULT"
if [[ "$(sed -n '1p' "$MISSING_TERMINAL_RESULT")" != "success_no_diff" ]] || \
  [[ ! -e "$MISSING_TERMINAL_LOCKS/publication/pushed.json" ]] || \
  [[ ! -e "$MISSING_TERMINAL_LOCKS/publisher-recovery.json" ]] || \
  [[ -e "$MISSING_TERMINAL_LOCKS/publication/latest.json" ]]; then
  echo "missing latest prevented durable terminal deferred recovery" >&2
  exit 1
fi
finalize_fixture_publication "$MISSING_TERMINAL_LOCKS" 74 "$MISSING_TERMINAL_DIGEST"

# Once Task 4 has CAS-cleared generation N from latest.json, the retained
# authenticated journal must still be sufficient to finish an interrupted N
# handoff. Re-prove the exact commit and reconstruct the missing checkpoint.
CAS_CLEARED_LOCKS="$TEST_ROOT/cas-cleared-recovery-locks"
CAS_CLEARED_RESULT="$TEST_ROOT/cas-cleared-recovery-result"
CAS_CLEARED_RAW="$TEST_ROOT/cas-cleared-recovery-raw"
CAS_CLEARED_PAGES="$TEST_ROOT/cas-cleared-recovery-pages"
CAS_CLEARED_DIGEST="$(write_fixture_publication_latest "$CAS_CLEARED_LOCKS" 68)"
write_fixture_deferred_journal \
  "$CAS_CLEARED_LOCKS" "$SUCCESS_REPO" "$NPLUS_PUSH_BASELINE" \
  68 "$CAS_CLEARED_DIGEST" "nplus-landed" raw_proven "$NPLUS_PUSH_COMMIT"
rm -- "$CAS_CLEARED_LOCKS/publication/latest.json" "$CAS_CLEARED_LOCKS/publication/pushed.json"
run_deferred_fixture \
  "$CAS_CLEARED_LOCKS" 68 "$CAS_CLEARED_DIGEST" \
  "$TEST_ROOT/cas-cleared-recovery-logs" "$CAS_CLEARED_RESULT" \
  "$CAS_CLEARED_RAW" "$CAS_CLEARED_PAGES"
if [[ "$(sed -n '1p' "$CAS_CLEARED_RESULT")" != "success_pushed" ]] || \
  [[ ! -e "$CAS_CLEARED_LOCKS/publication/pushed.json" ]] || \
  [[ ! -e "$CAS_CLEARED_LOCKS/publisher-recovery.json" ]] || \
  [[ -e "$CAS_CLEARED_LOCKS/publication/latest.json" ]] || \
  [[ ! -e "$CAS_CLEARED_RAW" || -e "$CAS_CLEARED_PAGES" ]]; then
  echo "missing latest after queue CAS prevented exact deferred recovery" >&2
  exit 1
fi
finalize_fixture_publication "$CAS_CLEARED_LOCKS" 68 "$CAS_CLEARED_DIGEST"

# A journal is authoritative only for its exact generation/digest. A same-
# generation different latest record or an older latest record is evidence of
# corruption/stale orchestration and must fail before mutating the repository.
CONFLICT_LOCKS="$TEST_ROOT/same-generation-conflict-locks"
CONFLICT_DIGEST="$(write_fixture_publication_latest "$CONFLICT_LOCKS" 69)"
write_fixture_deferred_journal \
  "$CONFLICT_LOCKS" "$SUCCESS_REPO" "$NPLUS_PUSH_COMMIT" \
  69 "$CONFLICT_DIGEST" "same-generation-conflict" generating
python3 - "$SOURCE_DIR/runner_publication_state.py" "$CONFLICT_LOCKS" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("runner_publication_state_conflict", Path(sys.argv[1]))
assert spec and spec.loader
state = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state
spec.loader.exec_module(state)
paths = state.state_paths(sys.argv[2])
latest = state.validate_latest(state._read_json(paths.latest))
latest["created_at_utc"] = "2026-08-30T12:35:00Z"
state.atomic_write_record(paths.latest, latest)
PY
if run_deferred_fixture \
  "$CONFLICT_LOCKS" 69 "$CONFLICT_DIGEST" \
  "$TEST_ROOT/same-generation-conflict-logs" "$TEST_ROOT/same-generation-conflict-result"; then
  echo "same-generation conflicting latest was accepted during journal recovery" >&2
  exit 1
fi
if [[ ! -e "$CONFLICT_LOCKS/publisher-recovery.json" || \
  -e "$CONFLICT_LOCKS/publication/pushed.json" ]]; then
  echo "same-generation latest conflict mutated deferred recovery state" >&2
  exit 1
fi

OLDER_LOCKS="$TEST_ROOT/older-latest-conflict-locks"
OLDER_DIGEST="$(write_fixture_publication_latest "$OLDER_LOCKS" 71)"
write_fixture_deferred_journal \
  "$OLDER_LOCKS" "$SUCCESS_REPO" "$NPLUS_PUSH_COMMIT" \
  71 "$OLDER_DIGEST" "older-latest-conflict" generating
write_fixture_publication_latest "$OLDER_LOCKS" 70 >/dev/null
if run_deferred_fixture \
  "$OLDER_LOCKS" 71 "$OLDER_DIGEST" \
  "$TEST_ROOT/older-latest-conflict-logs" "$TEST_ROOT/older-latest-conflict-result"; then
  echo "older latest was accepted during journal recovery" >&2
  exit 1
fi
if [[ ! -e "$OLDER_LOCKS/publisher-recovery.json" || \
  -e "$OLDER_LOCKS/publication/pushed.json" ]]; then
  echo "older latest conflict mutated deferred recovery state" >&2
  exit 1
fi

# A journaled commit remains landed when the remote advances to any descendant.
# Interrupted recovery must prove the exact journal commit rather than
# classifying the descendant as a competing alignment target.
DESCENDANT_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-30T13:20:00Z"
git -C "$SUCCESS_REPO" add generated public/generated
git -C "$SUCCESS_REPO" commit -qm "[cron] deferred commit before descendant" \
  -m "Refresh-Runner-ID: windows-wsl" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: descendant-landed"
DESCENDANT_LANDED="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
git -C "$SUCCESS_REPO" push -q
DESCENDANT_PEER_REPO="$TEST_ROOT/descendant-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$DESCENDANT_PEER_REPO"
git -C "$DESCENDANT_PEER_REPO" config user.name "Degen Dogs Descendant"
git -C "$DESCENDANT_PEER_REPO" config user.email "degen-dogs-descendant@example.invalid"
printf '%s\n' 'remote descendant' >"$DESCENDANT_PEER_REPO/descendant-fixture.txt"
git -C "$DESCENDANT_PEER_REPO" add descendant-fixture.txt
git -C "$DESCENDANT_PEER_REPO" commit -qm "fixture: remote descendant after landed publication"
DESCENDANT_REMOTE="$(git -C "$DESCENDANT_PEER_REPO" rev-parse HEAD)"
git -C "$DESCENDANT_PEER_REPO" push -q origin main

DESCENDANT_LOCKS="$TEST_ROOT/descendant-recovery-locks"
DESCENDANT_RESULT="$TEST_ROOT/descendant-recovery-result"
DESCENDANT_RAW="$TEST_ROOT/descendant-recovery-raw"
DESCENDANT_PAGES="$TEST_ROOT/descendant-recovery-pages"
DESCENDANT_DIGEST="$(write_fixture_publication_latest "$DESCENDANT_LOCKS" 66)"
write_fixture_deferred_journal \
  "$DESCENDANT_LOCKS" "$SUCCESS_REPO" "$DESCENDANT_BASELINE" \
  66 "$DESCENDANT_DIGEST" "descendant-landed" push_ready "$DESCENDANT_LANDED"
run_deferred_fixture \
  "$DESCENDANT_LOCKS" 66 "$DESCENDANT_DIGEST" \
  "$TEST_ROOT/descendant-recovery-logs" "$DESCENDANT_RESULT" \
  "$DESCENDANT_RAW" "$DESCENDANT_PAGES"
python3 - "$DESCENDANT_LOCKS" "$DESCENDANT_LANDED" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
pending = json.loads((root / "publication/pending.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
assert journal["remote_commit"] == pending["commit_sha"] == checkpoint["commit_sha"] == sys.argv[2]
assert journal["handoff_phase"] == "raw_proven"
PY
if [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$DESCENDANT_LANDED" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$DESCENDANT_REMOTE" ]] || \
  [[ "$(sed -n '1p' "$DESCENDANT_RESULT")" != "success_pushed" ]] || \
  [[ ! -e "$DESCENDANT_RAW" || -e "$DESCENDANT_PAGES" ]]; then
  echo "remote-descendant interrupted recovery did not preserve and prove the exact landed commit" >&2
  exit 1
fi
finalize_fixture_publication "$DESCENDANT_LOCKS" 66 "$DESCENDANT_DIGEST"

# Exercise the same descendant classification inside an ambiguous push: the
# pre-push hook lands the publisher commit, advances main by one descendant,
# and lets the outer CAS report failure. Fresh ancestry plus raw proof must
# complete the exact publisher handoff.
git -C "$SUCCESS_REPO" fetch -q origin main
git -C "$SUCCESS_REPO" merge -q --ff-only origin/main
AMBIGUOUS_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
AMBIGUOUS_HOOK_MARKER="$TEST_ROOT/ambiguous-descendant-hook"
AMBIGUOUS_REMOTE_SHA_FILE="$TEST_ROOT/ambiguous-descendant-remote-sha"
: >"$AMBIGUOUS_HOOK_MARKER"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [[ -e "$FIXTURE_AMBIGUOUS_HOOK_MARKER" ]]; then' \
  '  rm -- "$FIXTURE_AMBIGUOUS_HOOK_MARKER"' \
  '  runner_commit="$(git rev-parse HEAD)"' \
  '  git push --no-verify --force-with-lease="refs/heads/main:${FIXTURE_AMBIGUOUS_BASELINE}" origin "${runner_commit}:refs/heads/main"' \
  '  tree="$(git rev-parse "${runner_commit}^{tree}")"' \
  '  descendant="$(printf "%s\n" "fixture: descendant after ambiguous landed push" | git commit-tree "$tree" -p "$runner_commit")"' \
  '  git push --no-verify origin "${descendant}:refs/heads/main"' \
  '  printf "%s\n" "$descendant" >"$FIXTURE_AMBIGUOUS_REMOTE_SHA_FILE"' \
  'fi' \
  >"$SUCCESS_REPO/.git/hooks/pre-push"
chmod +x "$SUCCESS_REPO/.git/hooks/pre-push"
AMBIGUOUS_LOCKS="$TEST_ROOT/ambiguous-descendant-locks"
AMBIGUOUS_RESULT="$TEST_ROOT/ambiguous-descendant-result"
AMBIGUOUS_RAW="$TEST_ROOT/ambiguous-descendant-raw"
AMBIGUOUS_PAGES="$TEST_ROOT/ambiguous-descendant-pages"
AMBIGUOUS_DIGEST="$(write_fixture_publication_latest "$AMBIGUOUS_LOCKS" 67)"
export FIXTURE_AMBIGUOUS_HOOK_MARKER="$AMBIGUOUS_HOOK_MARKER"
export FIXTURE_AMBIGUOUS_BASELINE="$AMBIGUOUS_BASELINE"
export FIXTURE_AMBIGUOUS_REMOTE_SHA_FILE="$AMBIGUOUS_REMOTE_SHA_FILE"
run_deferred_fixture \
  "$AMBIGUOUS_LOCKS" 67 "$AMBIGUOUS_DIGEST" \
  "$TEST_ROOT/ambiguous-descendant-logs" "$AMBIGUOUS_RESULT" \
  "$AMBIGUOUS_RAW" "$AMBIGUOUS_PAGES"
unset FIXTURE_AMBIGUOUS_HOOK_MARKER FIXTURE_AMBIGUOUS_BASELINE FIXTURE_AMBIGUOUS_REMOTE_SHA_FILE
rm -- "$SUCCESS_REPO/.git/hooks/pre-push"
AMBIGUOUS_PUBLISHER="$(sed -n '2p' "$AMBIGUOUS_RESULT")"
AMBIGUOUS_REMOTE="$(<"$AMBIGUOUS_REMOTE_SHA_FILE")"
python3 - "$AMBIGUOUS_LOCKS" "$AMBIGUOUS_PUBLISHER" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
pending = json.loads((root / "publication/pending.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
assert journal["remote_commit"] == pending["commit_sha"] == checkpoint["commit_sha"] == sys.argv[2]
assert journal["handoff_phase"] == "raw_proven"
PY
if [[ "$AMBIGUOUS_PUBLISHER" != "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" ]] || \
  [[ "$AMBIGUOUS_REMOTE" != "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" ]] || \
  ! git --git-dir="$REJECT_REMOTE" merge-base --is-ancestor "$AMBIGUOUS_PUBLISHER" "$AMBIGUOUS_REMOTE" || \
  [[ "$(sed -n '1p' "$AMBIGUOUS_RESULT")" != "success_pushed" ]] || \
  [[ ! -e "$AMBIGUOUS_RAW" || -e "$AMBIGUOUS_PAGES" ]]; then
  echo "ambiguous landed push was not recovered from its remote descendant" >&2
  exit 1
fi
finalize_fixture_publication "$AMBIGUOUS_LOCKS" 67 "$AMBIGUOUS_DIGEST"

# Once push_ready is durable, termination while git push is still blocking is
# ambiguous. The hook lands the exact commit, terminates the publisher before
# the outer push returns, and a fresh invocation must classify/prove it.
git -C "$SUCCESS_REPO" fetch -q origin main
git -C "$SUCCESS_REPO" merge -q --ff-only origin/main
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-30T13:31:00Z"
git -C "$SUCCESS_REPO" add generated public/generated
git -C "$SUCCESS_REPO" commit -qm "fixture: force push-time termination publication"
git -C "$SUCCESS_REPO" push -q
TERMINATE_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
TERMINATE_HOOK_MARKER="$TEST_ROOT/terminate-after-accept-hook"
: >"$TERMINATE_HOOK_MARKER"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [[ -e "$FIXTURE_TERMINATE_HOOK_MARKER" ]]; then' \
  '  rm -- "$FIXTURE_TERMINATE_HOOK_MARKER"' \
  '  runner_commit="$(git rev-parse HEAD)"' \
  '  git push --no-verify --force-with-lease="refs/heads/main:${FIXTURE_TERMINATE_BASELINE}" origin "${runner_commit}:refs/heads/main"' \
  '  publisher_pid="$(ps -o ppid= -p "$PPID" | tr -d "[:space:]")"' \
  '  kill -TERM "$publisher_pid"' \
  '  sleep 1' \
  '  exit 1' \
  'fi' \
  >"$SUCCESS_REPO/.git/hooks/pre-push"
chmod +x "$SUCCESS_REPO/.git/hooks/pre-push"
TERMINATE_LOCKS="$TEST_ROOT/terminate-after-accept-locks"
TERMINATE_RESULT="$TEST_ROOT/terminate-after-accept-result"
TERMINATE_RAW="$TEST_ROOT/terminate-after-accept-raw"
TERMINATE_PAGES="$TEST_ROOT/terminate-after-accept-pages"
TERMINATE_DIGEST="$(write_fixture_publication_latest "$TERMINATE_LOCKS" 75)"
export FIXTURE_TERMINATE_HOOK_MARKER="$TERMINATE_HOOK_MARKER"
export FIXTURE_TERMINATE_BASELINE="$TERMINATE_BASELINE"
if run_deferred_fixture \
  "$TERMINATE_LOCKS" 75 "$TERMINATE_DIGEST" \
  "$TEST_ROOT/terminate-after-accept-logs" "$TERMINATE_RESULT" \
  "$TERMINATE_RAW" "$TERMINATE_PAGES"; then
  echo "publisher unexpectedly returned success after forced push-time termination" >&2
  exit 1
fi
unset FIXTURE_TERMINATE_HOOK_MARKER FIXTURE_TERMINATE_BASELINE
rm -- "$SUCCESS_REPO/.git/hooks/pre-push"
TERMINATE_LANDED="$(git --git-dir="$REJECT_REMOTE" rev-parse main)"
TERMINATE_LOCAL="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
if [[ "$TERMINATE_LOCAL" == "$TERMINATE_BASELINE" ]] || \
  [[ "$TERMINATE_LOCAL" != "$TERMINATE_LANDED" ]] || \
  [[ ! -e "$TERMINATE_LOCKS/publisher-recovery.json" ]]; then
  echo "ambiguous push-time termination rewound the landed commit or removed push-ready evidence" >&2
  exit 1
fi
python3 - "$TERMINATE_LOCKS" "$TERMINATE_BASELINE" "$TERMINATE_LANDED" "$SUCCESS_REPO" <<'PY'
import json
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=sys.argv[4], text=True).strip()
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
assert head == sys.argv[3] and head != sys.argv[2]
assert journal["handoff_phase"] == "push_ready"
assert journal["remote_commit"] == head
assert not (root / "publication/pending.json").exists()
assert not (root / "publication/pushed.json").exists()
PY
run_deferred_fixture \
  "$TERMINATE_LOCKS" 75 "$TERMINATE_DIGEST" \
  "$TEST_ROOT/terminate-recovery-logs" "$TERMINATE_RESULT" \
  "$TERMINATE_RAW" "$TERMINATE_PAGES"
if [[ "$(sed -n '1p' "$TERMINATE_RESULT")" != "success_pushed" ]] || \
  [[ "$(sed -n '2p' "$TERMINATE_RESULT")" != "$TERMINATE_LANDED" ]] || \
  [[ ! -e "$TERMINATE_RAW" || -e "$TERMINATE_PAGES" ]]; then
  echo "fresh recovery did not prove the exact commit after push-time termination" >&2
  exit 1
fi
finalize_fixture_publication "$TERMINATE_LOCKS" 75 "$TERMINATE_DIGEST"

# Deferred mode must also classify an ordinary sibling CAS winner after the
# rejected push. The explicit push-rejected transition is the only state path
# allowed to move push_ready into peer alignment/terminal evidence.
touch_valid_refresh_status "$SUCCESS_REPO" "2026-08-30T13:32:00Z"
git -C "$SUCCESS_REPO" add generated public/generated
git -C "$SUCCESS_REPO" commit -qm "fixture: prepare deferred CAS collision baseline"
git -C "$SUCCESS_REPO" push -q
DEFERRED_CAS_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
DEFERRED_CAS_PEER_REPO="$TEST_ROOT/deferred-cas-peer-repo"
git clone -q --branch main "$REJECT_REMOTE" "$DEFERRED_CAS_PEER_REPO"
git -C "$DEFERRED_CAS_PEER_REPO" config user.name "Degen Dogs Deferred CAS Peer"
git -C "$DEFERRED_CAS_PEER_REPO" config user.email "degen-dogs-deferred-cas-peer@example.invalid"
touch_valid_refresh_status "$DEFERRED_CAS_PEER_REPO" "2026-08-30T20:12:00Z"
git -C "$DEFERRED_CAS_PEER_REPO" add generated public/generated
git -C "$DEFERRED_CAS_PEER_REPO" commit -qm "[cron] deferred CAS peer winner" \
  -m "Refresh-Runner-ID: fixture-peer" \
  -m "Refresh-Run-Scope: current" \
  -m "Refresh-Run-ID: deferred-cas-peer-winner"
DEFERRED_CAS_PEER="$(git -C "$DEFERRED_CAS_PEER_REPO" rev-parse HEAD)"
git -C "$DEFERRED_CAS_PEER_REPO" push -q origin \
  "${DEFERRED_CAS_PEER}:refs/heads/deferred-cas-peer-fixture"
DEFERRED_CAS_HOOK_MARKER="$TEST_ROOT/deferred-cas-hook"
: >"$DEFERRED_CAS_HOOK_MARKER"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [[ -e "$FIXTURE_DEFERRED_CAS_HOOK_MARKER" ]]; then' \
  '  rm -- "$FIXTURE_DEFERRED_CAS_HOOK_MARKER"' \
  '  git --git-dir="$FIXTURE_DEFERRED_CAS_REMOTE" update-ref refs/heads/main "$FIXTURE_DEFERRED_CAS_PEER" "$FIXTURE_DEFERRED_CAS_BASELINE"' \
  'fi' \
  >"$SUCCESS_REPO/.git/hooks/pre-push"
chmod +x "$SUCCESS_REPO/.git/hooks/pre-push"
DEFERRED_CAS_LOCKS="$TEST_ROOT/deferred-cas-collision-locks"
DEFERRED_CAS_RESULT="$TEST_ROOT/deferred-cas-collision-result"
DEFERRED_CAS_PAGES="$TEST_ROOT/deferred-cas-collision-pages"
DEFERRED_CAS_DIGEST="$(write_fixture_publication_latest "$DEFERRED_CAS_LOCKS" 76)"
export FIXTURE_DEFERRED_CAS_HOOK_MARKER="$DEFERRED_CAS_HOOK_MARKER"
export FIXTURE_DEFERRED_CAS_REMOTE="$REJECT_REMOTE"
export FIXTURE_DEFERRED_CAS_PEER="$DEFERRED_CAS_PEER"
export FIXTURE_DEFERRED_CAS_BASELINE="$DEFERRED_CAS_BASELINE"
run_deferred_fixture \
  "$DEFERRED_CAS_LOCKS" 76 "$DEFERRED_CAS_DIGEST" \
  "$TEST_ROOT/deferred-cas-collision-logs" "$DEFERRED_CAS_RESULT" \
  "" "$DEFERRED_CAS_PAGES"
unset FIXTURE_DEFERRED_CAS_HOOK_MARKER FIXTURE_DEFERRED_CAS_REMOTE \
  FIXTURE_DEFERRED_CAS_PEER FIXTURE_DEFERRED_CAS_BASELINE
rm -- "$SUCCESS_REPO/.git/hooks/pre-push"
python3 - "$DEFERRED_CAS_LOCKS" "$DEFERRED_CAS_PEER" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
journal = json.loads((root / "publisher-recovery.json").read_text(encoding="utf-8"))
checkpoint = json.loads((root / "publication/pushed.json").read_text(encoding="utf-8"))
assert journal["handoff_phase"] == "terminal"
assert journal["terminal_outcome"] == checkpoint["outcome"] == "peer_superseded"
assert journal["remote_commit"] == checkpoint["commit_sha"] == sys.argv[2]
assert not (root / "publication/pending.json").exists()
PY
if [[ -e "$DEFERRED_CAS_HOOK_MARKER" ]] || \
  [[ "$(git -C "$SUCCESS_REPO" rev-parse HEAD)" != "$DEFERRED_CAS_PEER" ]] || \
  [[ "$(git --git-dir="$REJECT_REMOTE" rev-parse main)" != "$DEFERRED_CAS_PEER" ]] || \
  [[ "$(sed -n '1p' "$DEFERRED_CAS_RESULT")" != "success_superseded_by_peer" ]] || \
  [[ "$(sed -n '2p' "$DEFERRED_CAS_RESULT")" != "$DEFERRED_CAS_PEER" ]] || \
  [[ -e "$DEFERRED_CAS_PAGES" ]]; then
  echo "deferred sibling CAS collision was not durably classified as peer supersession" >&2
  exit 1
fi
finalize_fixture_publication "$DEFERRED_CAS_LOCKS" 76 "$DEFERRED_CAS_DIGEST"

# Legacy inline journals must retain their original fail-closed shape. Fields
# that are present but null/empty are malformed, not equivalent to absence.
git -C "$SUCCESS_REPO" fetch -q origin main
git -C "$SUCCESS_REPO" merge -q --ff-only origin/main
LEGACY_NULL_BASELINE="$(git -C "$SUCCESS_REPO" rev-parse HEAD)"
LEGACY_NULL_LOCKS="$TEST_ROOT/legacy-null-alignment-locks"
LEGACY_NULL_JOURNAL="$LEGACY_NULL_LOCKS/publisher-recovery.json"
write_fixture_recovery_journal \
  "$LEGACY_NULL_JOURNAL" "$SUCCESS_REPO" "$LEGACY_NULL_BASELINE" "legacy-null-alignment"
python3 - "$LEGACY_NULL_JOURNAL" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["alignment_runner_commit"] = None
value["alignment_remote_head"] = ""
value["alignment_result"] = None
path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
set +e
HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/legacy-null-alignment-logs" \
DEGEN_DOGS_LOCK_DIR="$LEGACY_NULL_LOCKS" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" || ! -e "$LEGACY_NULL_JOURNAL" ]]; then
  echo "legacy null/empty alignment fields were normalized into an absent alignment" >&2
  exit 1
fi

# A caller cannot bypass the shared lock with a trusted-looking environment
# flag unless it also supplies the inherited, matching locked descriptor.
set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/invalid-locks" \
DEGEN_DOGS_LOCK_HELD=1 \
DEGEN_DOGS_LOCK_FD=9999 \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" ]]; then
  echo "forged inherited lock flag was accepted" >&2
  exit 1
fi

# Never treat arbitrary local commits as a failed-runner push recovery queue.
git -C "$TEST_ROOT" init -q --bare remote.git
git -C "$TEST_REPO" remote add origin "$TEST_ROOT/remote.git"
git -C "$TEST_REPO" push -q -u origin main
printf '%s\n' 'manual local history' >"$TEST_REPO/manual.txt"
git -C "$TEST_REPO" add manual.txt
git -C "$TEST_REPO" commit -qm "manual local commit"
set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/ahead-locks" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=0 \
"$TEST_REPO/scripts/refresh_and_publish.sh"
status=$?
set -e
if [[ "$status" == "0" ]]; then
  echo "unverified local-ahead history was accepted" >&2
  exit 1
fi
if ! grep -q "refusing to publish unverified history" "$TEST_ROOT/logs/refresh.log"; then
  echo "local-ahead rejection was not logged" >&2
  exit 1
fi
if [[ "$(git --git-dir="$TEST_ROOT/remote.git" rev-parse main)" == "$(git -C "$TEST_REPO" rev-parse HEAD)" ]]; then
  echo "unverified local-ahead commit reached the remote" >&2
  exit 1
fi

echo "refresh rollback, lock, and publish-history regression tests passed"
