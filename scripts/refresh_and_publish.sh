#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Refresh Degen Dogs Mission 3 cached blockchain data locally and publish it to GitHub Pages.
# Intended to run from launchd on the private Mac mini runner.

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
REFRESH_LOCK_PATH="${DEGEN_DOGS_REFRESH_LOCK_PATH:-${LOCK_DIR}/refresh.lock}"
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
ARTIFACT_LIST=""
LIVE_VERIFY_ENV=""
LOCAL_AHEAD_COUNT=0
QUARANTINE_DIR=""

utc_stamp() {
  date -u '+%Y-%m-%dT%H:%M:%SZ'
}

export DEGEN_DOGS_REFRESH_RUN_ID="${DEGEN_DOGS_REFRESH_RUN_ID:-refresh-$(date -u '+%Y%m%dT%H%M%SZ')-$$}"
export DEGEN_DOGS_REFRESH_QUEUED_AT_UTC="${DEGEN_DOGS_REFRESH_QUEUED_AT_UTC:-$(utc_stamp)}"
export DEGEN_DOGS_REFRESH_TRIGGER="${DEGEN_DOGS_REFRESH_TRIGGER:-hourly_refresh}"
export DEGEN_DOGS_REFRESH_TELEMETRY_PATH="${DEGEN_DOGS_REFRESH_TELEMETRY_PATH:-${REPO_DIR}/.local/refresh_runs.jsonl}"
export DEGEN_DOGS_REFRESH_METRICS_PATH="${DEGEN_DOGS_REFRESH_METRICS_PATH:-${REPO_DIR}/logs/refresh-metrics.jsonl}"

degen_dogs_private_dir "$LOG_DIR"
degen_dogs_private_dir "$LOCK_DIR"
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

cleanup_partial_generation() {
  local tracked_changes=""
  local path=""
  if [[ "$MUTATION_STARTED" != "1" || -z "$BASELINE_HEAD" ]]; then
    return 0
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
    export RUNNER_COMMIT_HEAD
    if ! python3 - "${PUBLISH_PATHS[@]}" <<'PY'
from __future__ import annotations

import os
import subprocess
import sys

commit = os.environ["RUNNER_COMMIT_HEAD"]
allowed = tuple(sys.argv[1:])
raw = subprocess.check_output(
    ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit]
)
changed = [os.fsdecode(item) for item in raw.split(b"\0") if item]
unsafe = [
    path
    for path in changed
    if not any(path == root or path.startswith(f"{root}/") for root in allowed)
]
if not changed or unsafe:
    raise SystemExit(
        "runner commit changed no publish artifacts"
        if not changed
        else "runner commit changed paths outside the publish allowlist: " + ", ".join(unsafe)
    )
PY
    then
      log "warning: refusing to rewind runner commit with an unsafe path set"
      return 1
    fi
    git update-ref "refs/heads/${BRANCH}" "$BASELINE_HEAD" "$RUNNER_COMMIT_HEAD" || {
      log "warning: compare-and-swap rewind failed for runner commit ${RUNNER_COMMIT_HEAD}"
      return 1
    }
    log "rewound unpushed runner commit ${RUNNER_COMMIT_HEAD} to ${BASELINE_HEAD}"
    RUNNER_COMMIT_HEAD=""
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
  QUARANTINE_DIR="${LOCK_DIR}/recovery/${DEGEN_DOGS_REFRESH_RUN_ID}"
  export QUARANTINE_DIR REPO_DIR
  python3 - "${PUBLISH_PATHS[@]}" <<'PY' || {
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

repo = Path(os.environ["REPO_DIR"]).resolve()
recovery = Path(os.environ["QUARANTINE_DIR"]).expanduser()
paths = sys.argv[1:]
status = subprocess.check_output(
    ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths],
    cwd=repo,
)
untracked = [entry[3:] for entry in status.split(b"\0") if entry.startswith(b"?? ") and entry[3:]]
if not untracked:
    raise SystemExit(0)
