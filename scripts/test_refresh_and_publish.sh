#!/usr/bin/env bash
set -Eeuo pipefail

# Regression test: a failed generator must not poison every later scheduled run.

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEST_ROOT="$(mktemp -d -t degen-dogs-refresh-test.XXXXXX)"
TEST_REPO="${TEST_ROOT}/repo"

cleanup() {
  if [[ -n "${TEST_ROOT:-}" && -d "$TEST_ROOT" && "$(basename "$TEST_ROOT")" == degen-dogs-refresh-test.* ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
}
trap cleanup EXIT

mkdir -p "$TEST_REPO/scripts" "$TEST_REPO/generated" "$TEST_REPO/node_modules" "$TEST_ROOT/home"
cp "$SOURCE_DIR/refresh_and_publish.sh" "$TEST_REPO/scripts/refresh_and_publish.sh"
chmod +x "$TEST_REPO/scripts/refresh_and_publish.sh"

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

set +e
HOME="$TEST_ROOT/home" \
DEGEN_DOGS_REPO_DIR="$TEST_REPO" \
DEGEN_DOGS_LOG_DIR="$TEST_ROOT/logs" \
DEGEN_DOGS_LOCK_DIR="$TEST_ROOT/locks" \
DEGEN_DOGS_LOCK_HELD=1 \
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
DEGEN_DOGS_LOCK_HELD=1 \
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

echo "refresh rollback and full-fallback regression tests passed"
