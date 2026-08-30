#!/usr/bin/env bash
set -Eeuo pipefail

# Regression test: a failed generator must not poison every later scheduled run.

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_ROOT="$(mktemp -d -t degen-dogs-refresh-test.XXXXXX)"
TEST_REPO="${TEST_ROOT}/repo"

cleanup() {
  local status=$?
  if [[ "$status" != "0" && -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" ]]; then
    find "$TEST_ROOT" -name refresh.log -type f -exec sh -c 'printf "publisher fixture log: %s\n" "$1" >&2; tail -n 80 "$1" >&2' _ {} \;
  fi
  if [[ -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" && "$(basename "$TEST_ROOT")" == degen-dogs-refresh-test.* ]]; then
    rm -rf -- "$TEST_ROOT"
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

mkdir -p "$TEST_REPO/scripts" "$TEST_REPO/generated" "$TEST_REPO/node_modules" "$TEST_ROOT/home"
cp "$SOURCE_DIR/refresh_and_publish.sh" "$TEST_REPO/scripts/refresh_and_publish.sh"
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
mkdir -p "$TEST_ROOT/locks/recovery" "$QUARANTINE_ATTACK_TARGET"
chmod 700 "$TEST_ROOT/locks/recovery" "$QUARANTINE_ATTACK_TARGET"
ln -s "$QUARANTINE_ATTACK_TARGET" "$TEST_ROOT/locks/recovery/fixture-quarantine"
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
if [[ -z "$(find "$TEST_ROOT/locks/recovery" -type f -path '*/generated/runner-created.json' -print -quit)" ]]; then
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
if [[ "$(find "$TEST_ROOT/locks/recovery" -type f -name '*.json' | wc -l | tr -d ' ')" -lt 3 ]]; then
  echo "full-fallback untracked artifacts were not preserved in recovery quarantine" >&2
  exit 1
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
  'import os' \
  'import subprocess' \
  'import sys' \
  'from pathlib import Path' \
  'if len(sys.argv) > 1 and sys.argv[1] == "record-refresh" and os.environ.get("FIXTURE_RESULT_MARKER"):' \
  '    Path(os.environ["FIXTURE_RESULT_MARKER"]).write_text(os.environ.get("DEGEN_DOGS_REFRESH_RESULT", "") + "\n" + os.environ.get("DEGEN_DOGS_COMMIT_SHA", "") + "\n", encoding="utf-8")' \
  '    raise SystemExit(0)' \
  'if "validate-status" in sys.argv[1:]:' \
  '    root = Path(sys.argv[sys.argv.index("--root") + 1]) if "--root" in sys.argv else Path(__file__).resolve().parents[1]' \
  '    validator = Path(__file__).with_name("refresh_telemetry_validator.py")' \
  '    raise SystemExit(subprocess.run([sys.executable, str(validator), "--root", str(root), "validate-status"], check=False).returncode)' \
  'if len(sys.argv) > 1 and sys.argv[1] == "verify-live" and os.environ.get("FIXTURE_LIVE_TIMEOUT") == "1":' \
  '    env_path = Path(sys.argv[sys.argv.index("--env-file") + 1])' \
  '    env_path.write_text("export DEGEN_DOGS_LIVE_VERIFY_RESULT='"'"'timeout'"'"'\nexport DEGEN_DOGS_RAW_COMMIT_VERIFIED='"'"'True'"'"'\nexport DEGEN_DOGS_LIVE_VERIFY_ERROR='"'"'github_pages mismatch'"'"'\n", encoding="utf-8")' \
  '    raise SystemExit(2)' \
  'raise SystemExit(0)' >"$SUCCESS_REPO/scripts/refresh_telemetry.py"
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
  ! find "$CRASH_LOCK_DIR/recovery" -type f -name crash-only.json -print -quit | grep -q .; then
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
  -m "Refresh-Run-ID: post-commit-crash-fixture"

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
  '    path.write_text(json.dumps(status, sort_keys=True) + "\\n", encoding="utf-8")' \
  'for path in (Path("generated/auction_feed.csv"), Path("public/generated/auction_feed.csv")):' \
  '    path.write_text("id\\n3\\n", encoding="utf-8")' \
  'for path in (Path("generated/auction_feed.json"), Path("public/generated/auction_feed.json")):' \
  '    path.write_text("[{\\"id\\":3}]\\n", encoding="utf-8")' \
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
touch_valid_refresh_status "$CAS_PEER_REPO" "2026-08-18T20:04:00Z"
printf '%s\n' 'id' '3' >"$CAS_PEER_REPO/generated/auction_feed.csv"
printf '%s\n' '[{"id":3}]' >"$CAS_PEER_REPO/generated/auction_feed.json"
cp "$CAS_PEER_REPO/generated/auction_feed.csv" "$CAS_PEER_REPO/public/generated/auction_feed.csv"
cp "$CAS_PEER_REPO/generated/auction_feed.json" "$CAS_PEER_REPO/public/generated/auction_feed.json"
git -C "$CAS_PEER_REPO" add generated/refresh_status.json public/generated/refresh_status.json \
  generated/auction_feed.csv generated/auction_feed.json \
  public/generated/auction_feed.csv public/generated/auction_feed.json
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
