#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Refresh Degen Dogs Mission 3 cached blockchain data locally and publish it to GitHub Pages.
# Intended to run from a supervised private macOS or Linux runner.

PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

USER_HOME="${HOME:-$(python3 - <<'PY'
import os
import pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)}"
export HOME="$USER_HOME"

REPO_DIR="${DEGEN_DOGS_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUNNER_VENV_ERROR=""
if [[ -e "${REPO_DIR}/.venv" || -L "${REPO_DIR}/.venv" ]]; then
  if [[ "$REPO_DIR" != *:* && -x "${REPO_DIR}/.venv/bin/python3" ]] && \
    [[ -x "${REPO_DIR}/scripts/runtime-bin/python3" ]] && \
    PYTHONNOUSERSITE=1 "${REPO_DIR}/.venv/bin/python3" -I -c \
      'import Crypto; from Crypto.Hash import keccak; assert Crypto.__version__ == "3.23.0"; assert keccak.new(digest_bits=256, data=b"").hexdigest() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"' \
      >/dev/null 2>&1; then
  PATH="${REPO_DIR}/scripts/runtime-bin:${PATH}"
  export PATH
  else
    RUNNER_VENV_ERROR="repo Python virtualenv is present but failed the pinned Keccak runtime check"
  fi
fi
LOG_DIR="${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}"
LOCK_DIR="${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}"
REFRESH_LOCK_PATH="${DEGEN_DOGS_REFRESH_LOCK_PATH:-${MISSION3_REFRESH_LOCK_PATH:-${LOCK_DIR}/refresh.lock}}"
REFRESH_LOCK_PATH="$(python3 - "$REFRESH_LOCK_PATH" <<'PY'
import os
import sys

print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
)"
RECOVERY_STATE_DIR="$(dirname "$REFRESH_LOCK_PATH")"
REMOTE="${DEGEN_DOGS_REMOTE:-origin}"
BRANCH="${DEGEN_DOGS_BRANCH:-main}"
COMMIT_PREFIX="${DEGEN_DOGS_COMMIT_PREFIX:-[cron]}"
SKIP_PUSH="${DEGEN_DOGS_SKIP_PUSH:-0}"
SKIP_PULL="${DEGEN_DOGS_SKIP_PULL:-0}"
RUN_MISSION3_ARCHIVE="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE:-0}"
FULL_REFRESH="${DEGEN_DOGS_FULL_REFRESH:-0}"
LIVE_VERIFY_AFTER_PUSH="${DEGEN_DOGS_LIVE_VERIFY_AFTER_PUSH:-1}"
GIT_RETRY_ATTEMPTS="${DEGEN_DOGS_GIT_RETRY_ATTEMPTS:-4}"
GIT_RETRY_BASE_SECONDS="${DEGEN_DOGS_GIT_RETRY_BASE_SECONDS:-2}"
GIT_RETRY_MAX_SECONDS="${DEGEN_DOGS_GIT_RETRY_MAX_SECONDS:-30}"
GIT_RETRY_JITTER_SECONDS="${DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS:-3}"
SUPERSESSION_RETRY_COUNT="${DEGEN_DOGS_SUPERSESSION_RETRY_COUNT:-0}"
RUNNER_ID="${DEGEN_DOGS_RUNNER_ID:-}"
ORIGINAL_ARGS=("$@")
derive_run_scope() {
  if [[ "$RUN_MISSION3_ARCHIVE" == "1" && "$FULL_REFRESH" == "1" ]]; then
    printf '%s\n' "archive_full"
  elif [[ "$RUN_MISSION3_ARCHIVE" == "1" ]]; then
    printf '%s\n' "archive"
  elif [[ "$FULL_REFRESH" == "1" ]]; then
    printf '%s\n' "full"
  else
    printf '%s\n' "current"
  fi
}

RUN_SCOPE="$(derive_run_scope)"
export DEGEN_DOGS_RUN_SCOPE="$RUN_SCOPE"

if [[ -z "$RUNNER_ID" ]]; then
  RUNNER_ID="$(python3 - <<'PY'
from __future__ import annotations

import hashlib
import socket

identity = socket.gethostname().encode("utf-8", errors="surrogateescape")
print("runner-" + hashlib.sha256(identity).hexdigest()[:12])
PY
)"
fi
export DEGEN_DOGS_RUNNER_ID="$RUNNER_ID"

# shellcheck source=runner_permissions.sh
source "${REPO_DIR}/scripts/runner_permissions.sh"

PUBLISH_PATHS=(
  "README.md"
  "index.html"
  "generated"
  "public"
  "archive/mission3/data/generated"
  "archive/data/generated"
  "archive/data/identity/wallet_profiles.json"
  "archive/dogs"
  "archive/prices/data/generated"
  "archive/prices/data/raw"
)

BASELINE_HEAD=""
MUTATION_STARTED=0
RUNNER_COMMIT_HEAD=""
RUNNER_COMMIT_RUN_ID=""
RUNNER_COMMIT_RUNNER_ID=""
RUNNER_COMMIT_SCOPE=""
ARTIFACT_LIST=""
LIVE_VERIFY_ENV=""
LOCAL_AHEAD_COUNT=0
QUARANTINE_DIR=""
RECOVERY_JOURNAL="${RECOVERY_STATE_DIR}/publisher-recovery.json"
RECOVERY_SUPERSEDED=0
PUSH_REMOTE_HEAD=""

utc_stamp() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

export DEGEN_DOGS_REFRESH_RUN_ID="${DEGEN_DOGS_REFRESH_RUN_ID:-refresh-${RUNNER_ID}-$(date -u '+%Y%m%dT%H%M%SZ')-$$}"
export DEGEN_DOGS_REFRESH_QUEUED_AT_UTC="${DEGEN_DOGS_REFRESH_QUEUED_AT_UTC:-$(utc_stamp)}"
export DEGEN_DOGS_REFRESH_TRIGGER="${DEGEN_DOGS_REFRESH_TRIGGER:-hourly_refresh}"
export DEGEN_DOGS_REFRESH_TELEMETRY_PATH="${DEGEN_DOGS_REFRESH_TELEMETRY_PATH:-${REPO_DIR}/.local/refresh_runs.jsonl}"
export DEGEN_DOGS_REFRESH_METRICS_PATH="${DEGEN_DOGS_REFRESH_METRICS_PATH:-${REPO_DIR}/logs/refresh-metrics.jsonl}"

degen_dogs_private_dir "$LOG_DIR"
degen_dogs_private_dir "$LOCK_DIR"
degen_dogs_private_dir "$RECOVERY_STATE_DIR"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/refresh.log}"
degen_dogs_private_file "$LOG_FILE"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
  export DEGEN_DOGS_REFRESH_ERROR="$*"
  log "error: $*"
  exit 1
}

[[ -z "$RUNNER_VENV_ERROR" ]] || fail "$RUNNER_VENV_ERROR"
validate_nonnegative_integer() {
  local name="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9]+$ ]] || fail "${name} must be a non-negative integer: ${value}"
}

retry_delay_seconds() {
  local attempt="$1"
  local delay="$GIT_RETRY_BASE_SECONDS"
  local exponent=1
  local jitter=0
  local i

  for ((i = 1; i < attempt; i++)); do
    exponent=$((exponent * 2))
  done
  delay=$((delay * exponent))
  if (( delay > GIT_RETRY_MAX_SECONDS )); then
    delay="$GIT_RETRY_MAX_SECONDS"
  fi
  if (( GIT_RETRY_JITTER_SECONDS > 0 )); then
    jitter=$((RANDOM % (GIT_RETRY_JITTER_SECONDS + 1)))
  fi
  printf '%s\n' "$((delay + jitter))"
}

run_with_retry() {
  local label="$1"
  shift
  local attempt=1
  local status=0
  local delay=0

  while (( attempt <= GIT_RETRY_ATTEMPTS )); do
    log "${label} attempt=${attempt}/${GIT_RETRY_ATTEMPTS}"
    if "$@"; then
      return 0
    else
      status=$?
    fi
    if (( attempt == GIT_RETRY_ATTEMPTS )); then
      log "${label} failed after ${attempt} attempts status=${status}"
      return "$status"
    fi
    delay="$(retry_delay_seconds "$attempt")"
    log "${label} retrying in ${delay}s after status=${status}"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
  return "$status"
}

remove_recovery_journal() {
  [[ -e "$RECOVERY_JOURNAL" || -L "$RECOVERY_JOURNAL" ]] || return 0
  python3 - "$RECOVERY_JOURNAL" <<'PY'
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
details = path.lstat()
if (
    not stat.S_ISREG(details.st_mode)
    or stat.S_ISLNK(details.st_mode)
    or details.st_uid != os.getuid()
    or details.st_nlink != 1
    or stat.S_IMODE(details.st_mode) != 0o600
):
    raise SystemExit(f"refusing unsafe publisher recovery journal: {path}")
path.unlink()
PY
}

write_recovery_journal() {
  export RECOVERY_JOURNAL REPO_DIR BRANCH BASELINE_HEAD DEGEN_DOGS_REFRESH_RUN_ID DEGEN_DOGS_RUNNER_ID DEGEN_DOGS_RUN_SCOPE
  python3 - "${PUBLISH_PATHS[@]}" <<'PY'
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

path = Path(os.environ["RECOVERY_JOURNAL"])
if path.exists() or path.is_symlink():
    raise SystemExit(f"publisher recovery journal already exists: {path}")
payload = {
    "schema_version": 1,
    "repo_realpath": str(Path(os.environ["REPO_DIR"]).resolve()),
    "branch": os.environ["BRANCH"],
    "baseline_head": os.environ["BASELINE_HEAD"],
    "run_id": os.environ["DEGEN_DOGS_REFRESH_RUN_ID"],
    "runner_id": os.environ["DEGEN_DOGS_RUNNER_ID"],
    "run_scope": os.environ["DEGEN_DOGS_RUN_SCOPE"],
    "created_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "publish_paths": list(sys.argv[1:]),
}
descriptor, temporary_name = tempfile.mkstemp(prefix=".publisher-recovery.", suffix=".tmp", dir=path.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
PY
}

read_recovery_journal_baseline() {
  python3 - "$RECOVERY_JOURNAL" "$REPO_DIR" "$BRANCH" "${PUBLISH_PATHS[@]}" <<'PY'
from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
expected_repo = str(Path(sys.argv[2]).resolve())
expected_branch = sys.argv[3]
expected_paths = sys.argv[4:]
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
details = os.fstat(descriptor)
if (
    not stat.S_ISREG(details.st_mode)
    or details.st_uid != os.getuid()
    or details.st_nlink != 1
    or stat.S_IMODE(details.st_mode) != 0o600
    or details.st_size > 16_384
):
    os.close(descriptor)
    raise SystemExit("publisher recovery journal is not a private owned regular file")
with os.fdopen(descriptor, encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, dict) or payload.get("schema_version") != 1:
    raise SystemExit("publisher recovery journal schema is invalid")
baseline = payload.get("baseline_head")
run_id = payload.get("run_id")
runner_id = payload.get("runner_id")
run_scope = payload.get("run_scope")
alignment_runner_commit = payload.get("alignment_runner_commit", "-")
alignment_remote_head = payload.get("alignment_remote_head", "-")
alignment_result = payload.get("alignment_result", "-")
if (
    payload.get("repo_realpath") != expected_repo
    or payload.get("branch") != expected_branch
    or payload.get("publish_paths") != expected_paths
    or not isinstance(baseline, str)
    or re.fullmatch(r"[0-9a-f]{40}", baseline) is None
    or not isinstance(run_id, str)
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id) is None
    or not isinstance(runner_id, str)
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", runner_id) is None
    or run_scope not in {"current", "full", "archive", "archive_full"}
):
    raise SystemExit("publisher recovery journal provenance is invalid")
alignment_values = (alignment_runner_commit, alignment_remote_head, alignment_result)
if alignment_values != ("-", "-", "-") and (
    not isinstance(alignment_runner_commit, str)
    or re.fullmatch(r"[0-9a-f]{40}", alignment_runner_commit) is None
    or not isinstance(alignment_remote_head, str)
    or re.fullmatch(r"[0-9a-f]{40}", alignment_remote_head) is None
    or alignment_result not in {"peer_supersedes", "regenerate"}
):
    raise SystemExit("publisher recovery journal alignment state is invalid")
print(
    f"{baseline}\t{run_id}\t{runner_id}\t{run_scope}\t{alignment_runner_commit}"
    f"\t{alignment_remote_head}\t{alignment_result}"
)
PY
}

