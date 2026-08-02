#!/usr/bin/env bash

# Shared permission guards for private local-runner artifacts.  Callers are
# expected to run with `set -e`; these helpers return non-zero instead of ever
# following or replacing a symlink.

_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
_DEGEN_DOGS_RUNNER_PATH_SECURITY="${_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR}/runner_path_security.py"

degen_dogs_private_dir() {
  local path="${1:?private directory path is required}"
  python3 "$_DEGEN_DOGS_RUNNER_PATH_SECURITY" private-dir "$path"
}

degen_dogs_private_file() {
  local path="${1:?private file path is required}"
  local create="${2:-1}"
  [[ "$create" == "0" || "$create" == "1" ]] || {
    printf 'error: private file create flag must be 0 or 1\n' >&2
    return 1
  }
  python3 "$_DEGEN_DOGS_RUNNER_PATH_SECURITY" private-file "$path" "$create"
}

degen_dogs_private_temp_file() {
  local prefix="${1:?private temporary-file prefix is required}"
  python3 "$_DEGEN_DOGS_RUNNER_PATH_SECURITY" private-temp "$prefix"
}

degen_dogs_unlink_private_file() {
  local path="${1:?private file path is required}"
  python3 "$_DEGEN_DOGS_RUNNER_PATH_SECURITY" private-unlink "$path"
}

