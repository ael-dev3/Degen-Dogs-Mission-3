#!/usr/bin/env bash
set -Eeuo pipefail

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
LOG_DIR="${DEGEN_DOGS_LOG_DIR:-${USER_HOME}/Library/Logs/degen-dogs-mission3}"
LOCK_DIR="${DEGEN_DOGS_LOCK_DIR:-${USER_HOME}/Library/Caches/degen-dogs-mission3}"
REMOTE="${DEGEN_DOGS_REMOTE:-origin}"
BRANCH="${DEGEN_DOGS_BRANCH:-main}"
COMMIT_PREFIX="${DEGEN_DOGS_COMMIT_PREFIX:-[cron]}"
SKIP_PUSH="${DEGEN_DOGS_SKIP_PUSH:-0}"
SKIP_PULL="${DEGEN_DOGS_SKIP_PULL:-0}"
RUN_MISSION3_ARCHIVE="${DEGEN_DOGS_RUN_MISSION3_ARCHIVE:-0}"

mkdir -p "$LOG_DIR"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/refresh.log}"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '[%s] %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

fail() {
  log "error: $*"
  exit 1
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

if [[ "${DEGEN_DOGS_LOCK_HELD:-0}" != "1" ]]; then
  export DEGEN_DOGS_LOCK_DIR="$LOCK_DIR"
  exec python3 - "$0" "$@" <<'PY'
from __future__ import annotations

import fcntl
import os
import stat
import sys
from pathlib import Path

script = os.path.abspath(sys.argv[1])
args = sys.argv[2:]
lock_root = Path(os.environ["DEGEN_DOGS_LOCK_DIR"]).expanduser()
lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
st = lock_root.lstat()
if not stat.S_ISDIR(st.st_mode):
    print(f"lock path is not a directory: {lock_root}", file=sys.stderr)
    sys.exit(1)
if st.st_uid != os.getuid():
    print(f"lock directory is not owned by current user: {lock_root}", file=sys.stderr)
    sys.exit(1)
os.chmod(lock_root, 0o700)
lock_path = lock_root / "refresh.lock"
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
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
os.execvpe(script, [script, *args], env)
PY
fi

finish() {
  local status=$?
  log "finished status=${status}"
  exit "$status"
}
trap finish EXIT

log "starting hourly refresh repo=${REPO_DIR} branch=${BRANCH} lock=${LOCK_DIR}/refresh.lock"
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

paths = ["README.md", "index.html", "generated", "public", "archive/mission3/data/generated"]
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
  git fetch "$REMOTE" "$BRANCH"
  git pull --ff-only "$REMOTE" "$BRANCH"
fi

if [[ ! -d node_modules || package-lock.json -nt node_modules/.package-lock.json ]]; then
  log "installing npm dependencies"
  npm ci
fi

if [[ "$RUN_MISSION3_ARCHIVE" == "1" ]]; then
  log "running Mission 3 archive incremental index"
  npm run archive:mission3:index
  log "checking Mission 3 archive health"
  npm run archive:mission3:health
fi

log "running blockchain data generator"
npm run data

log "validating generated artifacts"
python3 -m py_compile scripts/build_dashboard.py
python3 - <<'PY'
from __future__ import annotations

import csv
import json
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

for row in rows:
    table = row.get("table", "")
    rel = row.get("file", "")
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

git diff --check
npm run build

if git diff --quiet -- README.md index.html generated public archive/mission3/data/generated; then
  log "no generated website/archive data changes to publish"
  exit 0
fi

artifact_list="$(mktemp -t degen-dogs-artifacts.XXXXXX)"
python3 - <<'PY' > "$artifact_list"
from __future__ import annotations

import csv
from pathlib import Path

paths = ["README.md", "index.html"]
with open("generated/manifest.csv", newline="", encoding="utf-8") as handle:
    for row in csv.DictReader(handle):
        rel = row["file"]
        csv_path = Path(rel)
        json_path = csv_path.with_suffix(".json")
        paths.extend([str(csv_path), str(json_path), str(Path("public") / csv_path), str(Path("public") / json_path)])
paths.extend(["generated/manifest.csv", "generated/manifest.json", "public/generated/manifest.csv", "public/generated/manifest.json"])
archive_public = Path("public/generated/mission3")
archive_generated = Path("archive/mission3/data/generated")
if archive_public.exists():
    paths.extend(str(path) for path in sorted(archive_public.glob("*.json")))
if archive_generated.exists():
    paths.extend(str(path) for path in sorted(archive_generated.glob("*.csv")))
    paths.extend(str(path) for path in sorted(archive_generated.glob("*.json")))
for path in dict.fromkeys(paths):
    print(path)
PY

while IFS= read -r path; do
  git add -- "$path"
done < "$artifact_list"
rm -f "$artifact_list"

git diff --cached --check

python3 - <<'PY'
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], text=True).splitlines()
allowed_exact = {"README.md", "index.html"}
allowed_artifact = re.compile(r"^(generated|public/generated)/[A-Za-z0-9_]+\.(csv|json)$")
allowed_archive_artifact = re.compile(r"^archive/mission3/data/generated/[A-Za-z0-9_]+\.(csv|json)$")
allowed_public_archive = re.compile(r"^public/generated/mission3/[A-Za-z0-9_]+\.json$")
unexpected = [
    path for path in staged
    if path not in allowed_exact
    and not allowed_artifact.fullmatch(path)
    and not allowed_archive_artifact.fullmatch(path)
    and not allowed_public_archive.fullmatch(path)
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

git commit \
  -m "$commit_message" \
  -m "Snapshot block: ${latest_block}" \
  -m "Current dog: ${current_dog}" \
  -m "Automated refresh from the private Mac mini runner."

if [[ "$SKIP_PUSH" == "1" ]]; then
  log "DEGEN_DOGS_SKIP_PUSH=1; leaving commit local"
  exit 0
fi

log "pushing generated data refresh"
git push "$REMOTE" "$BRANCH"
log "published snapshot block=${latest_block} current_dog=${current_dog}"
