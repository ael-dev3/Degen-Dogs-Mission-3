#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT/scripts/run_pages_validation.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/degen-dogs-pages-validation.XXXXXX")"
STUB_BIN="$TEST_ROOT/bin"
mkdir -p "$STUB_BIN"

cleanup() {
  local status=$?
  local leaf
  leaf="$(basename -- "$TEST_ROOT")"
  if [[ -d "$TEST_ROOT" && "$leaf" == degen-dogs-pages-validation.* ]]; then
    rm -rf -- "$TEST_ROOT"
  fi
  return "$status"
}
trap cleanup EXIT

cat >"$STUB_BIN/record-check" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail

check="${1:?missing check name}"
printf '%s\n' "$check" >>"${CALLS_FILE:?missing calls file}"
printf 'stub output for %s\n' "$check"

case "$check" in
  npm:test:runner-publish|npm:test:data-cache|npm:test:runner-health|python-bytecode)
    if [[ -n "${BARRIER_DIR:-}" ]]; then
      marker="${check//:/_}"
      : >"$BARRIER_DIR/$marker"
      for ((attempt = 0; attempt < 200; attempt += 1)); do
        marker_count="$(find "$BARRIER_DIR" -type f | wc -l | tr -d ' ')"
        if [[ "$marker_count" -ge 4 ]]; then
          break
        fi
        sleep 0.01
      done
      if [[ "${marker_count:-0}" -lt 4 ]]; then
        printf 'parallel group barrier timed out for %s\n' "$check" >&2
        exit 91
      fi
    fi
    ;;
esac

case ",${FAIL_CHECKS:-}," in
  *",$check,"*) exit "${FAIL_CODE:-17}" ;;
esac
SH

cat >"$STUB_BIN/npm" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "$#" != 2 || "$1" != "run" ]]; then
  printf 'unexpected npm invocation:' >&2
  printf ' %q' "$@" >&2
  printf '\n' >&2
  exit 90
fi
exec "$(dirname "$0")/record-check" "npm:$2"
SH

cat >"$STUB_BIN/python3" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
expected=(
  -m
  py_compile
  scripts/build_dashboard.py
  scripts/build_live_snapshot_bundle.py
  scripts/refresh_current_surface.py
  scripts/watch_mission3_auction.py
  scripts/validate_dashboard_consistency.py
  scripts/degen_dogs_runner_health.py
  scripts/check_remote_freshness.py
)
if [[ "$#" != "${#expected[@]}" ]]; then
  printf 'unexpected python3 invocation\n' >&2
  exit 90
fi
for index in "${!expected[@]}"; do
  position=$((index + 1))
  if [[ "${!position}" != "${expected[$index]}" ]]; then
    printf 'unexpected python3 invocation\n' >&2
    exit 90
  fi
done
exec "$(dirname "$0")/record-check" python-bytecode
SH

chmod 700 "$STUB_BIN/record-check" "$STUB_BIN/npm" "$STUB_BIN/python3"

EXPECTED_CHECKS="$TEST_ROOT/expected-checks"
python3 - "$ROOT/package.json" >"$EXPECTED_CHECKS" <<'PY'
import json
import re
import sys
from pathlib import Path

scripts = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["scripts"]
run_pattern = re.compile(r"npm run ([A-Za-z0-9:_-]+)")


def expand(name: str, stack: tuple[str, ...] = ()) -> list[str]:
    if name in stack:
        raise SystemExit(f"recursive npm script detected: {' -> '.join((*stack, name))}")
    command = scripts.get(name)
    if not isinstance(command, str):
        raise SystemExit(f"missing npm script: {name}")
    parts = command.split(" && ")
    children = [run_pattern.fullmatch(part) for part in parts]
    if children and all(children):
        expanded: list[str] = []
        for match in children:
            assert match is not None
            expanded.extend(expand(match.group(1), (*stack, name)))
        return expanded
    return [f"npm:{name}"]


checks = expand("test:dashboard")
checks.extend(("npm:validate:dashboard", "npm:check:dashboard-ui", "python-bytecode"))
print("\n".join(checks))
PY

assert_all_checks_ran_once() {
  local calls_file="$1"
  LC_ALL=C sort "$calls_file" >"$TEST_ROOT/actual-sorted"
  LC_ALL=C sort "$EXPECTED_CHECKS" >"$TEST_ROOT/expected-sorted"
  if ! cmp -s "$TEST_ROOT/expected-sorted" "$TEST_ROOT/actual-sorted"; then
    printf 'pages validation invocation set mismatch\n' >&2
    diff -u "$TEST_ROOT/expected-sorted" "$TEST_ROOT/actual-sorted" >&2 || true
    exit 1
  fi
}

PASS_CALLS="$TEST_ROOT/pass-calls"
PASS_OUTPUT="$TEST_ROOT/pass-output"
BARRIER_DIR="$TEST_ROOT/barrier"
mkdir "$BARRIER_DIR"
: >"$PASS_CALLS"
CALLS_FILE="$PASS_CALLS" \
BARRIER_DIR="$BARRIER_DIR" \
PATH="$STUB_BIN:/usr/bin:/bin" \
/bin/bash "$RUNNER" >"$PASS_OUTPUT" 2>&1
assert_all_checks_ran_once "$PASS_CALLS"
grep -Fq '[PASS] Publisher safety' "$PASS_OUTPUT"
grep -Fq '[PASS] Data and watcher tests' "$PASS_OUTPUT"
grep -Fq '[PASS] Runner tests' "$PASS_OUTPUT"
grep -Fq '[PASS] Artifact validation' "$PASS_OUTPUT"
grep -Fq 'pages_validation=pass groups=4' "$PASS_OUTPUT"

FAIL_CALLS="$TEST_ROOT/fail-calls"
FAIL_OUTPUT="$TEST_ROOT/fail-output"
: >"$FAIL_CALLS"
if CALLS_FILE="$FAIL_CALLS" \
  FAIL_CHECKS='npm:test:runner-publish,npm:test:validator' \
  FAIL_CODE=17 \
  PATH="$STUB_BIN:/usr/bin:/bin" \
  /bin/bash "$RUNNER" >"$FAIL_OUTPUT" 2>&1; then
  printf 'pages validation runner accepted failing checks\n' >&2
  exit 1
fi
assert_all_checks_ran_once "$FAIL_CALLS"
grep -Fq '[FAIL] npm:test:runner-publish (exit 17)' "$FAIL_OUTPUT"
grep -Fq '[FAIL] npm:test:validator (exit 17)' "$FAIL_OUTPUT"
grep -Fq '[FAIL] Publisher safety (group exit 1)' "$FAIL_OUTPUT"
grep -Fq '[FAIL] Artifact validation (group exit 1)' "$FAIL_OUTPUT"
grep -Fq 'pages validation failed: 2 of 4 groups failed' "$FAIL_OUTPUT"

printf 'pages_validation_runner_tests=pass count=2\n'