degen_dogs_resolve_runner_path() {
  local repo_dir="${1:?repo directory is required}"
  local path="${2:?runner path is required}"
  if [[ "$path" == "~/"* ]]; then
    printf '%s/%s\n' "${HOME:?HOME is required}" "${path#\~/}"
  elif [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$repo_dir" "$path"
  fi
}

degen_dogs_acquire_installer_lock() {
  local lock_path="${1:?installer refresh lock path is required}"
  local installer_script="${2:?installer script path is required}"
  shift 2

  export DEGEN_DOGS_INSTALL_LOCK_PATH="$lock_path"
  if [[ "${DEGEN_DOGS_INSTALL_LOCK_HELD:-0}" != "1" ]]; then
    exec python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$lock_path" "$installer_script" "$@" <<'PY'
from __future__ import annotations

import fcntl
import os
import sys
from datetime import datetime, timezone

helper_dir = sys.argv[1]
lock_path = os.path.expanduser(sys.argv[2])
installer_script = os.path.abspath(sys.argv[3])
args = sys.argv[4:]
sys.path.insert(0, helper_dir)
from runner_path_security import SecurePathError, open_private_lock

try:
    fd = open_private_lock(lock_path)
except (OSError, SecurePathError) as exc:
    raise SystemExit(f"error: refusing unsafe installer refresh lock path: {exc}") from exc
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as exc:
    os.close(fd)
    raise SystemExit("error: refusing to install while a refresh or another installer owns the shared lock") from exc
os.ftruncate(fd, 0)
metadata = (
    f"kind=installer\npid={os.getpid()}\n"
    f"started_at_utc={datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')}\n"
)
os.write(fd, metadata.encode("utf-8"))
os.set_inheritable(fd, True)
environment = os.environ.copy()
environment["DEGEN_DOGS_INSTALL_LOCK_HELD"] = "1"
environment["DEGEN_DOGS_INSTALL_LOCK_FD"] = str(fd)
environment["DEGEN_DOGS_INSTALL_LOCK_PATH"] = lock_path
os.execve("/bin/bash", ["bash", installer_script, *args], environment)
PY
  fi

  [[ "${DEGEN_DOGS_INSTALL_LOCK_FD:-}" =~ ^[0-9]+$ ]] || {
    printf 'error: inherited installer lock descriptor is missing or invalid\n' >&2
    return 1
  }
  python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$DEGEN_DOGS_INSTALL_LOCK_FD" "$lock_path" <<'PY'
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
        path_details = os.fstat(path_fd)
    finally:
        os.close(path_fd)
except (OSError, SecurePathError, ValueError) as exc:
    raise SystemExit(f"error: invalid inherited installer lock descriptor: {exc}") from exc
if not stat.S_ISREG(path_details.st_mode):
    raise SystemExit("error: inherited installer lock path is not a regular file")
if not stat.S_ISREG(descriptor.st_mode) or descriptor.st_uid != os.getuid():
    raise SystemExit("error: inherited installer lock descriptor is not an owned regular file")
if (descriptor.st_dev, descriptor.st_ino) != (path_details.st_dev, path_details.st_ino):
    raise SystemExit("error: inherited installer lock descriptor does not match the configured path")
if stat.S_IMODE(descriptor.st_mode) & 0o077:
    raise SystemExit("error: inherited installer lock permissions are too broad")
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError as exc:
    raise SystemExit("error: inherited installer descriptor does not own the shared lock") from exc
PY
}

degen_dogs_release_installer_lock() {
  local descriptor="${DEGEN_DOGS_INSTALL_LOCK_FD:-}"
  if [[ ! "$descriptor" =~ ^[0-9]+$ ]]; then
    printf 'error: installer refresh lock descriptor is missing or invalid\n' >&2
    return 1
  fi
  python3 - "$descriptor" <<'PY'
import fcntl
import sys

fcntl.flock(int(sys.argv[1]), fcntl.LOCK_UN)
PY
  # The shell retains an unlocked descriptor until it exits. Keeping that
  # harmless descriptor avoids eval/dynamic-redirection tricks in a privileged
  # installer; the advisory lock itself has already been released above.
  unset DEGEN_DOGS_INSTALL_LOCK_FD DEGEN_DOGS_INSTALL_LOCK_HELD
}

degen_dogs_install_launchd_transaction() {
  local candidate_path="${1:?candidate plist path is required}"
  local plist_path="${2:?installed plist path is required}"
  local label="${3:?launchd label is required}"
  local uid="${4:?user id is required}"
  local domain="gui/${uid}"
  local target="${domain}/${label}"
  local backup_path=""
  local had_prior="0"
  local failure_stage=""

  [[ "$uid" =~ ^[0-9]+$ ]] || {
    printf 'error: invalid launchd user id\n' >&2
    return 1
  }
  [[ "$label" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    printf 'error: invalid launchd transaction label\n' >&2
    return 1
  }
  [[ "${DEGEN_DOGS_INSTALL_LOCK_HELD:-0}" == "1" ]] || {
    printf 'error: launchd replacement requires the shared installer lock\n' >&2
    return 1
  }

  backup_path="$(degen_dogs_private_temp_file "${plist_path}.previous")"
  if ! had_prior="$(python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$candidate_path" "$plist_path" "$backup_path" <<'PY'
from __future__ import annotations

import os
import sys

helper_dir = sys.argv[1]
candidate = sys.argv[2]
target = sys.argv[3]
backup = sys.argv[4]
sys.path.insert(0, helper_dir)
from runner_path_security import open_existing_private_file, unlink_private_file

candidate_fd = open_existing_private_file(candidate)
os.close(candidate_fd)

try:
    target_fd = open_existing_private_file(target)
except FileNotFoundError:
    unlink_private_file(backup, missing_ok=True)
    print("0")
    raise SystemExit(0)

try:
    backup_fd = open_existing_private_file(backup, writable=True)
    try:
        os.ftruncate(backup_fd, 0)
        while True:
            chunk = os.read(target_fd, 131_072)
            if not chunk:
                break
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(backup_fd, remaining)
                if written <= 0:
                    raise OSError("short write while preserving prior launchd plist")
                remaining = remaining[written:]
        os.fsync(backup_fd)
    finally:
        os.close(backup_fd)
finally:
    os.close(target_fd)
print("1")
PY
  )"; then
    degen_dogs_unlink_private_file "$backup_path" || true
    return 1
  fi

  # The old job is removed only after its protected plist has been copied, and
  # the shared refresh lock remains held throughout replacement and validation.
  if ! launchctl bootout "$domain" "$plist_path" >/dev/null 2>&1; then
    if launchctl print "$target" >/dev/null 2>&1; then
      degen_dogs_unlink_private_file "$backup_path" || true
      degen_dogs_release_installer_lock
      printf 'error: existing launchd job could not be stopped; prior job remains active\n' >&2
      return 1
    fi
  fi

  if ! python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$candidate_path" "$plist_path" <<'PY'
from __future__ import annotations

import sys

helper_dir = sys.argv[1]
sys.path.insert(0, helper_dir)
from runner_path_security import replace_private_file

replace_private_file(sys.argv[2], sys.argv[3])
PY
  then
    failure_stage="install"
  elif ! launchctl bootstrap "$domain" "$plist_path" >/dev/null 2>&1; then
    failure_stage="bootstrap"
  elif ! launchctl enable "$target" >/dev/null 2>&1; then
    failure_stage="enable"
  elif ! launchctl print "$target" >/dev/null 2>&1; then
    failure_stage="print"
  fi

  if [[ -z "$failure_stage" ]]; then
    degen_dogs_unlink_private_file "$backup_path"
    degen_dogs_release_installer_lock
    return 0
  fi

  # A candidate may have loaded before a later verification step failed. Remove
  # it while still serialized, then atomically put the prior bytes back.
  launchctl bootout "$target" >/dev/null 2>&1 || \
    launchctl bootout "$domain" "$plist_path" >/dev/null 2>&1 || true

  # If launchd refused both removals, never overwrite the definition underneath
  # a still-loaded job. A transient validation failure may still be recoverable
  # by re-enabling and re-checking the candidate in place.
  if launchctl print "$target" >/dev/null 2>&1; then
    if launchctl enable "$target" >/dev/null 2>&1 && launchctl print "$target" >/dev/null 2>&1; then
      degen_dogs_unlink_private_file "$backup_path"
      degen_dogs_release_installer_lock
      printf 'warning: candidate launchd %s check failed once but candidate is now enabled and loaded\n' "$failure_stage" >&2
      return 0
    fi
    printf 'error: candidate launchd %s failed and could not be removed; refusing to overwrite a loaded job\n' "$failure_stage" >&2
    return 1
  fi

  if [[ "$had_prior" == "1" ]]; then
    if ! python3 - "$_DEGEN_DOGS_RUNNER_PERMISSIONS_DIR" "$backup_path" "$plist_path" <<'PY'
from __future__ import annotations

import sys

helper_dir = sys.argv[1]
sys.path.insert(0, helper_dir)
from runner_path_security import replace_private_file

replace_private_file(sys.argv[2], sys.argv[3])
PY
    then
      printf 'error: candidate launchd %s failed; prior plist restoration failed\n' "$failure_stage" >&2
      return 1
    fi
    if launchctl bootstrap "$domain" "$plist_path" >/dev/null 2>&1 && \
      launchctl enable "$target" >/dev/null 2>&1 && \
      launchctl print "$target" >/dev/null 2>&1; then
      degen_dogs_release_installer_lock
      printf 'error: candidate launchd %s failed; restored and reloaded prior job\n' "$failure_stage" >&2
      return 1
    fi
    printf 'error: candidate launchd %s failed; prior plist restored but prior job reload could not be confirmed\n' "$failure_stage" >&2
    return 1
  fi

  degen_dogs_unlink_private_file "$backup_path"
  degen_dogs_unlink_private_file "$plist_path"
  if launchctl print "$target" >/dev/null 2>&1; then
    printf 'error: candidate launchd %s failed; no prior plist existed and candidate removal could not be confirmed\n' "$failure_stage" >&2
    return 1
  fi
  degen_dogs_release_installer_lock
  printf 'error: candidate launchd %s failed; no prior plist/job was available for rollback\n' "$failure_stage" >&2
  return 1
}
