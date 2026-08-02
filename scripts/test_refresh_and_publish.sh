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

mkdir -p "$TEST_REPO/scripts" "$TEST_REPO/generated" "$TEST_REPO/node_modules" "$TEST_ROOT/home"
cp "$SOURCE_DIR/refresh_and_publish.sh" "$TEST_REPO/scripts/refresh_and_publish.sh"
cp "$SOURCE_DIR/runner_permissions.sh" "$TEST_REPO/scripts/runner_permissions.sh"
cp "$SOURCE_DIR/runner_path_security.py" "$TEST_REPO/scripts/runner_path_security.py"
chmod +x "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'npm ci --ignore-scripts' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'artifact_rel_pattern.fullmatch(rel)' "$TEST_REPO/scripts/refresh_and_publish.sh"
grep -q 'literal-pathspecs add --pathspec-from-file' "$TEST_REPO/scripts/refresh_and_publish.sh"

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
  "$SUCCESS_REPO/generated" \
  "$SUCCESS_REPO/public/generated" \
  "$SUCCESS_REPO/node_modules"
cp "$SOURCE_DIR/refresh_and_publish.sh" "$SUCCESS_REPO/scripts/refresh_and_publish.sh"
cp "$SOURCE_DIR/runner_permissions.sh" "$SUCCESS_REPO/scripts/runner_permissions.sh"
cp "$SOURCE_DIR/runner_path_security.py" "$SUCCESS_REPO/scripts/runner_path_security.py"
chmod +x "$SUCCESS_REPO/scripts/refresh_and_publish.sh"
printf '%s\n' \
  '{"scripts":{' \
  '"refresh:current":"node scripts/success_generation.js",' \
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
  "fs.writeFileSync('public/generated/auction_feed.json', '[{\\\"id\\\":2}]\\n');" >"$SUCCESS_REPO/scripts/success_generation.js"
printf '%s\n' \
  "const fs = require('fs');" \
  "fs.writeFileSync(process.env.VALIDATOR_MARKER, 'validated\\n');" >"$SUCCESS_REPO/scripts/validate_marker.js"
printf '%s\n' '# fixture compiles' >"$SUCCESS_REPO/scripts/build_dashboard.py"
printf '%s\n' \
  '#!/usr/bin/env python3' \
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
printf '%s\n' '{}' >"$SUCCESS_REPO/generated/refresh_status.json"
cp "$SUCCESS_REPO/generated/refresh_status.json" "$SUCCESS_REPO/public/generated/refresh_status.json"
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

HOME="$TEST_ROOT/home" \
VALIDATOR_MARKER="$SUCCESS_MARKER" \
DEGEN_DOGS_REPO_DIR="$SUCCESS_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/success-logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/success-locks" \
DEGEN_DOGS_SKIP_PULL=1 \
DEGEN_DOGS_SKIP_PUSH=1 \
"$SUCCESS_REPO/scripts/refresh_and_publish.sh"

[[ "$(<"$SUCCESS_MARKER")" == "validated" ]]
if git -C "$SUCCESS_REPO" cat-file -e HEAD^:generated/obsolete.json 2>/dev/null && \
  git -C "$SUCCESS_REPO" cat-file -e HEAD:generated/obsolete.json 2>/dev/null; then
  echo "tracked generated deletion was not committed" >&2
  exit 1
fi
if git -C "$SUCCESS_REPO" cat-file -e HEAD:public/generated/obsolete.json 2>/dev/null; then
  echo "tracked public deletion was not committed" >&2
  exit 1
fi
if [[ -n "$(git -C "$SUCCESS_REPO" status --porcelain --untracked-files=all -- README.md index.html generated public)" ]]; then
  echo "successful publisher left an unstaged publish-path diff" >&2
  git -C "$SUCCESS_REPO" status --short --untracked-files=all -- README.md index.html generated public >&2
  exit 1
fi

# A post-commit push failure must atomically rewind only the commit created by
# this runner. Otherwise one transient outage leaves main permanently ahead and
# every later scheduled refresh refuses to run.
REJECT_REMOTE="$TEST_ROOT/reject-remote.git"
git -C "$TEST_ROOT" init -q --bare "$REJECT_REMOTE"
git -C "$SUCCESS_REPO" remote add origin "$REJECT_REMOTE"
git -C "$SUCCESS_REPO" push -q -u origin main
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