recovery.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(recovery, 0o700)
for raw in untracked:
    relative = Path(os.fsdecode(raw))
    if relative.is_absolute() or ".." in relative.parts:
        raise SystemExit(f"refusing to quarantine unsafe repository path: {relative}")
    source = repo / relative
    if not source.exists() and not source.is_symlink():
        continue
    destination = recovery / relative
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.move(os.fspath(source), os.fspath(destination))
    print(f"quarantined untracked artifact: {relative}")
PY
    log "warning: could not quarantine runner-created untracked artifacts"
    return 1
  }
  if [[ -d "$QUARANTINE_DIR" ]]; then
    log "untracked artifacts preserved under ${QUARANTINE_DIR}"
  fi
  if [[ -n "$(git status --porcelain --untracked-files=all -- "${PUBLISH_PATHS[@]}")" ]]; then
    log "warning: generated artifact paths remain dirty after rollback"
    git status --short --untracked-files=all -- "${PUBLISH_PATHS[@]}"
    return 1
  fi
  MUTATION_STARTED=0
  log "partial generated artifacts rolled back"
}

git_index_lock_is_stale() {
  local lock_path="$1"
  local lock_mtime
  local now

  [[ -e "$lock_path" ]] || return 1
  if command -v lsof >/dev/null 2>&1 && lsof "$lock_path" >/dev/null 2>&1; then
    return 1
  fi
  lock_mtime="$(stat -f %m "$lock_path" 2>/dev/null || stat -c %Y "$lock_path" 2>/dev/null || printf '0')"
  now="$(date +%s)"
  [[ "$lock_mtime" =~ ^[0-9]+$ ]] || return 1
  (( now - lock_mtime >= 60 ))
}

commit_refresh_snapshot() {
  local commit_output
  local commit_status
  local index_lock="${REPO_DIR}/.git/index.lock"

  if commit_output="$(git commit \
    -m "$commit_message" \
    -m "Snapshot block: ${latest_block}" \
    -m "Current dog: ${current_dog}" \
    -m "Automated refresh from the private Mac mini runner." 2>&1)"; then
    printf '%s\n' "$commit_output"
    return 0
  fi
  commit_status=$?
  printf '%s\n' "$commit_output"
  if [[ "$commit_status" != "128" || "$commit_output" != *".git/index.lock"* || "$commit_output" != *"File exists"* ]]; then
    return "$commit_status"
  fi
  if ! git_index_lock_is_stale "$index_lock"; then
    return "$commit_status"
  fi
  rm -f -- "$index_lock"
  log "removed stale git index lock and retrying commit"
  git commit \
    -m "$commit_message" \
    -m "Snapshot block: ${latest_block}" \
    -m "Current dog: ${current_dog}" \
    -m "Automated refresh from the private Mac mini runner."
}

verify_live_deployment() {
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
    export DEGEN_DOGS_REFRESH_RESULT="failed_live_verify"
    fail "live verification did not complete before timeout"
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
python3 -m py_compile scripts/build_dashboard.py
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

if [[ "$SKIP_PUSH" == "1" ]]; then
  log "DEGEN_DOGS_SKIP_PUSH=1; leaving commit local"
  MUTATION_STARTED=0
  RUNNER_COMMIT_HEAD=""
  export DEGEN_DOGS_REFRESH_RESULT="success_skip_push"
  exit 0
fi

log "pushing generated data refresh"
export DEGEN_DOGS_PUSH_STARTED_AT_UTC="$(utc_stamp)"
run_with_retry "git fetch before push" git fetch "$REMOTE" "$BRANCH"
remote_head="$(git rev-parse "refs/remotes/${REMOTE}/${BRANCH}")"
if [[ "$remote_head" != "$BASELINE_HEAD" ]]; then
  fail "${REMOTE}/${BRANCH} changed during refresh; refusing a divergent push (remote=${remote_head} baseline=${BASELINE_HEAD})"
fi
run_with_retry "git push" git push "$REMOTE" "$BRANCH"
export DEGEN_DOGS_PUSH_COMPLETED_AT_UTC="$(utc_stamp)"
export DEGEN_DOGS_REFRESH_RESULT="success_pushed"
MUTATION_STARTED=0
RUNNER_COMMIT_HEAD=""

verify_live_deployment
log "published snapshot block=${latest_block} current_dog=${current_dog}"