update_recovery_run_scope() {
  local run_scope="$1"
  python3 - "$RECOVERY_JOURNAL" "$REPO_DIR" "$BRANCH" "$BASELINE_HEAD" \
    "$DEGEN_DOGS_REFRESH_RUN_ID" "$run_scope" "${PUBLISH_PATHS[@]}" <<'PY'
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

journal_name, repo_name, branch, baseline, run_id, run_scope, *publish_paths = sys.argv[1:]
path = Path(journal_name)
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
details = os.fstat(descriptor)
if (
    not stat.S_ISREG(details.st_mode)
    or details.st_uid != os.getuid()
    or details.st_nlink != 1
    or stat.S_IMODE(details.st_mode) != 0o600
    or details.st_size > 16_384
):
    os.close(descriptor)
    raise SystemExit("publisher recovery journal is not a private owned regular file")
with os.fdopen(descriptor, encoding="utf-8") as handle:
    payload = json.load(handle)
if (
    not isinstance(payload, dict)
    or payload.get("schema_version") != 1
    or payload.get("repo_realpath") != str(Path(repo_name).resolve())
    or payload.get("branch") != branch
    or payload.get("baseline_head") != baseline
    or payload.get("run_id") != run_id
    or payload.get("publish_paths") != publish_paths
    or run_scope not in {"full", "archive", "archive_full"}
):
    raise SystemExit("publisher recovery journal cannot be attributed to this scope update")
payload["run_scope"] = run_scope
descriptor, temporary_name = tempfile.mkstemp(prefix=".publisher-recovery.", suffix=".tmp", dir=path.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
PY
}

write_recovery_alignment() {
  local runner_commit="$1"
  local remote_head="$2"
  local alignment_result="$3"
  python3 - "$RECOVERY_JOURNAL" "$REPO_DIR" "$BRANCH" "$BASELINE_HEAD" \
    "$RUNNER_COMMIT_RUN_ID" "$runner_commit" "$remote_head" "$alignment_result" \
    "${PUBLISH_PATHS[@]}" <<'PY'
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

(
    journal_name,
    repo_name,
    branch,
    baseline,
    run_id,
    runner_commit,
    remote_head,
    alignment_result,
    *publish_paths,
) = sys.argv[1:]
path = Path(journal_name)
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
details = os.fstat(descriptor)
if (
    not stat.S_ISREG(details.st_mode)
    or details.st_uid != os.getuid()
    or details.st_nlink != 1
    or stat.S_IMODE(details.st_mode) != 0o600
    or details.st_size > 16_384
):
    os.close(descriptor)
    raise SystemExit("publisher recovery journal is not a private owned regular file")
with os.fdopen(descriptor, encoding="utf-8") as handle:
    payload = json.load(handle)
sha = re.compile(r"[0-9a-f]{40}")
if (
    not isinstance(payload, dict)
    or payload.get("schema_version") != 1
    or payload.get("repo_realpath") != str(Path(repo_name).resolve())
    or payload.get("branch") != branch
    or payload.get("baseline_head") != baseline
    or payload.get("run_id") != run_id
    or payload.get("publish_paths") != publish_paths
    or sha.fullmatch(runner_commit) is None
    or sha.fullmatch(remote_head) is None
    or alignment_result not in {"peer_supersedes", "regenerate"}
):
    raise SystemExit("publisher recovery journal cannot be attributed to this alignment")
payload["alignment_runner_commit"] = runner_commit
payload["alignment_remote_head"] = remote_head
payload["alignment_result"] = alignment_result
descriptor, temporary_name = tempfile.mkstemp(prefix=".publisher-recovery.", suffix=".tmp", dir=path.parent)
temporary = Path(temporary_name)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
PY
}

validate_runner_commit() {
  local commit="$1"
  local baseline="$2"
  local run_id="$3"
  local runner_id="$4"
  local run_scope="$5"
  python3 - "$commit" "$baseline" "$run_id" "$runner_id" "$run_scope" <<'PY'
from __future__ import annotations

import os
import re
import subprocess
import sys

commit, baseline, run_id, runner_id, run_scope = sys.argv[1:]
sha = re.compile(r"[0-9a-f]{40}")
if (
    not sha.fullmatch(commit)
    or not sha.fullmatch(baseline)
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", run_id) is None
    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", runner_id) is None
    or run_scope not in {"current", "full", "archive", "archive_full"}
):
    raise SystemExit("runner commit identity is invalid")

history = subprocess.check_output(
    ["git", "rev-list", "--parents", "-n", "1", commit],
    text=True,
).split()
if history != [commit, baseline]:
    raise SystemExit("runner commit is not the single expected child of the refresh baseline")

message = subprocess.check_output(["git", "show", "-s", "--format=%B", commit], text=True)
def exact_trailer(prefix: str, expected: str, pattern: str | None = None) -> None:
    values = [line[len(prefix):] for line in message.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or values[0] != expected or (pattern and re.fullmatch(pattern, values[0]) is None):
        raise SystemExit(f"runner commit {prefix[:-2].lower()} attribution is missing, conflicting, or ambiguous")

exact_trailer("Refresh-Run-ID: ", run_id, r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
exact_trailer("Refresh-Runner-ID: ", runner_id, r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
exact_trailer("Refresh-Run-Scope: ", run_scope)

raw = subprocess.check_output(
    ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit]
)
changed = [os.fsdecode(item) for item in raw.split(b"\0") if item]
allowed_exact = {
    "README.md",
    "index.html",
    "archive/data/identity/wallet_profiles.json",
    "archive/dogs/manifest.json",
}
allowed_patterns = (
    re.compile(r"^(generated|public/generated)/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^archive/mission3/data/generated/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^public/generated/mission3/[A-Za-z0-9_]+\.json$"),
    re.compile(r"^archive/data/generated/unified_dog_search_[A-Za-z0-9_]+\.json$"),
    re.compile(r"^archive/dogs/by-id/[0-9]+\.json$"),
    re.compile(r"^archive/prices/data/generated/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^archive/prices/data/raw/[A-Za-z0-9_-]+\.json$"),
)
unexpected = [
    path
    for path in changed
    if path not in allowed_exact and not any(pattern.fullmatch(path) for pattern in allowed_patterns)
]
if not changed or unexpected:
    raise SystemExit(
        "runner commit changed no publish artifacts"
        if not changed
        else "runner commit changed paths outside the exact publish allowlist: " + ", ".join(unexpected)
    )
PY
}

runner_commit_run_id() {
  local commit="$1"
  python3 - "$commit" <<'PY'
from __future__ import annotations

import re
import subprocess
import sys

message = subprocess.check_output(["git", "show", "-s", "--format=%B", sys.argv[1]], text=True)
prefix = "Refresh-Run-ID: "
values = [line[len(prefix):] for line in message.splitlines() if line.startswith(prefix)]
if len(values) != 1 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", values[0]) is None:
    raise SystemExit(1)
print(values[0])
PY
}

runner_commit_runner_id() {
  local commit="$1"
  python3 - "$commit" <<'PY'
from __future__ import annotations

import re
import subprocess
import sys

message = subprocess.check_output(["git", "show", "-s", "--format=%B", sys.argv[1]], text=True)
prefix = "Refresh-Runner-ID: "
values = [line[len(prefix):] for line in message.splitlines() if line.startswith(prefix)]
if len(values) != 1 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", values[0]) is None:
    raise SystemExit(1)
print(values[0])
PY
}

runner_commit_scope() {
  local commit="$1"
  python3 - "$commit" <<'PY'
from __future__ import annotations

import subprocess
import sys

message = subprocess.check_output(["git", "show", "-s", "--format=%B", sys.argv[1]], text=True)
prefix = "Refresh-Run-Scope: "
values = [line[len(prefix):] for line in message.splitlines() if line.startswith(prefix)]
if len(values) != 1 or values[0] not in {"current", "full", "archive", "archive_full"}:
    raise SystemExit(1)
print(values[0])
PY
}

remote_head_is_valid_runner_commit() {
  local baseline="$1"
  local remote_head="$2"
  local record=""
  local parent=""
  local run_id=""
  local runner_id=""
  local run_scope=""

  git merge-base --is-ancestor "$baseline" "$remote_head" || return 1
  record="$(git rev-list --parents -n 1 "$remote_head")" || return 1
  # Only the current remote HEAD may authorize a no-op success. A historical
  # runner commit followed by an arbitrary commit must be aligned and rebuilt.
  [[ "$record" =~ ^[0-9a-f]{40}[[:space:]]([0-9a-f]{40})$ ]] || return 1
  parent="${BASH_REMATCH[1]}"
  [[ "$parent" == "$baseline" ]] || return 1
  run_id="$(runner_commit_run_id "$remote_head" 2>/dev/null)" || return 1
  runner_id="$(runner_commit_runner_id "$remote_head" 2>/dev/null)" || return 1
  run_scope="$(runner_commit_scope "$remote_head" 2>/dev/null)" || return 1
  [[ -n "$runner_id" ]] || return 1
  [[ -n "$run_scope" ]] || return 1
  validate_runner_commit "$remote_head" "$parent" "$run_id" "$runner_id" "$run_scope" >/dev/null 2>&1
}

validate_committed_refresh_snapshot() {
  local commit="$1"
  export RECOVERY_STATE_DIR REPO_DIR
  python3 - "$commit" "${REPO_DIR}/scripts/refresh_telemetry.py" <<'PY'
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

commit, telemetry_name = sys.argv[1:]
status = json.loads(
    subprocess.check_output(
        ["git", "show", f"{commit}:generated/refresh_status.json"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
)
if not isinstance(status, dict):
    raise ValueError("refresh status is not an object")
live_bundle = status.get("live_snapshot_bundle")
if not isinstance(live_bundle, str) or re.fullmatch(
    r"live_snapshot_[1-9][0-9]*_[0-9a-f]{64}_[0-9a-f]{64}\.json",
    live_bundle,
) is None:
    raise ValueError("refresh status has an unsafe live snapshot bundle")
paths = (
    "generated/current_auction.json",
    "public/generated/current_auction.json",
    "generated/auction_feed.json",
    "public/generated/auction_feed.json",
    "generated/current_auction_bid_history.json",
    "public/generated/current_auction_bid_history.json",
    "generated/mission3_metrics.json",
    "public/generated/mission3_metrics.json",
    "generated/refresh_status.json",
    "public/generated/refresh_status.json",
    f"generated/{live_bundle}",
    f"public/generated/{live_bundle}",
    "public/generated/unified_dog_search_index.json",
)
try:
    with tempfile.TemporaryDirectory(
        prefix="peer-snapshot-validation.",
        dir=os.environ["RECOVERY_STATE_DIR"],
    ) as temporary_name:
        root = Path(temporary_name)
        for relative in paths:
            payload = subprocess.check_output(
                ["git", "show", f"{commit}:{relative}"],
                stderr=subprocess.DEVNULL,
            )
            destination = root / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.write_bytes(payload)
        subprocess.run(
            [sys.executable, telemetry_name, "--root", str(root), "validate-status"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError):
    raise SystemExit(1)
PY
}

remote_snapshot_supersedes_local_commit() {
  local local_commit="$1"
  local remote_head="$2"
  local local_scope=""
  local remote_scope=""
  local_scope="$(runner_commit_scope "$local_commit" 2>/dev/null)" || return 1
  remote_scope="$(runner_commit_scope "$remote_head" 2>/dev/null)" || return 1
  # Archive/full reconciliation can carry offchain or historical deltas that a
  # newer current-auction block does not prove. Always rerun those scopes after
  # alignment; only an effectively bounded current request is eligible for a
  # peer no-op, even when the interrupted commit itself was current-scoped.
  [[ "$RUN_SCOPE" == "current" ]] || return 1
  [[ "$local_scope" == "current" ]] || return 1
  [[ -n "$remote_scope" ]] || return 1
  validate_committed_refresh_snapshot "$local_commit" || return 1
  validate_committed_refresh_snapshot "$remote_head" || return 1
  python3 - "$local_commit" "$remote_head" <<'PY'
from __future__ import annotations

import json
import subprocess
import sys


def load_status(commit: str, relative: str) -> dict[str, object]:
    raw = subprocess.check_output(
        ["git", "show", f"{commit}:{relative}"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("refresh status is not an object")
    return value


try:
    local = load_status(sys.argv[1], "generated/refresh_status.json")
    local_public = load_status(sys.argv[1], "public/generated/refresh_status.json")
    remote = load_status(sys.argv[2], "generated/refresh_status.json")
    remote_public = load_status(sys.argv[2], "public/generated/refresh_status.json")
    local_block = int(local.get("latest_generated_block") or 0)
    remote_block = int(remote.get("latest_generated_block") or 0)
except (KeyError, TypeError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError):
    raise SystemExit(1)

if (
    local_block <= 0
    or remote_block < local_block
    or local != local_public
    or remote != remote_public
):
    raise SystemExit(1)

if remote_block == local_block:
    exact_keys = (
        "snapshot_block_hash",
        "current_dog_token_id",
        "current_bid_eth",
        "current_high_bidder_wallet",
        "current_auction_status",
        "current_auction_end_time_utc",
        "live_snapshot_bundle_sha256",
        "unified_dog_search_sha256",
    )
    if any(local.get(key) != remote.get(key) for key in exact_keys):
        raise SystemExit(1)

raise SystemExit(0)
PY
}

cleanup_partial_generation() {
  local preserve_journal="${1:-0}"
  local tracked_changes=""
  local path=""
  if [[ "$preserve_journal" != "0" && "$preserve_journal" != "1" ]]; then
    log "warning: invalid cleanup journal-preservation mode"
    return 1
  fi
  if [[ "$MUTATION_STARTED" != "1" || -z "$BASELINE_HEAD" ]]; then
    return 0
  fi
  if ! clear_stale_git_index_lock "rollback"; then
    log "warning: refusing partial-generation rollback while the git index lock is active or unsafe"
    return 1
  fi
  if [[ -n "$RUNNER_COMMIT_HEAD" ]]; then
    local current_head=""
    local runner_parent=""
    current_head="$(git rev-parse HEAD)" || {
      log "warning: could not resolve HEAD while rolling back runner commit"
      return 1
    }
    runner_parent="$(git rev-parse "${RUNNER_COMMIT_HEAD}^")" || {
      log "warning: could not resolve parent of runner commit ${RUNNER_COMMIT_HEAD}"
      return 1
    }
    if [[ "$current_head" != "$RUNNER_COMMIT_HEAD" || "$runner_parent" != "$BASELINE_HEAD" ]]; then
      log "warning: refusing to rewind an unauthenticated local commit (head=${current_head} runner=${RUNNER_COMMIT_HEAD} parent=${runner_parent} baseline=${BASELINE_HEAD})"
      return 1
    fi
    if [[ -z "$RUNNER_COMMIT_RUN_ID" ]] || \
      ! validate_runner_commit "$RUNNER_COMMIT_HEAD" "$BASELINE_HEAD" "$RUNNER_COMMIT_RUN_ID" "$RUNNER_COMMIT_RUNNER_ID" "$RUNNER_COMMIT_SCOPE"; then
      log "warning: refusing to rewind runner commit with an unsafe path set"
      return 1
    fi
    git update-ref "refs/heads/${BRANCH}" "$BASELINE_HEAD" "$RUNNER_COMMIT_HEAD" || {
      log "warning: compare-and-swap rewind failed for runner commit ${RUNNER_COMMIT_HEAD}"
      return 1
    }
    log "rewound unpushed runner commit ${RUNNER_COMMIT_HEAD} to ${BASELINE_HEAD}"
    RUNNER_COMMIT_HEAD=""
    RUNNER_COMMIT_RUN_ID=""
    RUNNER_COMMIT_RUNNER_ID=""
    RUNNER_COMMIT_SCOPE=""
  fi

  log "rolling back partial generated artifacts to ${BASELINE_HEAD}"
  tracked_changes="$(git diff --name-only "$BASELINE_HEAD" -- "${PUBLISH_PATHS[@]}")" || {
    log "warning: could not enumerate partial tracked generated artifacts"
    return 1
  }
  if [[ -n "$tracked_changes" ]]; then
    while IFS= read -r path; do
      [[ -n "$path" ]] || continue
      git restore --source="$BASELINE_HEAD" --staged --worktree -- "$path" || {
        log "warning: git restore could not roll back ${path}"
        return 1
      }
    done <<< "$tracked_changes"
  fi
  # Preserve rather than delete untracked artifacts. Preflight rejects any that
  # existed before this run, but a same-user process could still create a file
  # concurrently; quarantine keeps recovery possible without poisoning the
  # next scheduled run.
  QUARANTINE_DIR=""
  export REPO_DIR
  if ! QUARANTINE_DIR="$(python3 - "$RECOVERY_STATE_DIR" "$DEGEN_DOGS_REFRESH_RUN_ID" "${PUBLISH_PATHS[@]}" <<'PY'
from __future__ import annotations

import errno
import os
import secrets
import stat as stat_module
import subprocess
import sys
from pathlib import Path

repo = Path(os.path.abspath(os.path.expanduser(os.environ["REPO_DIR"])))
state_root = Path(os.path.abspath(os.path.expanduser(sys.argv[1])))
run_id = sys.argv[2]
paths = sys.argv[3:]
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def open_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise SystemExit(f"directory path is not absolute: {path}")
    descriptor = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def require_private_directory(descriptor: int, display: Path) -> None:
    details = os.fstat(descriptor)
    if (
        not stat_module.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or (details.st_mode & 0o777) != 0o700
    ):
        raise SystemExit(f"unsafe recovery directory: {display}")


def open_private_child(parent_fd: int, name: str, display: Path, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(name, directory_flags, dir_fd=parent_fd)
    except FileNotFoundError:
        raise SystemExit(f"missing authenticated recovery directory: {display}") from None
    try:
        require_private_directory(descriptor, display)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_relative_directory(
    base_fd: int,
    parts: tuple[str, ...],
    display_root: Path,
    *,
    create_private: bool,
) -> int:
    descriptor = os.dup(base_fd)
    cursor = display_root
    try:
        for part in parts:
            cursor /= part
            if create_private:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if create_private:
                require_private_directory(descriptor, cursor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


repo_fd = open_absolute_directory(repo)
state_root_fd = open_absolute_directory(state_root)
recovery_root_fd = -1
recovery_fd = -1
try:
    # The state root was authenticated at startup. Do not traverse any recovery
    # or repository component through a symlink between validation and use.
    require_private_directory(state_root_fd, state_root)
    recovery_root = state_root / "recovery"
    recovery_root_fd = open_private_child(
        state_root_fd,
        "recovery",
        recovery_root,
        create=True,
    )
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths],
        cwd=repo,
    )
    untracked = [
        entry[3:]
        for entry in status.split(b"\0")
        if entry.startswith(b"?? ") and entry[3:]
    ]
    if not untracked:
        raise SystemExit(0)

    # Never reuse a run-id path. Create the quarantine directory relative to
    # the already-open recovery root so a renamed path cannot redirect it.
    for _ in range(128):
        recovery_name = f"{run_id}.{secrets.token_hex(8)}"
        try:
            os.mkdir(recovery_name, mode=0o700, dir_fd=recovery_root_fd)
        except FileExistsError:
            continue
        break
    else:
        raise SystemExit("could not allocate a unique recovery quarantine")
    recovery = recovery_root / recovery_name
    recovery_fd = open_private_child(
        recovery_root_fd,
        recovery_name,
        recovery,
        create=False,
    )

    for raw in untracked:
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SystemExit(f"refusing to quarantine unsafe repository path: {relative}")
        source_parent_fd = open_relative_directory(
            repo_fd,
            tuple(relative.parts[:-1]),
            repo,
            create_private=False,
        )
        try:
            source_name = relative.parts[-1]
            try:
                os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            destination_parent_fd = open_relative_directory(
                recovery_fd,
                tuple(relative.parts[:-1]),
                recovery,
                create_private=True,
            )
            try:
                destination_name = relative.parts[-1]
                try:
                    os.stat(
                        destination_name,
                        dir_fd=destination_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise SystemExit(
                        f"refusing to overwrite quarantine destination: {recovery / relative}"
                    )
                try:
                    os.rename(
                        source_name,
                        destination_name,
                        src_dir_fd=source_parent_fd,
                        dst_dir_fd=destination_parent_fd,
                    )
                except OSError as exc:
                    if exc.errno == errno.EXDEV:
                        raise SystemExit(
                            f"refusing cross-device quarantine move for {relative}"
                        ) from None
                    raise
                print(f"quarantined untracked artifact: {relative}", file=sys.stderr)
            finally:
                os.close(destination_parent_fd)
        finally:
            os.close(source_parent_fd)
    print(recovery)
finally:
    if recovery_fd >= 0:
        os.close(recovery_fd)
    if recovery_root_fd >= 0:
        os.close(recovery_root_fd)
    os.close(state_root_fd)
    os.close(repo_fd)
PY
  )"; then
    log "warning: could not quarantine runner-created untracked artifacts"
    return 1
  fi
  if [[ -d "$QUARANTINE_DIR" ]]; then
    log "untracked artifacts preserved under ${QUARANTINE_DIR}"
  fi
  if [[ -n "$(git status --porcelain --untracked-files=all -- "${PUBLISH_PATHS[@]}")" ]]; then
    log "warning: generated artifact paths remain dirty after rollback"
    git status --short --untracked-files=all -- "${PUBLISH_PATHS[@]}"
    return 1
  fi
  if [[ "$preserve_journal" != "1" ]]; then
    remove_recovery_journal || {
      log "warning: could not remove the authenticated publisher recovery journal"
      return 1
    }
    MUTATION_STARTED=0
  fi
  log "partial generated artifacts rolled back"
}

align_local_runner_commit_to_remote() {
  local remote_head="$1"
  local context="$2"
  local peer_snapshot_covers="$3"
  local alignment_result="regenerate"
  local current_head=""

  current_head="$(git rev-parse HEAD)" || return 1
  if [[ -z "$RUNNER_COMMIT_HEAD" || -z "$RUNNER_COMMIT_RUN_ID" || \
    "$current_head" != "$RUNNER_COMMIT_HEAD" ]]; then
    log "warning: refusing ${context} alignment without the authenticated local runner commit"
    return 1
  fi
  validate_runner_commit "$RUNNER_COMMIT_HEAD" "$BASELINE_HEAD" "$RUNNER_COMMIT_RUN_ID" "$RUNNER_COMMIT_RUNNER_ID" "$RUNNER_COMMIT_SCOPE" || {
    log "warning: refusing ${context} alignment for an invalid local runner commit"
    return 1
  }
  if ! git merge-base --is-ancestor "$BASELINE_HEAD" "$remote_head"; then
    log "warning: refusing ${context} alignment because remote ${remote_head} is not a descendant of baseline ${BASELINE_HEAD}"
    return 1
  fi

  if [[ "$peer_snapshot_covers" == "1" ]]; then
    alignment_result="peer_supersedes"
  fi
  write_recovery_alignment \
    "$RUNNER_COMMIT_HEAD" "$remote_head" "$alignment_result" || {
    log "warning: could not persist crash-safe ${context} alignment state"
    return 1
  }
  # Keep the journal armed while HEAD crosses from the losing child through
  # its baseline to the remote descendant. Recovery understands every state.
  cleanup_partial_generation 1 || return 1
  git merge --ff-only "$remote_head" || {
    log "warning: could not fast-forward the clean baseline to competing remote ${remote_head}"
    return 1
  }
  if [[ "$(git rev-parse HEAD)" != "$remote_head" ]] || \
    [[ -n "$(git status --porcelain --untracked-files=all -- "${PUBLISH_PATHS[@]}")" ]]; then
    MUTATION_STARTED=0
    log "warning: remote supersession alignment did not leave the expected clean publish snapshot"
    return 1
  fi
  if ! remove_recovery_journal; then
    # HEAD now names the exact journaled remote target. Preserve both so the
    # next invocation can finish the alignment without guessing provenance.
    MUTATION_STARTED=0
    log "warning: aligned remote snapshot but could not clear its recovery journal"
    return 1
  fi
  BASELINE_HEAD=""
  RUNNER_COMMIT_HEAD=""
  RUNNER_COMMIT_RUN_ID=""
  RUNNER_COMMIT_RUNNER_ID=""
  RUNNER_COMMIT_SCOPE=""
  MUTATION_STARTED=0
  log "aligned local ${BRANCH} to competing remote descendant ${remote_head} during ${context}"
}

reconcile_remote_descendant() {
  local remote_head="$1"
  local context="$2"
  local peer_snapshot_covers=0

  if remote_head_is_valid_runner_commit "$BASELINE_HEAD" "$remote_head" && \
    remote_snapshot_supersedes_local_commit "$RUNNER_COMMIT_HEAD" "$remote_head"; then
    peer_snapshot_covers=1
  fi
  align_local_runner_commit_to_remote "$remote_head" "$context" "$peer_snapshot_covers" || return 1
  if [[ "$peer_snapshot_covers" == "1" ]]; then
    return 0
  fi
  return 75
}

complete_peer_supersession() {
  local remote_head="$1"
  local context="$2"
  export DEGEN_DOGS_REFRESH_RESULT="success_superseded_by_peer"
  export DEGEN_DOGS_COMMIT_SHA="$remote_head"
  unset DEGEN_DOGS_PUSH_COMPLETED_AT_UTC
  log "peer publisher already committed an equal-or-newer verified snapshot at ${remote_head}; ${context} completed by supersession"
  exit 0
}

restart_after_remote_advance() {
  local remote_head="$1"
  local context="$2"
  if (( SUPERSESSION_RETRY_COUNT < 1 )); then
    export DEGEN_DOGS_SUPERSESSION_RETRY_COUNT="$((SUPERSESSION_RETRY_COUNT + 1))"
    unset \
      DEGEN_DOGS_COMMIT_SHA \
      DEGEN_DOGS_COMMIT_STARTED_AT_UTC \
      DEGEN_DOGS_COMMIT_COMPLETED_AT_UTC \
      DEGEN_DOGS_PUSH_STARTED_AT_UTC \
      DEGEN_DOGS_PUSH_COMPLETED_AT_UTC \
      DEGEN_DOGS_LIVE_VERIFY_STARTED_AT_UTC \
      DEGEN_DOGS_LIVE_VERIFIED_AT_UTC \
      DEGEN_DOGS_LIVE_VERIFY_RESULT \
      DEGEN_DOGS_RAW_COMMIT_VERIFIED \
      DEGEN_DOGS_LIVE_VERIFY_ERROR
    log "remote advanced to ${remote_head} without a provably covering peer snapshot; restarting once from the new baseline after ${context}"
    exec "$0" "${ORIGINAL_ARGS[@]}"
  fi
  fail "remote advanced again during the bounded supersession retry (${context}); local branch is safely aligned and a later watcher run will retry"
}

handle_remote_advance_after_local_commit() {
  local remote_head="$1"
  local context="$2"
  local reconcile_status=0

  if ! git merge-base --is-ancestor "$BASELINE_HEAD" "$remote_head"; then
    MUTATION_STARTED=0
    fail "remote changed non-linearly after ${context}; preserving authenticated recovery journal"
  fi
  if reconcile_remote_descendant "$remote_head" "$context"; then
    complete_peer_supersession "$remote_head" "$context"
  else
    reconcile_status=$?
  fi
  if [[ "$reconcile_status" == "75" ]]; then
    restart_after_remote_advance "$remote_head" "$context"
  fi
  fail "could not safely reconcile competing remote descendant ${remote_head} after ${context}"
}

push_with_compare_and_swap() {
  local attempt=1
  local push_status=1
  local delay=0
  local remote_head=""
  local lease="refs/heads/${BRANCH}:${BASELINE_HEAD}"

  while (( attempt <= GIT_RETRY_ATTEMPTS )); do
    log "git push compare-and-swap attempt=${attempt}/${GIT_RETRY_ATTEMPTS} baseline=${BASELINE_HEAD}"
    if git push --porcelain --force-with-lease="$lease" "$REMOTE" "${RUNNER_COMMIT_HEAD}:refs/heads/${BRANCH}"; then
      return 0
    else
      push_status=$?
    fi

    # Every failed push is ambiguous until a fresh fetch classifies the remote.
    # Never blindly retry a rejected non-fast-forward update from the same base.
    if ! run_with_retry "git fetch after failed compare-and-swap push" git fetch "$REMOTE" "$BRANCH"; then
      MUTATION_STARTED=0
      fail "compare-and-swap push failed and remote state could not be reconciled; preserving authenticated recovery journal"
    fi
    remote_head="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}")" || {
      MUTATION_STARTED=0
      fail "compare-and-swap push failed and reconciled remote ref could not be resolved"
    }
    if [[ "$remote_head" == "$RUNNER_COMMIT_HEAD" ]]; then
      log "push command reported failure, but immutable publisher commit is confirmed on the remote"
      return 0
    fi
    if [[ "$remote_head" != "$BASELINE_HEAD" ]]; then
      PUSH_REMOTE_HEAD="$remote_head"
      return 75
    fi
    if (( attempt == GIT_RETRY_ATTEMPTS )); then
      log "git push compare-and-swap failed after ${attempt} attempts status=${push_status}; remote remains at baseline"
      return "$push_status"
    fi
    delay="$(retry_delay_seconds "$attempt")"
    log "git push compare-and-swap retrying in ${delay}s after status=${push_status}; remote still equals baseline"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
  return "$push_status"
}

portable_stat_value() {
  local bsd_format="$1"
  local gnu_format="$2"
  local path="$3"
  if stat -f "$bsd_format" "$path" >/dev/null 2>&1; then
    stat -f "$bsd_format" "$path"
  else
    stat -c "$gnu_format" "$path"
  fi
}

git_index_lock_is_stale() {
  local lock_path="$1"
  local lock_mtime
  local lock_mode
  local lock_owner
  local lock_links
  local lsof_output
  local lsof_status
  local now

  [[ -f "$lock_path" && ! -L "$lock_path" ]] || return 1
  lock_owner="$(portable_stat_value %u %u "$lock_path" 2>/dev/null || printf '%s' '-1')"
  lock_links="$(portable_stat_value %l %h "$lock_path" 2>/dev/null || printf '0')"
  lock_mode="$(portable_stat_value %Lp %a "$lock_path" 2>/dev/null || printf '0')"
  # Git honors the invoking process umask when creating index.lock, so an
  # otherwise safe crash artifact may be 0600 (runner) or 0644 (interactive
  # Git). Never accept executable or group/other-writable lock files.
  [[ "$lock_owner" == "$(id -u)" && "$lock_links" == "1" && "$lock_mode" =~ ^6(00|44)$ ]] || return 1
  command -v lsof >/dev/null 2>&1 || return 1
  if lsof_output="$(lsof "$lock_path" 2>&1)"; then
    return 1
  else
    lsof_status=$?
  fi
  # lsof uses status 1 with no output for an explicit no-match. Any diagnostic
  # or different status is an inspection failure, not proof that the file is idle.
  [[ "$lsof_status" == "1" && -z "$lsof_output" ]] || return 1
  lock_mtime="$(portable_stat_value %m %Y "$lock_path" 2>/dev/null || printf '0')"
  now="$(date +%s)"
  [[ "$lock_mtime" =~ ^[0-9]+$ ]] || return 1
  (( now - lock_mtime >= 60 ))
}

git_index_lock_path() {
  local lock_path
  lock_path="$(git rev-parse --git-path index.lock)" || return 1
  if [[ "$lock_path" != /* ]]; then
    lock_path="${REPO_DIR}/${lock_path}"
  fi
  printf '%s\n' "$lock_path"
}

clear_stale_git_index_lock() {
  local context="$1"
  local lock_path
  lock_path="$(git_index_lock_path)" || return 1
  [[ -e "$lock_path" || -L "$lock_path" ]] || return 0
  if ! git_index_lock_is_stale "$lock_path"; then
    log "git index lock is active, recent, or unsafe during ${context}; refusing automatic removal"
    return 1
  fi
  rm -- "$lock_path" || return 1
  log "removed proven-stale git index lock before ${context}"
}

recover_interrupted_generation() {
  local journal_record
  local journal_baseline
  local journal_run_id
  local journal_runner_id
  local journal_run_scope
  local alignment_runner_commit
  local alignment_remote_head
  local alignment_result
  local journaled_alignment_head
  local latest_alignment_result
  local current_branch
  local current_head
  local current_run_scope
  local remote_head
  local reconcile_status=0

  [[ -e "$RECOVERY_JOURNAL" || -L "$RECOVERY_JOURNAL" ]] || return 0
  journal_record="$(read_recovery_journal_baseline)" || fail "publisher recovery journal could not be authenticated"
  IFS=$'\t' read -r journal_baseline journal_run_id journal_runner_id journal_run_scope alignment_runner_commit \
    alignment_remote_head alignment_result <<<"$journal_record"
  RUNNER_COMMIT_RUNNER_ID="$journal_runner_id"
  RUNNER_COMMIT_SCOPE="$journal_run_scope"
  current_branch="$(git branch --show-current)" || fail "could not resolve branch during interrupted-run recovery"
  current_head="$(git rev-parse HEAD)" || fail "could not resolve HEAD during interrupted-run recovery"
  if [[ "$current_branch" != "$BRANCH" ]]; then
    fail "authenticated publisher recovery journal belongs to ${BRANCH}, but worktree is on ${current_branch:-detached}"
  fi

  # Recovery must never downgrade either the caller's requested work or the
  # interrupted job's journaled work.  Union the two dimensions, then derive
  # the canonical trailer scope exactly once.
  if [[ "$journal_run_scope" == "archive" || "$journal_run_scope" == "archive_full" ]]; then
    RUN_MISSION3_ARCHIVE=1
  fi
  if [[ "$journal_run_scope" == "full" || "$journal_run_scope" == "archive_full" ]]; then
    FULL_REFRESH=1
  fi
  RUN_SCOPE="$(derive_run_scope)"
  export DEGEN_DOGS_RUN_MISSION3_ARCHIVE="$RUN_MISSION3_ARCHIVE"
  export DEGEN_DOGS_FULL_REFRESH="$FULL_REFRESH"
  export DEGEN_DOGS_RUN_SCOPE="$RUN_SCOPE"

  if [[ "$alignment_remote_head" != "-" ]]; then
    validate_runner_commit "$alignment_runner_commit" "$journal_baseline" "$journal_run_id" "$journal_runner_id" "$journal_run_scope" || \
      fail "publisher alignment journal no longer identifies its losing runner commit"
    current_run_scope="$(runner_commit_scope "$alignment_runner_commit" 2>/dev/null || true)"
    if [[ "$current_run_scope" != "$journal_run_scope" ]]; then
      fail "publisher alignment journal scope differs from its losing runner commit"
    fi
    run_with_retry "git fetch for interrupted remote alignment" git fetch "$REMOTE" "$BRANCH"
    remote_head="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}")" || \
      fail "could not resolve remote during interrupted alignment"
    journaled_alignment_head="$alignment_remote_head"
    git merge-base --is-ancestor "$journal_baseline" "$alignment_remote_head" || \
      fail "interrupted publisher alignment target is no longer a baseline descendant"
    git merge-base --is-ancestor "$journaled_alignment_head" "$remote_head" || \
      fail "remote changed non-linearly beyond the interrupted publisher alignment target"

    latest_alignment_result="regenerate"
    if remote_head_is_valid_runner_commit "$journal_baseline" "$remote_head" && \
      remote_snapshot_supersedes_local_commit "$alignment_runner_commit" "$remote_head"; then
      latest_alignment_result="peer_supersedes"
    fi
    if [[ "$remote_head" != "$journaled_alignment_head" || \
      "$alignment_result" != "$latest_alignment_result" ]]; then
      # Advance the private journal before moving HEAD again. A second crash can
      # therefore resume from any clean remote-line prefix through this target.
      BASELINE_HEAD="$journal_baseline"
      RUNNER_COMMIT_HEAD="$alignment_runner_commit"
      RUNNER_COMMIT_RUN_ID="$journal_run_id"
      write_recovery_alignment \
        "$alignment_runner_commit" "$remote_head" "$latest_alignment_result" || \
        fail "could not advance interrupted publisher alignment journal to latest remote"
      BASELINE_HEAD=""
      RUNNER_COMMIT_HEAD=""
      RUNNER_COMMIT_RUN_ID=""
      alignment_remote_head="$remote_head"
      alignment_result="$latest_alignment_result"
      log "advanced interrupted publisher alignment target from ${journaled_alignment_head} to ${remote_head}"
    fi

    if [[ "$current_head" == "$alignment_runner_commit" ]]; then
      # The process stopped before the journaled rewind. The normal local-child
      # path below can repeat reconciliation against the latest remote safely.
      :
    elif git merge-base --is-ancestor "$journal_baseline" "$current_head" && \
      git merge-base --is-ancestor "$current_head" "$alignment_remote_head"; then
      BASELINE_HEAD="$journal_baseline"
      MUTATION_STARTED=1
      if [[ "$current_head" == "$journal_baseline" ]]; then
        cleanup_partial_generation 1 || \
          fail "could not finish the journaled baseline cleanup before remote alignment"
      elif [[ -n "$(git status --porcelain --untracked-files=all -- "${PUBLISH_PATHS[@]}")" ]]; then
        MUTATION_STARTED=0
        fail "interrupted publisher alignment prefix has dirty publish artifacts"
      fi
      if [[ "$current_head" != "$alignment_remote_head" ]]; then
        if ! git merge --ff-only "$alignment_remote_head"; then
          MUTATION_STARTED=0
          fail "could not resume interrupted fast-forward to latest competing remote"
        fi
      fi
      current_head="$(git rev-parse HEAD)" || fail "could not resolve resumed alignment HEAD"
    else
      fail "worktree HEAD does not match any crash-safe publisher alignment phase"
    fi

    if [[ "$current_head" == "$alignment_remote_head" ]]; then
      if [[ -n "$(git status --porcelain --untracked-files=all -- "${PUBLISH_PATHS[@]}")" ]]; then
        MUTATION_STARTED=0
        fail "interrupted publisher alignment reached remote HEAD with dirty publish artifacts"
      fi
      if ! remove_recovery_journal; then
        MUTATION_STARTED=0
        fail "could not clear completed remote-alignment recovery journal"
      fi
      BASELINE_HEAD=""
      RUNNER_COMMIT_HEAD=""
      RUNNER_COMMIT_RUN_ID=""
      RUNNER_COMMIT_RUNNER_ID=""
      RUNNER_COMMIT_SCOPE=""
      MUTATION_STARTED=0
      if [[ "$alignment_result" == "peer_supersedes" ]]; then
        RECOVERY_SUPERSEDED=1
      fi
      log "completed interrupted publisher alignment to remote ${alignment_remote_head}"
      return 0
    fi
  fi

  if [[ "$current_head" == "$journal_baseline" ]]; then
    log "recovering interrupted generated artifacts from baseline ${journal_baseline}"
  else
    validate_runner_commit "$current_head" "$journal_baseline" "$journal_run_id" "$journal_runner_id" "$journal_run_scope" || \
      fail "refusing to attribute an unsafe or different commit to the interrupted publisher"
    current_run_scope="$(runner_commit_scope "$current_head" 2>/dev/null || true)"
    if [[ "$current_run_scope" != "$journal_run_scope" ]]; then
      fail "interrupted publisher commit scope differs from its recovery journal"
    fi

    run_with_retry "git fetch for interrupted publisher recovery" git fetch "$REMOTE" "$BRANCH"
    remote_head="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}")" || fail "could not resolve remote during interrupted publisher recovery"
    if [[ "$remote_head" == "$current_head" ]]; then
      if [[ -n "$(git status --porcelain --untracked-files=all -- "${PUBLISH_PATHS[@]}")" ]]; then
        fail "interrupted publisher commit reached the remote but publish artifacts are unexpectedly dirty"
      fi
      remove_recovery_journal || fail "could not clear landed publisher recovery journal"
      BASELINE_HEAD=""
      MUTATION_STARTED=0
      log "reconciled publisher commit ${current_head} that landed before the interruption"
      return 0
    fi
    if [[ "$remote_head" == "$journal_baseline" ]]; then
      log "recovering unpushed interrupted publisher commit ${current_head}"
    elif git merge-base --is-ancestor "$journal_baseline" "$remote_head"; then
      # Another writer may have won after this process pushed ambiguously or
      # crashed. Authenticate our local child first, then either acknowledge a
      # covering peer snapshot or fast-forward and regenerate on the new base.
      BASELINE_HEAD="$journal_baseline"
      RUNNER_COMMIT_HEAD="$current_head"
      RUNNER_COMMIT_RUN_ID="$journal_run_id"
      MUTATION_STARTED=1
      if reconcile_remote_descendant "$remote_head" "interrupted publisher recovery"; then
        RECOVERY_SUPERSEDED=1
        log "interrupted publisher was safely superseded by peer commit ${remote_head}"
        return 0
      else
        reconcile_status=$?
      fi
      if [[ "$reconcile_status" == "75" ]]; then
        log "interrupted publisher aligned to remote ${remote_head}; regenerating because peer coverage was not proven"
        return 0
      fi
      fail "could not safely align interrupted publisher state to remote descendant ${remote_head}"
    else
      fail "remote changed non-linearly across interrupted publisher recovery"
    fi
  fi

  # Arm EXIT rollback only after every provenance/history/remote check passes.
  # A refused or temporarily blocked recovery must leave HEAD, files, and the
  # journal byte-for-byte intact for the next safe retry.
  BASELINE_HEAD="$journal_baseline"
  if [[ "$current_head" != "$journal_baseline" ]]; then
    RUNNER_COMMIT_HEAD="$current_head"
    RUNNER_COMMIT_RUN_ID="$journal_run_id"
  fi
  MUTATION_STARTED=1
  cleanup_partial_generation || fail "authenticated interrupted publisher rollback did not complete"
  BASELINE_HEAD=""
  RUNNER_COMMIT_HEAD=""
  RUNNER_COMMIT_RUN_ID=""
  RUNNER_COMMIT_RUNNER_ID=""
  RUNNER_COMMIT_SCOPE=""
  log "interrupted publisher recovery completed"
}

commit_refresh_snapshot() {
  local commit_output
  local commit_status

  if commit_output="$(git commit \
    -m "$commit_message" \
    -m "Snapshot block: ${latest_block}" \
    -m "Current dog: ${current_dog}" \
    -m "Automated refresh from runner ${RUNNER_ID}." \
    -m "Refresh-Runner-ID: ${RUNNER_ID}" \
    -m "Refresh-Run-Scope: ${RUN_SCOPE}" \
    -m "Refresh-Run-ID: ${DEGEN_DOGS_REFRESH_RUN_ID}" 2>&1)"; then
    printf '%s\n' "$commit_output"
    return 0
  fi
  commit_status=$?
  printf '%s\n' "$commit_output"
  if [[ "$commit_status" != "128" || "$commit_output" != *"index.lock"* || "$commit_output" != *"File exists"* ]]; then
    return "$commit_status"
  fi
  if ! clear_stale_git_index_lock "commit retry"; then
    return "$commit_status"
  fi
  log "retrying commit after stale git index lock recovery"
  git commit \
    -m "$commit_message" \
    -m "Snapshot block: ${latest_block}" \
    -m "Current dog: ${current_dog}" \
    -m "Automated refresh from runner ${RUNNER_ID}." \
    -m "Refresh-Runner-ID: ${RUNNER_ID}" \
    -m "Refresh-Run-Scope: ${RUN_SCOPE}" \
    -m "Refresh-Run-ID: ${DEGEN_DOGS_REFRESH_RUN_ID}"
}

verify_live_deployment() {
  unset DEGEN_DOGS_LIVE_VERIFY_RESULT DEGEN_DOGS_RAW_COMMIT_VERIFIED DEGEN_DOGS_LIVE_VERIFY_ERROR
  if [[ "$LIVE_VERIFY_AFTER_PUSH" != "1" ]]; then
    return 0
  fi
  LIVE_VERIFY_ENV="$(mktemp -t degen-dogs-live-verify.XXXXXX)"
  if python3 scripts/refresh_telemetry.py verify-live --env-file "$LIVE_VERIFY_ENV"; then
    # shellcheck disable=SC1090
    source "$LIVE_VERIFY_ENV"
  else
    # shellcheck disable=SC1090
    source "$LIVE_VERIFY_ENV" || true
    if [[ "${DEGEN_DOGS_LIVE_VERIFY_RESULT:-}" == "timeout" && "${DEGEN_DOGS_RAW_COMMIT_VERIFIED:-}" == "True" ]]; then
      # The commit has already been pushed and immutable-commit verification is
      # recorded separately. A slow or wedged Pages deployment must not turn a
      # successful data publication into a rapid regenerate-and-repush storm.
      export DEGEN_DOGS_REFRESH_RESULT="success_pushed_live_timeout"
      log "warning: pushed snapshot is awaiting GitHub Pages after the live-verification timeout"
    else
      export DEGEN_DOGS_REFRESH_RESULT="failed_live_verify"
      fail "live verification failed before proving the immutable pushed snapshot"
    fi
  fi
  rm -f -- "$LIVE_VERIFY_ENV"
  LIVE_VERIFY_ENV=""
}

validate_name() {
  local name="$1"
  local value="$2"
  if [[ -z "$value" || "$value" == -* || "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *$'\t'* || "$value" == *' '* ]]; then
    fail "invalid ${name}: ${value}"
  fi
}

validate_name "remote" "$REMOTE"
validate_name "branch" "$BRANCH"
if [[ ! "$RUNNER_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  fail "invalid DEGEN_DOGS_RUNNER_ID"
fi
if [[ ! "$DEGEN_DOGS_REFRESH_RUN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
  fail "invalid refresh run ID"
fi
validate_nonnegative_integer "DEGEN_DOGS_SUPERSESSION_RETRY_COUNT" "$SUPERSESSION_RETRY_COUNT"
if (( SUPERSESSION_RETRY_COUNT > 1 )); then
  fail "DEGEN_DOGS_SUPERSESSION_RETRY_COUNT must be 0 or 1"
fi
validate_nonnegative_integer "DEGEN_DOGS_GIT_RETRY_ATTEMPTS" "$GIT_RETRY_ATTEMPTS"
validate_nonnegative_integer "DEGEN_DOGS_GIT_RETRY_BASE_SECONDS" "$GIT_RETRY_BASE_SECONDS"
validate_nonnegative_integer "DEGEN_DOGS_GIT_RETRY_MAX_SECONDS" "$GIT_RETRY_MAX_SECONDS"
validate_nonnegative_integer "DEGEN_DOGS_GIT_RETRY_JITTER_SECONDS" "$GIT_RETRY_JITTER_SECONDS"
if (( GIT_RETRY_ATTEMPTS < 1 || GIT_RETRY_ATTEMPTS > 10 )); then
  fail "DEGEN_DOGS_GIT_RETRY_ATTEMPTS must be between 1 and 10"
fi
if (( GIT_RETRY_MAX_SECONDS < GIT_RETRY_BASE_SECONDS )); then
  fail "DEGEN_DOGS_GIT_RETRY_MAX_SECONDS must be >= DEGEN_DOGS_GIT_RETRY_BASE_SECONDS"
fi

if [[ "${DEGEN_DOGS_LOCK_HELD:-0}" != "1" ]]; then
  export DEGEN_DOGS_REFRESH_LOCK_PATH="$REFRESH_LOCK_PATH"
  exec python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$0" "$@" <<'PY'
from __future__ import annotations

import fcntl
import os
import sys
from datetime import datetime, timezone

helper_dir = sys.argv[1]
script = os.path.abspath(sys.argv[2])
args = sys.argv[3:]
lock_path = os.path.expanduser(os.environ["DEGEN_DOGS_REFRESH_LOCK_PATH"])
sys.path.insert(0, helper_dir)
from runner_path_security import SecurePathError, open_private_lock

try:
    fd = open_private_lock(lock_path)
except (OSError, SecurePathError) as exc:
    raise SystemExit(f"refusing unsafe refresh lock path: {exc}") from exc
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("another refresh is already running; exiting")
    sys.exit(0)
os.ftruncate(fd, 0)
os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
os.set_inheritable(fd, True)
env = os.environ.copy()
env["DEGEN_DOGS_LOCK_HELD"] = "1"
env["DEGEN_DOGS_LOCK_FD"] = str(fd)
env["DEGEN_DOGS_LOCK_ACQUIRED_AT_UTC"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
os.execvpe(script, [script, *args], env)
PY
else
  [[ "${DEGEN_DOGS_LOCK_FD:-}" =~ ^[0-9]+$ ]] || fail "DEGEN_DOGS_LOCK_HELD requires an inherited lock descriptor"
  export DEGEN_DOGS_REFRESH_LOCK_PATH="$REFRESH_LOCK_PATH"
  python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$DEGEN_DOGS_LOCK_FD" "$REFRESH_LOCK_PATH" <<'PY'
from __future__ import annotations

import fcntl
import os
import stat
import sys

helper_dir = sys.argv[1]
fd = int(sys.argv[2])
path = os.path.expanduser(sys.argv[3])
sys.path.insert(0, helper_dir)
from runner_path_security import SecurePathError, open_existing_private_file

try:
    descriptor = os.fstat(fd)
    path_fd = open_existing_private_file(path, writable=True)
    try:
        target = os.fstat(path_fd)
    finally:
        os.close(path_fd)
except (OSError, SecurePathError, ValueError) as exc:
    raise SystemExit(f"invalid inherited refresh lock descriptor: {exc}") from exc
if not stat.S_ISREG(descriptor.st_mode) or descriptor.st_uid != os.getuid():
    raise SystemExit("inherited refresh lock descriptor is not an owned regular file")
if (descriptor.st_dev, descriptor.st_ino) != (target.st_dev, target.st_ino):
    raise SystemExit("inherited refresh lock descriptor does not match configured lock path")
if stat.S_IMODE(descriptor.st_mode) & 0o077:
    raise SystemExit("inherited refresh lock file permissions are too broad")
try:
    # On the inherited open-file description this preserves the existing lock;
    # on an unlocked descriptor it atomically acquires it before mutation.
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as exc:
    raise SystemExit("inherited refresh lock descriptor does not own the lock") from exc
PY
fi

export DEGEN_DOGS_REFRESH_STARTED_AT_UTC="${DEGEN_DOGS_REFRESH_STARTED_AT_UTC:-$(utc_stamp)}"

finish() {
  local status=$?
  local result="${DEGEN_DOGS_REFRESH_RESULT:-}"
  if [[ -n "$ARTIFACT_LIST" ]]; then
    rm -f -- "$ARTIFACT_LIST"
  fi
  if [[ -n "$LIVE_VERIFY_ENV" ]]; then
    rm -f -- "$LIVE_VERIFY_ENV"
  fi
  if [[ "$status" != "0" ]]; then
    cleanup_partial_generation || true
  fi
  if [[ -z "$result" ]]; then
    if [[ "$status" == "0" ]]; then
      result="success"
    else
      result="failed"
    fi
  fi
  export DEGEN_DOGS_REFRESH_RESULT="$result"
  if [[ "$status" != "0" && -z "${DEGEN_DOGS_REFRESH_ERROR:-}" ]]; then
    export DEGEN_DOGS_REFRESH_ERROR="exit status ${status}"
  fi
  if [[ -f "${REPO_DIR}/scripts/refresh_telemetry.py" ]]; then
    python3 "${REPO_DIR}/scripts/refresh_telemetry.py" record-refresh --result "$result" --error "${DEGEN_DOGS_REFRESH_ERROR:-}" >/dev/null 2>&1 || true
  fi
  log "finished status=${status}"
  exit "$status"
}
trap finish EXIT

log "starting hourly refresh repo=${REPO_DIR} branch=${BRANCH} lock=${REFRESH_LOCK_PATH}"
cd "$REPO_DIR"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  fail "not a git worktree: ${REPO_DIR}"
fi
if ! git check-ref-format --branch "$BRANCH" >/dev/null 2>&1; then
  fail "invalid git branch ref: ${BRANCH}"
fi
clear_stale_git_index_lock "publisher preflight" || fail "git index lock is active, recent, or unsafe"
recover_interrupted_generation
if [[ "$RECOVERY_SUPERSEDED" == "1" ]]; then
  complete_peer_supersession "$(git rev-parse HEAD)" "interrupted recovery"
fi

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    log "tracked changes exist on ${current_branch}; refusing to switch to ${BRANCH}"
    git status --short --untracked-files=no
    exit 1
  fi
  git switch "$BRANCH"
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  log "tracked working tree changes exist before refresh; refusing to overwrite"
  git status --short --untracked-files=no
  exit 1
fi

python3 - <<'PY'
from __future__ import annotations

import subprocess
import sys

paths = [
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
]
status = subprocess.check_output(
    ["git", "status", "--porcelain", "--untracked-files=all", "--", *paths],
    text=True,
)
untracked = [line[3:] for line in status.splitlines() if line.startswith("?? ")]
if untracked:
    print("refusing to refresh with pre-existing untracked publish-path files:", file=sys.stderr)
    for path in untracked:
        print(f"  {path}", file=sys.stderr)
    sys.exit(1)
PY

if [[ "$SKIP_PULL" != "1" ]]; then
  export DEGEN_DOGS_GIT_PULL_STARTED_AT_UTC="$(utc_stamp)"
  run_with_retry "git fetch" git fetch "$REMOTE" "$BRANCH"
  run_with_retry "git pull --ff-only" git pull --ff-only "$REMOTE" "$BRANCH"
  export DEGEN_DOGS_GIT_PULL_COMPLETED_AT_UTC="$(utc_stamp)"
fi

BASELINE_HEAD="$(git rev-parse HEAD)"
if git show-ref --verify --quiet "refs/remotes/${REMOTE}/${BRANCH}"; then
  LOCAL_AHEAD_COUNT="$(git rev-list --count "${REMOTE}/${BRANCH}..HEAD")"
fi
if [[ ! "$LOCAL_AHEAD_COUNT" =~ ^[0-9]+$ ]]; then
  fail "unable to determine local-ahead commit count"
fi
if (( LOCAL_AHEAD_COUNT > 0 )); then
  fail "local ${BRANCH} is ${LOCAL_AHEAD_COUNT} commit(s) ahead of ${REMOTE}/${BRANCH}; refusing to publish unverified history"
fi

if [[ ! -d node_modules || package-lock.json -nt node_modules/.package-lock.json ]]; then
  log "installing npm dependencies"
  npm ci --ignore-scripts
fi

if [[ "$RUN_MISSION3_ARCHIVE" == "1" ]]; then
  if ! python3 - <<'PY'
from Crypto.Hash import keccak

digest = keccak.new(digest_bits=256, data=b"").hexdigest()
if digest != "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470":
    raise SystemExit("unexpected Ethereum Keccak-256 implementation")
PY
  then
    fail "Mission 3 archive requires the hash-locked Python runtime; create .venv and install requirements.txt"
  fi
fi

write_recovery_journal || fail "could not persist publisher crash-recovery journal"
MUTATION_STARTED=1

if [[ "$RUN_MISSION3_ARCHIVE" == "1" ]]; then
  log "running Mission 3 archive incremental index"
  npm run archive:mission3:index
  log "checking Mission 3 archive health"
  npm run archive:mission3:health
fi

log "running blockchain data generator"
export DEGEN_DOGS_DATA_STARTED_AT_UTC="$(utc_stamp)"
if [[ "$FULL_REFRESH" == "1" ]]; then
  log "full refresh explicitly enabled"
  npm run data
else
  log "bounded current refresh (set DEGEN_DOGS_FULL_REFRESH=1 for full history)"
  if npm run refresh:current; then
    :
  else
    current_refresh_status=$?
    if [[ "$current_refresh_status" == "75" ]]; then
      FULL_REFRESH=1
      export DEGEN_DOGS_FULL_REFRESH=1
      if [[ "$RUN_SCOPE" == "current" ]]; then
        RUN_SCOPE="full"
        export DEGEN_DOGS_RUN_SCOPE="full"
        update_recovery_run_scope "full" || \
          fail "could not promote publisher recovery journal to full-refresh scope"
      elif [[ "$RUN_SCOPE" == "archive" ]]; then
        RUN_SCOPE="archive_full"
        export DEGEN_DOGS_RUN_SCOPE="archive_full"
        update_recovery_run_scope "archive_full" || \
          fail "could not promote archive recovery journal to archive/full scope"
      fi
      log "bounded current refresh requires full reconciliation; falling back to npm run data"
      npm run data
    else
      exit "$current_refresh_status"
    fi
  fi
fi
export DEGEN_DOGS_DATA_COMPLETED_AT_UTC="$(utc_stamp)"

log "validating generated artifacts"
export DEGEN_DOGS_VALIDATION_STARTED_AT_UTC="$(utc_stamp)"
python3 -m py_compile scripts/build_dashboard.py scripts/build_live_snapshot_bundle.py
python3 - <<'PY'
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

root = Path.cwd()
errors: list[str] = []
manifest_path = root / "generated" / "manifest.csv"
if not manifest_path.exists():
    raise SystemExit("generated/manifest.csv missing")

with manifest_path.open(newline="", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    if reader.fieldnames != ["table", "file", "rows"]:
        errors.append(f"manifest header mismatch: {reader.fieldnames}")
    rows = list(reader)

if not rows:
    errors.append("manifest has no rows")

artifact_rel_pattern = re.compile(r"generated/[A-Za-z0-9_]+\.csv")
for row in rows:
    table = row.get("table", "")
    rel = row.get("file", "")
    if not artifact_rel_pattern.fullmatch(rel):
        errors.append(f"unsafe manifest artifact path for {table}: {rel!r}")
        continue
    expected_rows = int(row.get("rows") or -1)
    csv_path = root / rel
    json_path = csv_path.with_suffix(".json")
    public_csv = root / "public" / rel
    public_json = public_csv.with_suffix(".json")
    for path in (csv_path, json_path, public_csv, public_json):
        if not path.exists():
            errors.append(f"missing artifact for {table}: {path.relative_to(root)}")
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as handle:
            actual_rows = max(sum(1 for _ in handle) - 1, 0)
        if actual_rows != expected_rows:
            errors.append(f"row count mismatch for {table}: manifest={expected_rows} csv={actual_rows}")
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            errors.append(f"json artifact is not a list: {json_path.relative_to(root)}")
        elif len(data) != expected_rows:
            errors.append(f"json row count mismatch for {table}: manifest={expected_rows} json={len(data)}")

metrics: dict[str, str] = {}
metrics_path = root / "generated" / "mission3_metrics.csv"
if metrics_path.exists():
    with metrics_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            metrics[row.get("metric", "")] = row.get("value", "")
else:
    errors.append("generated/mission3_metrics.csv missing")

if metrics.get("site_url") != "https://ael-dev3.github.io/Degen-Dogs-Mission-3/":
    errors.append("site_url metric missing or incorrect")
if not metrics.get("latest_block", "").isdigit():
    errors.append("latest_block metric missing or non-numeric")

index = (root / "index.html").read_text(encoding="utf-8") if (root / "index.html").exists() else ""
if 'data-table="auction_feed"' not in index:
    errors.append("index.html missing rendered auction_feed table")
if 'data-table="mission3_metrics"' not in index or "site_url" not in index or "latest_block" not in index:
    errors.append("index.html missing hidden mission3_metrics verification table")
if "generated/auction_feed.csv" not in index and not (root / "public" / "generated" / "auction_feed.csv").exists():
    errors.append("auction_feed public CSV artifact missing")

if errors:
    raise SystemExit("\n".join(errors))
print("artifact validation ok")
PY
npm run validate:dashboard
python3 scripts/refresh_telemetry.py validate-status
npm run archive:prices:validate
npm run check:historical-dogs
export DEGEN_DOGS_VALIDATION_COMPLETED_AT_UTC="$(utc_stamp)"

git diff --check
export DEGEN_DOGS_BUILD_STARTED_AT_UTC="$(utc_stamp)"
npm run build
export DEGEN_DOGS_BUILD_COMPLETED_AT_UTC="$(utc_stamp)"

DEGEN_DOGS_REFRESH_RESULT=success_generated python3 scripts/refresh_telemetry.py write-status --prefer-current-env >/dev/null
python3 scripts/refresh_telemetry.py validate-status

export DEGEN_DOGS_GIT_STATUS_STARTED_AT_UTC="$(utc_stamp)"
export DEGEN_DOGS_CHANGED_FILES="$(python3 - <<'PY'
from __future__ import annotations

import json
import subprocess

paths = [
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
]
changed = set(
    line.strip()
    for line in subprocess.check_output(["git", "diff", "--name-only", "--", *paths], text=True).splitlines()
    if line.strip()
)
status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all", "--", *paths], text=True)
for line in status.splitlines():
    if line.startswith("?? "):
        path = line[3:].strip()
        if path:
            changed.add(path)
print(json.dumps(sorted(changed)))
PY
)"
export DEGEN_DOGS_GIT_STATUS_COMPLETED_AT_UTC="$(utc_stamp)"

if [[ "$DEGEN_DOGS_CHANGED_FILES" == "[]" ]]; then
  log "no generated website/archive data changes to publish"
  MUTATION_STARTED=0
  remove_recovery_journal || fail "could not clear publisher recovery journal after no-diff refresh"
  export DEGEN_DOGS_REFRESH_RESULT="success_no_diff"
  exit 0
fi

ARTIFACT_LIST="$(mktemp -t degen-dogs-artifacts.XXXXXX)"
python3 - <<'PY' > "$ARTIFACT_LIST"
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

paths = ["README.md", "index.html"]
with open("generated/manifest.csv", newline="", encoding="utf-8") as handle:
    for row in csv.DictReader(handle):
        rel = row["file"]
        csv_path = Path(rel)
        json_path = csv_path.with_suffix(".json")
        paths.extend([str(csv_path), str(json_path), str(Path("public") / csv_path), str(Path("public") / json_path)])
paths.extend(["generated/manifest.csv", "generated/manifest.json", "public/generated/manifest.csv", "public/generated/manifest.json", "generated/refresh_status.json", "public/generated/refresh_status.json"])
archive_public = Path("public/generated/mission3")
archive_generated = Path("archive/mission3/data/generated")
unified_archive = Path("archive/data/generated")
unified_public = Path("public/generated")
live_bundle_directories = (Path("generated"), Path("public/generated"))
identity_path = Path("archive/data/identity/wallet_profiles.json")
dog_archive = Path("archive/dogs")
price_generated = Path("archive/prices/data/generated")
price_raw = Path("archive/prices/data/raw")
if archive_public.exists():
    paths.extend(str(path) for path in sorted(archive_public.glob("*.json")))
if archive_generated.exists():
    paths.extend(str(path) for path in sorted(archive_generated.glob("*.csv")))
    paths.extend(str(path) for path in sorted(archive_generated.glob("*.json")))
if unified_archive.exists():
    paths.extend(str(path) for path in sorted(unified_archive.glob("unified_dog_search_*.json")))
if unified_public.exists():
    paths.extend(str(path) for path in sorted(unified_public.glob("unified_dog_search_*.json")))
live_bundle_name = re.compile(
    r"^live_snapshot_[1-9][0-9]*_[0-9a-f]{64}_[0-9a-f]{64}\.json$"
)
for directory in live_bundle_directories:
    if not directory.exists():
        continue
    for path in sorted(directory.glob("live_snapshot_*.json")):
        if not live_bundle_name.fullmatch(path.name):
            raise SystemExit(f"refusing unsafe live snapshot artifact name: {path}")
        paths.append(str(path))
if identity_path.exists():
    paths.append(str(identity_path))
if dog_archive.exists():
    manifest = dog_archive / "manifest.json"
    if manifest.exists():
        paths.append(str(manifest))
    by_id = dog_archive / "by-id"
    if by_id.exists():
        paths.extend(str(path) for path in sorted(by_id.glob("*.json")))
if price_generated.exists():
    paths.extend(str(path) for path in sorted(price_generated.glob("*.csv")))
    paths.extend(str(path) for path in sorted(price_generated.glob("*.json")))
if price_raw.exists():
    paths.extend(str(path) for path in sorted(price_raw.glob("*.json")))
allowed_exact = {"README.md", "index.html", "archive/data/identity/wallet_profiles.json", "archive/dogs/manifest.json"}
allowed_patterns = (
    re.compile(r"^(generated|public/generated)/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^archive/mission3/data/generated/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^public/generated/mission3/[A-Za-z0-9_]+\.json$"),
    re.compile(r"^archive/data/generated/unified_dog_search_[A-Za-z0-9_]+\.json$"),
    re.compile(r"^archive/dogs/by-id/[0-9]+\.json$"),
    re.compile(r"^archive/prices/data/generated/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^archive/prices/data/raw/[A-Za-z0-9_-]+\.json$"),
)
for path in dict.fromkeys(paths):
    if path not in allowed_exact and not any(pattern.fullmatch(path) for pattern in allowed_patterns):
        raise SystemExit(f"refusing unsafe artifact inventory path before staging: {path}")
    sys.stdout.write(path + "\0")
PY

# Include tracked deletions in the same NUL-delimited literal inventory.
git diff --name-only -z --diff-filter=D "$BASELINE_HEAD" -- "${PUBLISH_PATHS[@]}" >> "$ARTIFACT_LIST"

# Stage the allowlisted inventory in one index transaction. The previous
# per-file loop spawned hundreds of git processes and could hold the shared
# refresh lock for close to a minute on every full rebuild.
clear_stale_git_index_lock "artifact staging" || fail "git index lock is active, recent, or unsafe"
git --literal-pathspecs add --pathspec-from-file="$ARTIFACT_LIST" --pathspec-file-nul

rm -f -- "$ARTIFACT_LIST"
ARTIFACT_LIST=""

if ! git diff --quiet -- "${PUBLISH_PATHS[@]}"; then
  log "unstaged tracked publish-path changes remain after artifact staging"
  git diff --name-status -- "${PUBLISH_PATHS[@]}"
  fail "refusing to commit a partial generated snapshot"
fi
if [[ -n "$(git ls-files --others --exclude-standard -- "${PUBLISH_PATHS[@]}")" ]]; then
  log "unstaged untracked publish-path artifacts remain after artifact staging"
  git ls-files --others --exclude-standard -- "${PUBLISH_PATHS[@]}"
  fail "refusing to commit a partial generated snapshot"
fi

git diff --cached --check

python3 - <<'PY'
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], text=True).splitlines()
allowed_exact = {"README.md", "index.html", "archive/data/identity/wallet_profiles.json", "archive/dogs/manifest.json"}
allowed_artifact = re.compile(r"^(generated|public/generated)/[A-Za-z0-9_]+\.(csv|json)$")
allowed_archive_artifact = re.compile(r"^archive/mission3/data/generated/[A-Za-z0-9_]+\.(csv|json)$")
allowed_public_archive = re.compile(r"^public/generated/mission3/[A-Za-z0-9_]+\.json$")
allowed_unified_archive = re.compile(r"^archive/data/generated/unified_dog_search_[A-Za-z0-9_]+\.json$")
allowed_dog_archive = re.compile(r"^archive/dogs/by-id/[0-9]+\.json$")
allowed_price_archive = re.compile(r"^archive/prices/data/generated/[A-Za-z0-9_]+\.(csv|json)$")
allowed_price_raw = re.compile(r"^archive/prices/data/raw/[A-Za-z0-9_\-]+\.json$")
unexpected = [
    path for path in staged
    if path not in allowed_exact
    and not allowed_artifact.fullmatch(path)
    and not allowed_archive_artifact.fullmatch(path)
    and not allowed_public_archive.fullmatch(path)
    and not allowed_unified_archive.fullmatch(path)
    and not allowed_dog_archive.fullmatch(path)
    and not allowed_price_archive.fullmatch(path)
    and not allowed_price_raw.fullmatch(path)
]
if unexpected:
    print("refusing to publish unexpected staged paths:", file=sys.stderr)
    for path in unexpected:
        print(f"  {path}", file=sys.stderr)
    sys.exit(1)

patterns = [
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
]
findings: list[str] = []
for name in staged:
    path = Path(name)
    if not path.exists():
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    for pattern in patterns:
        if pattern.search(text):
            findings.append(name)
            break
if findings:
    raise SystemExit("possible secret pattern in staged generated artifacts: " + ", ".join(sorted(set(findings))))
print("staged path/secret scan ok")
PY

latest_block="$(python3 - <<'PY'
import csv
with open('generated/mission3_metrics.csv', newline='', encoding='utf-8') as handle:
    metrics = {row['metric']: row['value'] for row in csv.DictReader(handle)}
print(metrics.get('latest_block', 'unknown'))
PY
)"
current_dog="$(python3 - <<'PY'
import csv
with open('generated/mission3_metrics.csv', newline='', encoding='utf-8') as handle:
    metrics = {row['metric']: row['value'] for row in csv.DictReader(handle)}
print(metrics.get('current_auction_token_id', 'unknown'))
PY
)"

commit_message="${COMMIT_PREFIX} refresh Mission 3 data"

export DEGEN_DOGS_COMMIT_STARTED_AT_UTC="$(utc_stamp)"
commit_refresh_snapshot
export DEGEN_DOGS_COMMIT_COMPLETED_AT_UTC="$(utc_stamp)"
export DEGEN_DOGS_COMMIT_SHA="$(git rev-parse HEAD)"
RUNNER_COMMIT_HEAD="$DEGEN_DOGS_COMMIT_SHA"
RUNNER_COMMIT_RUN_ID="$DEGEN_DOGS_REFRESH_RUN_ID"
RUNNER_COMMIT_RUNNER_ID="$RUNNER_ID"
RUNNER_COMMIT_SCOPE="$RUN_SCOPE"
if [[ "$(git rev-parse HEAD)" != "$RUNNER_COMMIT_HEAD" ]] || \
  ! validate_runner_commit "$RUNNER_COMMIT_HEAD" "$BASELINE_HEAD" "$DEGEN_DOGS_REFRESH_RUN_ID" "$RUNNER_ID" "$RUN_SCOPE"; then
  fail "publisher commit identity, parent, attribution, or exact path set failed validation"
fi
if [[ "$(runner_commit_scope "$RUNNER_COMMIT_HEAD" 2>/dev/null || true)" != "$RUN_SCOPE" ]]; then
  fail "publisher commit scope differs from the completed generation scope"
fi

if [[ "$SKIP_PUSH" == "1" ]]; then
  log "DEGEN_DOGS_SKIP_PUSH=1; leaving commit local"
  MUTATION_STARTED=0
  RUNNER_COMMIT_HEAD=""
  RUNNER_COMMIT_RUN_ID=""
  RUNNER_COMMIT_RUNNER_ID=""
  RUNNER_COMMIT_SCOPE=""
  remove_recovery_journal || fail "could not clear publisher recovery journal after skip-push refresh"
  export DEGEN_DOGS_REFRESH_RESULT="success_skip_push"
  exit 0
fi

log "pushing generated data refresh"
export DEGEN_DOGS_PUSH_STARTED_AT_UTC="$(utc_stamp)"
if [[ "$(git rev-parse HEAD)" != "$RUNNER_COMMIT_HEAD" ]] || \
  ! validate_runner_commit "$RUNNER_COMMIT_HEAD" "$BASELINE_HEAD" "$DEGEN_DOGS_REFRESH_RUN_ID" "$RUNNER_ID" "$RUN_SCOPE"; then
  fail "local branch changed after publisher commit; refusing to push a moving branch ref"
fi
if [[ "$(runner_commit_scope "$RUNNER_COMMIT_HEAD" 2>/dev/null || true)" != "$RUN_SCOPE" ]]; then
  fail "publisher commit scope changed before push"
fi
run_with_retry "git fetch before push" git fetch "$REMOTE" "$BRANCH"
remote_head="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}")"
if [[ "$remote_head" != "$BASELINE_HEAD" ]]; then
  handle_remote_advance_after_local_commit "$remote_head" "pre-push fetch"
fi
push_status=0
if push_with_compare_and_swap; then
  :
else
  push_status=$?
  if [[ "$push_status" == "75" && -n "$PUSH_REMOTE_HEAD" ]]; then
    handle_remote_advance_after_local_commit "$PUSH_REMOTE_HEAD" "compare-and-swap push"
  fi
  fail "immutable compare-and-swap publisher push failed with status ${push_status} and did not land"
fi
# From this point the immutable commit is known to have landed. Never let a
# later diagnostic or Pages failure rewind it locally.
MUTATION_STARTED=0
if [[ "$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}")" != "$RUNNER_COMMIT_HEAD" ]]; then
  fail "remote-tracking ref did not confirm the immutable publisher commit after push"
fi
export DEGEN_DOGS_PUSH_COMPLETED_AT_UTC="$(utc_stamp)"
export DEGEN_DOGS_REFRESH_RESULT="success_pushed"
RUNNER_COMMIT_HEAD=""
RUNNER_COMMIT_RUN_ID=""
RUNNER_COMMIT_RUNNER_ID=""
RUNNER_COMMIT_SCOPE=""
remove_recovery_journal || fail "could not clear publisher recovery journal after push"

verify_live_deployment
log "published snapshot block=${latest_block} current_dog=${current_dog}"
