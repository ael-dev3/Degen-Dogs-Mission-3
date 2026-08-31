#!/usr/bin/env python3
"""Static regression checks for the WSL2/systemd runner package."""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

NEW_UNIT_ASSETS = (
    "config/systemd/degen-dogs-publisher.service.in",
    "config/systemd/degen-dogs-publisher.path.in",
    "config/systemd/degen-dogs-publisher.timer",
    "config/systemd/degen-dogs-pages-verifier.service.in",
    "config/systemd/degen-dogs-pages-verifier.path.in",
    "config/systemd/degen-dogs-pages-verifier.timer",
)
NEW_RUNTIME_ASSETS = (
    "scripts/runner_publication_state.py",
    "scripts/drain_publication_queue.py",
    "scripts/verify_pages_deployment.py",
)
ACTIVATION_UNITS = (
    "degen-dogs-runner.target",
    "degen-dogs-watcher.timer",
    "degen-dogs-hourly.timer",
    "degen-dogs-health.timer",
    "degen-dogs-publisher.path",
    "degen-dogs-publisher.timer",
    "degen-dogs-pages-verifier.path",
    "degen-dogs-pages-verifier.timer",
)
SERVICE_UNITS = (
    "degen-dogs-watcher.service",
    "degen-dogs-hourly.service",
    "degen-dogs-health.service",
    "degen-dogs-publisher.service",
    "degen-dogs-pages-verifier.service",
)
NEW_TRIGGER_UNITS = ACTIVATION_UNITS[-4:]
NEW_TRIGGERED_SERVICES = SERVICE_UNITS[-2:]
PRIVILEGED_ISOLATION_FLAG = "--require-rendered-systemd-isolation"
BOOTSTRAP_CORE_TESTS = (
    "scripts/test_runner_publication_state.py",
    "scripts/test_watch_mission3_auction.py",
    "scripts/test_drain_publication_queue.py",
    "scripts/test_verify_pages_deployment.py",
    "scripts/test_refresh_and_publish.sh",
    "scripts/test_refresh_telemetry.py",
    "scripts/test_degen_dogs_runner_health.py",
    "scripts/test_wsl_publication_integration.py",
)


def text(relative: str) -> str:
    path = ROOT / relative
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{relative} must use LF line endings"
    return raw.decode("utf-8", errors="strict")


def powershell_literal_payload(source: str, variable: str) -> str:
    match = re.search(
        rf"(?ms)^[ \t]*\${re.escape(variable)}\s*=\s+@'\r?\n(?P<body>.*?)\r?\n[ \t]*'@\s*$",
        source,
    )
    assert match, f"missing literal PowerShell payload ${variable}"
    return match.group("body")


def powershell_activation_liveness_probes(source: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"(?m)^[ \t]*'(?P<probe>test -f /run/degen-dogs/anchor-ready.*?)'[ \t]*$",
            source,
        )
    )


def bash_array_items(source: str, variable: str) -> tuple[str, ...]:
    match = re.search(
        rf"(?ms)^[ \t]*{re.escape(variable)}=\(\s*(?P<body>.*?)^[ \t]*\)",
        source,
    )
    assert match, f"missing Bash array {variable}"
    return tuple(shlex.split(match.group("body"), comments=True, posix=True))


def run_bash(source: str, *, expected_returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["bash", "-s", "--"],
        input=source.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert result.returncode == expected_returncode, (
        f"bash returncode={result.returncode}, expected={expected_returncode}\n"
        f"stdout={result.stdout.decode('utf-8', errors='replace')}\n"
        f"stderr={result.stderr.decode('utf-8', errors='replace')}"
    )
    return result


def marked_payload(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker) + len(start_marker)
    end = source.index(end_marker, start)
    return source[start:end].strip("\n")


def python_payload_with_marker(source: str, marker: str) -> str:
    marker_offset = source.index(marker)
    payload_start = source.rfind("<<'PY'\n", 0, marker_offset)
    assert payload_start >= 0, f"missing Python heredoc before {marker}"
    payload_start += len("<<'PY'\n")
    payload_end = source.index("\nPY", marker_offset)
    return source[payload_start:payload_end]


def test_bootstrap_receipt_gate(installer: str) -> None:
    core = marked_payload(
        installer,
        "# WSL_BOOTSTRAP_CORE_START",
        "# WSL_BOOTSTRAP_CORE_END",
    )
    for suite in BOOTSTRAP_CORE_TESTS:
        assert installer.count(suite) == 1, f"bootstrap suite is not single-run: {suite}"
    expected_order = "|".join(BOOTSTRAP_CORE_TESTS)
    harness = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT
repo_dir="$test_root/repo"
state_dir="$test_root/state"
tested_receipt_path="$state_dir/bootstrap-test-receipt.json"
legacy_tested_sha_path="$state_dir/tested-main.sha"
expected_head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
trusted_installer_commit=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
bootstrap_receipt_schema_version=1
runner_user=degendogs
mkdir -p "$repo_dir/.venv/bin" "$repo_dir/scripts/runtime-bin" "$state_dir"
printf '#!/bin/sh\nexit 0\n' >"$repo_dir/.venv/bin/python3"
chmod 0755 "$repo_dir/.venv/bin/python3"
calls="$test_root/calls"
fail_needle=''

run_as_runner() {
  printf 'dependency:%s\n' "$*" >>"$calls"
  return 0
}
run_as_runner_runtime() {
  printf 'runtime:%s\n' "$*" >>"$calls"
  if [[ -n "$fail_needle" && "$*" == *"$fail_needle"* ]]; then
    return 73
  fi
  return 0
}
write_bootstrap_receipt() {
  printf 'receipt\n' >"$tested_receipt_path"
}
bootstrap_under_test() {
''' + core + r'''
}

run_case() {
  fail_needle="$1"
  : >"$calls"
  printf 'stale\n' >"$tested_receipt_path"
  printf 'legacy\n' >"$legacy_tested_sha_path"
  set +e
  ( set -Eeuo pipefail; bootstrap_under_test )
  status=$?
  set -e
  printf 'case=%s status=%s receipt=%s legacy=%s\n' \
    "${fail_needle:-success}" "$status" \
    "$([[ -e "$tested_receipt_path" ]] && printf yes || printf no)" \
    "$([[ -e "$legacy_tested_sha_path" ]] && printf yes || printf no)"
}

run_case ''
test -f "$tested_receipt_path"
test ! -e "$legacy_tested_sha_path"
printf 'order='
awk -F: '/^runtime:/ && /scripts\/test_/ {print $2}' "$calls" | \
  sed -E 's#^.*(scripts/test_[^ ]+).*$#\1#' | paste -sd'|' -
for suite in ''' + " ".join(BOOTSTRAP_CORE_TESTS) + r'''; do
  test "$(grep -F -c -- "$suite" "$calls")" = 1
done
test "$(grep -F -c -- "npm --prefix $repo_dir run build" "$calls")" = 1
test "$(grep -F -c -- "npm --prefix $repo_dir ci --ignore-scripts" "$calls")" = 1

for suite in ''' + " ".join(BOOTSTRAP_CORE_TESTS) + r'''; do
  run_case "$suite"
  test ! -e "$tested_receipt_path"
  test ! -e "$legacy_tested_sha_path"
done
run_case 'npm --prefix'
test ! -e "$tested_receipt_path"
test ! -e "$legacy_tested_sha_path"
run_case 'run build'
test ! -e "$tested_receipt_path"
test ! -e "$legacy_tested_sha_path"
'''
    result = run_bash(harness)
    output = result.stdout.decode("utf-8", errors="strict")
    assert "case=success status=0 receipt=yes legacy=no" in output
    assert f"order={expected_order}" in output
    for suite in BOOTSTRAP_CORE_TESTS:
        assert f"case={suite} status=73 receipt=no legacy=no" in output

    assert installer.count('if [[ "$skip_bootstrap" == "1" && "$enable_now" == "1" ]]') == 1
    activation_guard = installer.split(
        'if [[ "$skip_bootstrap" == "1" && "$enable_now" == "1" ]]', 1
    )[1].split("fi", 1)[0]
    assert "validate_bootstrap_receipt" in activation_guard
    assert "tested-main.sha" not in activation_guard

    writer = python_payload_with_marker(installer, "# WSL_BOOTSTRAP_RECEIPT_WRITER")
    validator = python_payload_with_marker(installer, "# WSL_BOOTSTRAP_RECEIPT_VALIDATOR")
    ast.parse(writer)
    ast.parse(validator)
    if os.name == "nt":
        return

    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        receipt = directory / "bootstrap-test-receipt.json"
        runtime_commit = "a" * 40
        trusted_commit = "b" * 40
        schema_version = "1"
        expected_uid = str(os.getuid())

        def write_receipt() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    writer,
                    str(receipt),
                    runtime_commit,
                    trusted_commit,
                    schema_version,
                    expected_uid,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        def validate_receipt() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    validator,
                    str(receipt),
                    runtime_commit,
                    trusted_commit,
                    schema_version,
                    expected_uid,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        first = write_receipt()
        assert first.returncode == 0, first.stderr.decode("utf-8", errors="replace")
        first_inode = receipt.stat().st_ino
        expected_record = {
            "runtime_commit": runtime_commit,
            "schema_version": 1,
            "trusted_installer_commit": trusted_commit,
        }
        assert json.loads(receipt.read_text(encoding="utf-8")) == expected_record
        assert receipt.stat().st_mode & 0o777 == 0o600
        assert receipt.stat().st_nlink == 1
        assert not list(directory.glob(".bootstrap-test-receipt.json.*"))
        accepted = validate_receipt()
        assert accepted.returncode == 0, accepted.stderr.decode("utf-8", errors="replace")

        second = write_receipt()
        assert second.returncode == 0, second.stderr.decode("utf-8", errors="replace")
        assert receipt.stat().st_ino != first_inode, "receipt replacement was not atomic"

        mutations = (
            b"a" * 40 + b"\n",
            b"{}\n",
            b'{"runtime_commit":"' + b"a" * 40 + b'"}\n',
            json.dumps({**expected_record, "schema_version": 0}).encode() + b"\n",
            json.dumps({**expected_record, "schema_version": True}).encode() + b"\n",
            json.dumps({**expected_record, "runtime_commit": "c" * 40}).encode() + b"\n",
            json.dumps({**expected_record, "trusted_installer_commit": "c" * 40}).encode() + b"\n",
            json.dumps({**expected_record, "extra": "field"}).encode() + b"\n",
            b"{" + b"x" * 1024 + b"}\n",
        )
        for mutation in mutations:
            receipt.write_bytes(mutation)
            receipt.chmod(0o600)
            rejected = validate_receipt()
            assert rejected.returncode != 0, f"unsafe receipt accepted: {mutation[:80]!r}"

        assert write_receipt().returncode == 0
        receipt.chmod(0o644)
        assert validate_receipt().returncode != 0, "non-private receipt was accepted"
        receipt.chmod(0o600)
        hard_link = directory / "receipt-link.json"
        os.link(receipt, hard_link)
        assert validate_receipt().returncode != 0, "multiply-linked receipt was accepted"
        hard_link.unlink()
        receipt.unlink()
        receipt.symlink_to(directory / "missing-target")
        assert validate_receipt().returncode != 0, "receipt symlink was accepted"


def run_checkout_attestation(
    attestation: str,
    trusted: Path,
    checkout: Path,
    *,
    mode_overrides: dict[Path, int] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory() as harness_directory:
        harness = Path(harness_directory)
        script = harness / "attest.py"
        script.write_text(attestation, encoding="utf-8", newline="\n")
        environment = os.environ.copy()
        if mode_overrides:
            serialized_modes = {
                str(path.resolve()).casefold(): mode
                for path, mode in mode_overrides.items()
            }
            (harness / "sitecustomize.py").write_text(
                """import json
import os
import pathlib

_real_lstat = pathlib.Path.lstat
_mode_overrides = json.loads(os.environ["DEGEN_DOGS_TEST_MODE_OVERRIDES"])


def _lstat_with_test_mode(path):
    details = _real_lstat(path)
    mode = _mode_overrides.get(str(path.resolve()).casefold())
    if mode is None:
        return details
    values = list(details)
    values[0] = (details.st_mode & ~0o777) | int(mode)
    return os.stat_result(values)


pathlib.Path.lstat = _lstat_with_test_mode
""",
                encoding="utf-8",
                newline="\n",
            )
            environment["DEGEN_DOGS_TEST_MODE_OVERRIDES"] = json.dumps(serialized_modes)
            environment["PYTHONPATH"] = str(harness)
        return subprocess.run(
            [sys.executable, str(script), str(trusted), str(checkout)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            env=environment,
        )


def checkout_attestation_result_for_modes(
    attestation: str,
    trusted_mode: int,
    checkout_mode: int,
) -> subprocess.CompletedProcess[bytes]:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        trusted = root / "trusted"
        checkout = root / "checkout"
        trusted.mkdir()
        checkout.mkdir()
        trusted_asset = trusted / "asset.sh"
        checkout_asset = checkout / "asset.sh"
        trusted_asset.write_bytes(b"#!/bin/sh\nexit 0\n")
        checkout_asset.write_bytes(trusted_asset.read_bytes())
        trusted_asset.chmod(trusted_mode)
        checkout_asset.chmod(checkout_mode)
        overrides = None
        if os.name == "nt":
            overrides = {
                trusted_asset: trusted_mode,
                checkout_asset: checkout_mode,
            }
        return run_checkout_attestation(
            attestation,
            trusted,
            checkout,
            mode_overrides=overrides,
        )


def assert_posix_umask_fast_forward_attests(attestation: str) -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        source = root / "source"
        checkout = root / "checkout"
        trusted = root / "trusted"
        source.mkdir()
        git_environment = os.environ.copy()
        git_environment.update(
            {
                "GIT_AUTHOR_EMAIL": "runner-test@example.invalid",
                "GIT_AUTHOR_NAME": "Runner Test",
                "GIT_COMMITTER_EMAIL": "runner-test@example.invalid",
                "GIT_COMMITTER_NAME": "Runner Test",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(source)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment,
        )
        (source / "README").write_text("initial\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "-m", "initial"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment,
        )
        subprocess.run(
            ["git", "clone", "--quiet", str(source), str(checkout)],
            check=True,
            env=git_environment,
        )
        executable = source / "runner.sh"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
        subprocess.run(["git", "-C", str(source), "add", "runner.sh"], check=True)
        subprocess.run(
            ["git", "-C", str(source), "commit", "-m", "add executable"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment,
        )
        subprocess.run(
            [
                "bash",
                "-c",
                'umask 077; git -C "$1" pull --ff-only --quiet',
                "--",
                str(checkout),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_environment,
        )
        checkout_executable = checkout / "runner.sh"
        assert checkout_executable.stat().st_mode & 0o777 == 0o700
        archive = subprocess.run(
            ["git", "-C", str(source), "archive", "HEAD"],
            check=True,
            stdout=subprocess.PIPE,
            env=git_environment,
        ).stdout
        trusted.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as archive_file:
            archive_file.extractall(trusted)
        extracted_mode = (trusted / "runner.sh").stat().st_mode & 0o777
        git_version = subprocess.run(
            ["git", "--version"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        if git_version.startswith("git version 2.43."):
            assert extracted_mode == 0o775
        else:
            assert extracted_mode in {0o755, 0o775}
        subprocess.run(["chmod", "-R", "go-w", str(trusted)], check=True)
        assert (trusted / "runner.sh").stat().st_mode & 0o777 == 0o755
        accepted = run_checkout_attestation(attestation, trusted, checkout)
        assert accepted.returncode == 0, accepted.stderr.decode("utf-8", errors="replace")


def test_checkout_attestation_modes(installer: str) -> None:
    attestation_marker = '/usr/bin/python3 - "$runtime_tree" "$repo_dir" <<\'PY\'\n'
    attestation_start = installer.index(attestation_marker) + len(attestation_marker)
    attestation_end = installer.index("\nPY\n", attestation_start)
    checkout_attestation = installer[attestation_start:attestation_end]

    owner_execute_preserved = checkout_attestation_result_for_modes(
        checkout_attestation,
        trusted_mode=0o755,
        checkout_mode=0o700,
    )
    assert owner_execute_preserved.returncode == 0, owner_execute_preserved.stderr.decode(
        "utf-8", errors="replace"
    )

    rejected_mode_cases = (
        (0o755, 0o600, "trusted executable without checkout owner execute"),
        (0o755, 0o611, "checkout group/other execute without owner execute"),
        (0o644, 0o744, "checkout owner execute for trusted non-executable"),
    )
    for trusted_mode, checkout_mode, label in rejected_mode_cases:
        rejected = checkout_attestation_result_for_modes(
            checkout_attestation,
            trusted_mode=trusted_mode,
            checkout_mode=checkout_mode,
        )
        assert rejected.returncode != 0, f"attestation accepted {label}"
        assert b"runner executable mode differs from trusted commit" in rejected.stderr
    assert_posix_umask_fast_forward_attests(checkout_attestation)


def test_health_timer_activation(powershell: str) -> None:
    commit_activation = powershell_literal_payload(powershell, "commitActivation")
    expected_activation_calls = """is-enabled --quiet degen-dogs-runner.target
is-enabled --quiet degen-dogs-watcher.timer
is-enabled --quiet degen-dogs-hourly.timer
is-enabled --quiet degen-dogs-health.timer
is-enabled --quiet degen-dogs-publisher.path
is-enabled --quiet degen-dogs-publisher.timer
is-enabled --quiet degen-dogs-pages-verifier.path
is-enabled --quiet degen-dogs-pages-verifier.timer
start degen-dogs-runner.target degen-dogs-watcher.timer degen-dogs-hourly.timer degen-dogs-publisher.path degen-dogs-publisher.timer degen-dogs-pages-verifier.path degen-dogs-pages-verifier.timer
restart degen-dogs-health.timer
is-active --quiet degen-dogs-runner.target
is-active --quiet degen-dogs-watcher.timer
is-active --quiet degen-dogs-hourly.timer
is-active --quiet degen-dogs-health.timer
is-active --quiet degen-dogs-publisher.path
is-active --quiet degen-dogs-publisher.timer
is-active --quiet degen-dogs-pages-verifier.path
is-active --quiet degen-dogs-pages-verifier.timer
show --property=LoadState --value degen-dogs-publisher.service
is-failed --quiet degen-dogs-publisher.service
show --property=LoadState --value degen-dogs-pages-verifier.service
is-failed --quiet degen-dogs-pages-verifier.service
show --property=NextElapseUSecMonotonic --value degen-dogs-health.timer
"""

    def health_timer_activation_regression(
        next_elapse: str,
        inactive_unit: str,
        expected_returncode: int,
        *,
        verify_complete_calls: bool,
    ) -> None:
        harness = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
trap 'command rm -rf -- "$test_root"' EXIT
calls="$test_root/calls"
cat >"$test_root/expected" <<'EXPECTED_CALLS'
''' + expected_activation_calls + r'''EXPECTED_CALLS
next_elapse=''' + shlex.quote(next_elapse) + r'''
inactive_unit=''' + shlex.quote(inactive_unit) + r'''
systemctl() {
  printf '%s\n' "$*" >>"$calls"
  case "${1:-}" in
    is-enabled|start|restart) return 0 ;;
    is-active)
      shift
      [[ "${1:-}" == --quiet ]] && shift
      for queried_unit in "$@"; do
        [[ "$queried_unit" != "$inactive_unit" ]] && return 0
      done
      return 3
      ;;
    is-failed) return 1 ;;
    show)
      if [[ "$*" == "show --property=LoadState --value "* ]]; then printf 'loaded\n'; else printf '%s\n' "$next_elapse"; fi
      ;;
    *) return 97 ;;
  esac
}
install() { return 0; }
mktemp() {
  case "${1:-}" in
    /var/lib/degen-dogs/*) printf '%s\n' "$test_root/armed.tmp" ;;
    /run/degen-dogs/*) printf '%s\n' "$test_root/active.tmp" ;;
    *) return 98 ;;
  esac
}
export calls next_elapse inactive_unit test_root
export -f systemctl install mktemp
cat >"$test_root/commit-activation.sh" <<'COMMIT_ACTIVATION'
''' + commit_activation + r'''
COMMIT_ACTIVATION
set +e
bash -Eeuo pipefail "$test_root/commit-activation.sh"
status=$?
set -e
if [[ "$status" != "''' + str(expected_returncode) + r'''" ]]; then
  printf 'expected activation status ''' + str(expected_returncode) + r''' with inactive unit %s, got %s\n' \
    "${inactive_unit:-none}" "$status" >&2
  exit 96
fi
if [[ "''' + ("1" if verify_complete_calls else "0") + r'''" == 1 ]] && ! cmp -s "$test_root/expected" "$calls"; then
  diff -u "$test_root/expected" "$calls" >&2 || true
  exit 99
fi
printf 'health-timer-activation-checked inactive=%s status=%s\n' "${inactive_unit:-none}" "$status"
'''
        result = run_bash(harness)
        assert b"health-timer-activation-checked" in result.stdout

    health_timer_activation_regression("5min", "", 0, verify_complete_calls=True)
    for inactive_unit in ACTIVATION_UNITS:
        health_timer_activation_regression(
            "5min",
            inactive_unit,
            3,
            verify_complete_calls=False,
        )
    health_timer_activation_regression("0", "", 1, verify_complete_calls=True)


def test_activation_liveness_probes_reject_each_inactive_unit(powershell: str) -> None:
    probes = powershell_activation_liveness_probes(powershell)
    assert len(probes) == 2
    for probe_number, probe in enumerate(probes, start=1):
        harness = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
trap 'command rm -rf -- "$test_root"' EXIT
cat >"$test_root/probe.sh" <<'ACTIVATION_PROBE'
''' + probe + r'''
ACTIVATION_PROBE
test() {
  if [[ "$#" == 2 && "$1" == -f ]]; then return 0; fi
  builtin test "$@"
}
systemctl() {
  case "${1:-}" in
    is-active)
      shift
      [[ "${1:-}" == --quiet ]] && shift
      for queried_unit in "$@"; do
        [[ "$queried_unit" != "$inactive_unit" ]] && return 0
      done
      return 3
      ;;
    show) printf 'loaded\n' ;;
    is-failed) return 1 ;;
    *) return 97 ;;
  esac
}
export -f test systemctl
run_probe() {
  inactive_unit="$1"
  expected_status="$2"
  export inactive_unit
  set +e
  bash -Eeuo pipefail "$test_root/probe.sh"
  status=$?
  set -e
  if [[ "$status" != "$expected_status" ]]; then
    printf 'probe ''' + str(probe_number) + r''' expected status %s with inactive unit %s, got %s\n' \
      "$expected_status" "${inactive_unit:-none}" "$status" >&2
    exit 96
  fi
}
run_probe '' 0
for inactive_unit in ''' + " ".join(ACTIVATION_UNITS) + r'''; do
  run_probe "$inactive_unit" 1
done
printf 'activation-liveness-probe-checked probe=''' + str(probe_number) + r'''\n'
'''
        result = run_bash(harness)
        assert b"activation-liveness-probe-checked" in result.stdout


def test_wsl_launcher_policy(launcher: str, env_loader: str) -> None:
    regression = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT
repo_dir="$test_root/repo"
runner_home="$test_root/home"
mkdir -p "$repo_dir/scripts/runtime-bin" "$runner_home/.ssh" "$test_root/log" "$test_root/lock"

cat >"$repo_dir/scripts/run_wsl_runner_job.sh" <<'LAUNCHER'
''' + launcher + r'''
LAUNCHER
cat >"$repo_dir/scripts/load_runner_env.sh" <<'ENV_LOADER'
''' + env_loader + r'''
ENV_LOADER
cat >"$repo_dir/scripts/runtime-bin/git" <<'FAKE_GIT'
#!/usr/bin/env bash
set -Eeuo pipefail
case "$*" in
  'branch --show-current') printf 'main\n' ;;
  'remote get-url origin'|'remote get-url --push --all origin')
    printf 'git@github-degen-dogs:ael-dev3/Degen-Dogs-Mission-3.git\n'
    ;;
  'config --local --get-all remote.origin.pushurl') ;;
  'config --local --get core.sshCommand')
    printf 'ssh -F %s/.ssh/degen_dogs_config\n' "$HOME"
    ;;
  'config --local --get core.hooksPath') printf '/dev/null\n' ;;
  *) printf 'unexpected git call: %s\n' "$*" >&2; exit 97 ;;
esac
FAKE_GIT
cat >"$repo_dir/scripts/refresh_and_publish.sh" <<'FAKE_PUBLISHER'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'hourly|%s|%s|%s|%s|%s\n' \
  "$DEGEN_DOGS_RUN_MISSION3_ARCHIVE" \
  "$DEGEN_DOGS_REMOTE" \
  "$DEGEN_DOGS_BRANCH" \
  "$DEGEN_DOGS_SKIP_PUSH" \
  "$DEGEN_DOGS_SKIP_PULL"
FAKE_PUBLISHER
cat >"$repo_dir/scripts/watch_mission3_onchain_activity.py" <<'FAKE_WATCHER'
import os

print(
    "watcher|{}|{}|{}|{}|{}|{}".format(
        os.environ["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"],
        os.environ["DEGEN_DOGS_REMOTE"],
        os.environ["DEGEN_DOGS_BRANCH"],
        os.environ["DEGEN_DOGS_SKIP_PUSH"],
        os.environ["DEGEN_DOGS_SKIP_PULL"],
        os.environ["MISSION3_WATCHER_PUBLICATION_MODE"],
    )
)
FAKE_WATCHER
cat >"$repo_dir/scripts/drain_publication_queue.py" <<'FAKE_DRAINER'
import os

scrubbed = (
    "MISSION3_REFRESH_COMMAND",
    "DEGEN_DOGS_PUBLICATION_OUTCOME",
    "DEGEN_DOGS_RAW_COMMIT_URL",
    "DEGEN_DOGS_REFRESH_TELEMETRY_PATH",
)
assert not any(name in os.environ for name in scrubbed), scrubbed
print(
    "publisher|{}|{}|{}".format(
        os.environ["DEGEN_DOGS_REPO_DIR"],
        os.environ["DEGEN_DOGS_LOG_DIR"],
        os.environ["DEGEN_DOGS_LOCK_DIR"],
    )
)
FAKE_DRAINER
cat >"$repo_dir/scripts/verify_pages_deployment.py" <<'FAKE_VERIFIER'
import os

scrubbed = (
    "MISSION3_REFRESH_COMMAND",
    "DEGEN_DOGS_PUBLICATION_OUTCOME",
    "DEGEN_DOGS_RAW_COMMIT_URL",
    "DEGEN_DOGS_REFRESH_TELEMETRY_PATH",
)
assert not any(name in os.environ for name in scrubbed), scrubbed
print(
    "verifier|{}|{}|{}".format(
        os.environ["DEGEN_DOGS_REPO_DIR"],
        os.environ["DEGEN_DOGS_LOG_DIR"],
        os.environ["DEGEN_DOGS_LOCK_DIR"],
    )
)
FAKE_VERIFIER
cat >"$repo_dir/scripts/check_wsl_runner_health.py" <<'FAKE_HEALTH'
import os

print("health|{}".format(os.environ["MISSION3_PUBLICATION_MODE"]))
FAKE_HEALTH
chmod 0755 "$repo_dir/scripts/run_wsl_runner_job.sh" "$repo_dir/scripts/runtime-bin/git"

write_env() {
  cat >"$repo_dir/.env.local" <<'COMMON_ENV'
BASE_RPC_URLS=https://rpc-one.invalid,https://rpc-two.invalid
BASE_LOG_RPC_URLS=https://logs-one.invalid,https://logs-two.invalid
BASE_RPC_QUORUM_SIZE=2
DEGEN_DOGS_REMOTE=attacker
DEGEN_DOGS_BRANCH=attacker
DEGEN_DOGS_SKIP_PUSH=1
DEGEN_DOGS_SKIP_PULL=1
MISSION3_REFRESH_COMMAND=/attacker/command
DEGEN_DOGS_PUBLICATION_OUTCOME=attacker-outcome
DEGEN_DOGS_RAW_COMMIT_URL=https://attacker.invalid/raw
DEGEN_DOGS_REFRESH_TELEMETRY_PATH=/attacker/telemetry
COMMON_ENV
  if (( $# > 0 )); then
    printf '%s\n' "$1" >>"$repo_dir/.env.local"
  fi
  chmod 0600 "$repo_dir/.env.local"
}

run_job() {
  env -u DEGEN_DOGS_RUN_MISSION3_ARCHIVE \
    HOME="$runner_home" \
    DEGEN_DOGS_REPO_DIR="$repo_dir" \
    DEGEN_DOGS_LOG_DIR="$test_root/log" \
    DEGEN_DOGS_LOCK_DIR="$test_root/lock" \
    DEGEN_DOGS_ENV_FILE="$repo_dir/.env.local" \
    /bin/bash -p "$repo_dir/scripts/run_wsl_runner_job.sh" "$1"
}

write_env 'DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0'
test "$(run_job hourly)" = 'hourly|0|origin|main|0|0'

write_env
test "$(run_job hourly)" = 'hourly|1|origin|main|0|0'

for invalid_value in 2 ''; do
  write_env "DEGEN_DOGS_RUN_MISSION3_ARCHIVE=$invalid_value"
  set +e
  invalid_output=$(run_job hourly 2>&1)
  invalid_status=$?
  set -e
  test "$invalid_status" = 78
  test "$invalid_output" = 'error: DEGEN_DOGS_RUN_MISSION3_ARCHIVE must be 0 or 1'
done

write_env 'DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1'
test "$(run_job watcher)" = 'watcher|0|origin|main|0|0|queue'
test "$(run_job publisher)" = "publisher|$repo_dir|$test_root/log|$test_root/lock"
test "$(run_job verifier)" = "verifier|$repo_dir|$test_root/log|$test_root/lock"
test "$(run_job health)" = 'health|queue'
printf 'wsl-launcher-policy-checked\n'
'''
    result = run_bash(regression)
    assert result.stdout == b"wsl-launcher-policy-checked\n"


def systemd_values(source: str, directive: str) -> tuple[str, ...]:
    return tuple(
        match.group(1)
        for match in re.finditer(rf"(?m)^{re.escape(directive)}=(.*)$", source)
    )


def test_queued_worker_units() -> None:
    publisher_service = text("config/systemd/degen-dogs-publisher.service.in")
    verifier_service = text("config/systemd/degen-dogs-pages-verifier.service.in")
    common_service_lines = (
        "ConditionPathExists=/run/degen-dogs/activation-enabled",
        "User=@RUNNER_USER@",
        "Group=@RUNNER_GROUP@",
        "WorkingDirectory=@REPO_DIR@",
        "UMask=0077",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "CapabilityBoundingSet=",
        "KillMode=control-group",
        "TimeoutStopSec=30s",
    )
    for service in (publisher_service, verifier_service):
        for line in common_service_lines:
            assert service.count(line) == 1, line
        assert "Restart=" not in service
        assert "ExecStart=/bin/bash -c" not in service

    assert systemd_values(publisher_service, "ExecStart") == (
        "/bin/bash -p @REPO_DIR@/scripts/run_wsl_runner_job.sh publisher",
    )
    assert systemd_values(publisher_service, "TimeoutStartSec") == ("5min",)
    assert systemd_values(publisher_service, "ReadWritePaths") == (
        "@REPO_DIR@ @LOG_DIR@ @LOCK_DIR@",
    )
    assert not systemd_values(publisher_service, "ReadOnlyPaths")

    assert systemd_values(verifier_service, "ExecStart") == (
        "/bin/bash -p @REPO_DIR@/scripts/run_wsl_runner_job.sh verifier",
    )
    assert systemd_values(verifier_service, "TimeoutStartSec") == ("6min",)
    assert systemd_values(verifier_service, "SuccessExitStatus") == ("2",)
    assert systemd_values(verifier_service, "ReadOnlyPaths") == ("@REPO_DIR@",)
    assert systemd_values(verifier_service, "ReadWritePaths") == (
        "@LOG_DIR@ @LOCK_DIR@",
    )
    assert "ReadWritePaths=@REPO_DIR@" not in verifier_service

    path_expectations = {
        "config/systemd/degen-dogs-publisher.path.in": (
            "@LOCK_DIR@/publication/latest.json",
            "degen-dogs-publisher.service",
        ),
        "config/systemd/degen-dogs-pages-verifier.path.in": (
            "@LOCK_DIR@/publication/pending.json",
            "degen-dogs-pages-verifier.service",
        ),
    }
    for relative, (watched_leaf, service_name) in path_expectations.items():
        path_unit = text(relative)
        assert systemd_values(path_unit, "PathChanged") == (watched_leaf,)
        assert systemd_values(path_unit, "Unit") == (service_name,)
        assert not systemd_values(path_unit, "ConditionPathExists")
        assert "DirectoryNotEmpty=" not in path_unit
        assert not systemd_values(path_unit, "PathExists")
        assert "*" not in watched_leaf

    timer_expectations = {
        "config/systemd/degen-dogs-publisher.timer": (
            "degen-dogs-publisher.service",
            "10s",
            "1s",
            "1s",
        ),
        "config/systemd/degen-dogs-pages-verifier.timer": (
            "degen-dogs-pages-verifier.service",
            "50s",
            "5s",
            "5s",
        ),
    }
    for relative, (service_name, inactive, accuracy, jitter) in timer_expectations.items():
        timer = text(relative)
        assert systemd_values(timer, "Unit") == (service_name,)
        assert systemd_values(timer, "Persistent") == ("false",)
        assert systemd_values(timer, "OnUnitInactiveSec") == (inactive,)
        assert systemd_values(timer, "AccuracySec") == (accuracy,)
        assert systemd_values(timer, "RandomizedDelaySec") == (jitter,)
        assert len(systemd_values(timer, "OnBootSec")) == 1
        assert not systemd_values(timer, "OnCalendar")

    target = text("config/systemd/degen-dogs-runner.target")
    assert systemd_values(target, "Wants") == (" ".join(NEW_TRIGGER_UNITS),)


def test_queued_worker_lifecycle_inventories() -> None:
    installer = text("scripts/install_wsl_runner.sh")
    assert bash_array_items(installer, "activation_unit_names") == ACTIVATION_UNITS
    assert bash_array_items(installer, "service_unit_names") == SERVICE_UNITS
    assert bash_array_items(installer, "unit_names") == (
        "${activation_unit_names[@]}",
        "${service_unit_names[@]}",
    )
    expected_trusted_assets = (
        "scripts/install_wsl_runner.sh",
        "scripts/run_wsl_runner_anchor.sh",
        "config/wsl-runner.env.template",
        "config/logrotate/degen-dogs-wsl.in",
        "config/systemd/degen-dogs-watcher.service.in",
        "config/systemd/degen-dogs-watcher.timer",
        "config/systemd/degen-dogs-hourly.service.in",
        "config/systemd/degen-dogs-hourly.timer",
        "config/systemd/degen-dogs-health.service.in",
        "config/systemd/degen-dogs-health.timer",
        "config/systemd/degen-dogs-runner.target",
        *NEW_UNIT_ASSETS,
        *NEW_RUNTIME_ASSETS,
    )
    assert bash_array_items(installer, "trusted_root_assets") == expected_trusted_assets
    assert bash_array_items(installer, "rendered_unit_names") == (
        "degen-dogs-watcher.service",
        "degen-dogs-hourly.service",
        "degen-dogs-health.service",
        "degen-dogs-publisher.service",
        "degen-dogs-publisher.path",
        "degen-dogs-pages-verifier.service",
        "degen-dogs-pages-verifier.path",
    )
    assert bash_array_items(installer, "copied_unit_names") == (
        "degen-dogs-watcher.timer",
        "degen-dogs-hourly.timer",
        "degen-dogs-health.timer",
        "degen-dogs-publisher.timer",
        "degen-dogs-pages-verifier.timer",
        "degen-dogs-runner.target",
    )
    assert installer.count('systemctl disable --now "${activation_unit_names[@]}"') == 4
    assert installer.count('systemctl stop "${service_unit_names[@]}"') == 4
    assert installer.count('for old_unit in "${unit_names[@]}"') == 2
    assert 'systemctl enable "${activation_unit_names[@]}"' in installer
    assert 'systemd-analyze verify "${verify_units[@]}"' in installer

    anchor = text("scripts/run_wsl_runner_anchor.sh")
    assert bash_array_items(anchor, "units") == ACTIVATION_UNITS
    assert bash_array_items(anchor, "triggered_services") == NEW_TRIGGERED_SERVICES
    assert 'systemctl show --property=LoadState --value "$unit"' in anchor
    assert 'systemctl is-failed --quiet "$unit"' in anchor

    powershell = text("scripts/install_wsl_startup_task.ps1")
    trusted_stage = powershell_literal_payload(powershell, "trustedBundleProvision")
    assert bash_array_items(trusted_stage, "required") == expected_trusted_assets
    runtime_stage = powershell_literal_payload(powershell, "runtimeStage")
    assert bash_array_items(runtime_stage, "runtime_required_assets") == (
        *NEW_UNIT_ASSETS,
        *NEW_RUNTIME_ASSETS,
    )
    for payload_name in ("uninstallScript", "quiesce", "rollbackPublisher"):
        payload = powershell_literal_payload(powershell, payload_name)
        assert bash_array_items(payload, "activation_units") == ACTIVATION_UNITS
        assert bash_array_items(payload, "service_units") == SERVICE_UNITS
    activation = powershell_literal_payload(powershell, "commitActivation")
    assert bash_array_items(activation, "activation_units") == ACTIVATION_UNITS
    assert bash_array_items(activation, "triggered_services") == NEW_TRIGGERED_SERVICES


def test_rendered_verifier_systemd_isolation() -> None:
    test_id = uuid.uuid4().hex
    unit_name = f"degen-dogs-pages-verifier-isolation-test-{test_id}.service"
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        assert wsl, "rendered systemd isolation requires wsl.exe"
        distro = "DegenDogsRunner"
        converted = subprocess.run(
            [
                wsl,
                "-d",
                distro,
                "-u",
                "root",
                "--",
                "/bin/bash",
                "-c",
                'IFS= read -r value; wslpath -a -u "$value"',
            ],
            check=True,
            input=str(ROOT) + "\n",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        ).stdout.strip()
        command = [wsl, "-d", distro, "-u", "root", "--", "/bin/bash", "-s", "--", converted, unit_name]
    else:
        assert os.geteuid() == 0, "rendered systemd isolation requires root"
        command = ["/bin/bash", "-s", "--", str(ROOT), unit_name]

    harness = r'''
set -Eeuo pipefail
source_root="$1"
unit_name="$2"
cleanup_status=0
cleanup() {
  prior_status=$?
  trap - EXIT
  if (( prior_status != 0 )); then
    journalctl --no-pager -n 20 -u "$unit_name" >&2 || true
  fi
  systemctl stop "$unit_name" >/dev/null 2>&1 || true
  systemctl reset-failed "$unit_name" >/dev/null 2>&1 || true
  load_state=$(systemctl show --property=LoadState --value "$unit_name" 2>/dev/null || true)
  if (( prior_status != 0 )) && [[ -n "${run_output:-}" && -f "$run_output" ]]; then
    sed 's/^/systemd-run: /' "$run_output" >&2 || true
  fi
  case "$test_root" in
    /srv/degen-dogs-task7.??????) rm -rf -- "$test_root" ;;
    *) printf 'error: refusing unsafe isolation cleanup path: %s\n' "$test_root" >&2; cleanup_status=1 ;;
  esac
  if [[ -e "$test_root" || -L "$test_root" ]]; then cleanup_status=1; fi
  if [[ -n "$load_state" && "$load_state" != "not-found" ]]; then cleanup_status=1; fi
  printf 'rendered-verifier-isolation-cleanup unit=%s load=%s temp_absent=%s\n' \
    "$unit_name" "${load_state:-not-found}" "$([[ ! -e "$test_root" ]] && printf yes || printf no)"
  if (( prior_status != 0 )); then exit "$prior_status"; fi
  exit "$cleanup_status"
}
test_root=$(mktemp -d /srv/degen-dogs-task7.XXXXXX)
trap cleanup EXIT
if [[ "${3:-}" == force-prerequisite-failure ]]; then
  printf 'forced isolation prerequisite failure\n' >&2
  false
fi
unit_dir="$test_root/units"
repo_dir="$test_root/repo"
log_dir="$test_root/log"
lock_dir="$test_root/lock"
run_output="$test_root/systemd-run.log"
test_user=degendogs
test_group=$(id -gn "$test_user")
test "$(id -u "$test_user")" != 0

test "$(ps -p 1 -o comm=)" = systemd
test "$(stat -f -c %T /srv)" = 'ext2/ext3'
mkdir -p "$unit_dir" "$repo_dir/scripts" "$log_dir" "$lock_dir/publication"
chmod 0711 "$test_root"
cat >"$repo_dir/scripts/run_wsl_runner_job.sh" <<'FAKE_LAUNCHER'
#!/usr/bin/env bash
set -Eeuo pipefail
test "${1:-}" = verifier
printf 'started\n' >"$DEGEN_DOGS_LOG_DIR/isolation-started"
printf 'forbidden\n' >"$DEGEN_DOGS_REPO_DIR/forbidden-write"
printf 'escaped\n' >"$DEGEN_DOGS_LOG_DIR/isolation-escaped"
FAKE_LAUNCHER
chmod 0755 "$repo_dir/scripts/run_wsl_runner_job.sh"
chown -R root:root "$test_root"
chown "$test_user:$test_group" "$repo_dir" "$log_dir" "$lock_dir" "$lock_dir/publication"
chmod 0711 "$test_root"
chmod 0755 "$repo_dir" "$repo_dir/scripts"
chmod 0700 "$log_dir" "$lock_dir" "$lock_dir/publication"
chmod 0755 "$repo_dir/scripts/run_wsl_runner_job.sh"

render() {
  sed \
    -e "s|@RUNNER_USER@|$test_user|g" \
    -e "s|@RUNNER_GROUP@|$test_group|g" \
    -e 's|@RUNNER_HOME@|/nonexistent|g' \
    -e "s|@REPO_DIR@|$repo_dir|g" \
    -e "s|@LOG_DIR@|$log_dir|g" \
    -e "s|@LOCK_DIR@|$lock_dir|g" \
    -e "s|@ENV_FILE@|$repo_dir/.env.local|g" \
    -e 's|@RUNNER_ID@|isolation-test|g' \
    "$1" >"$2"
}
render "$source_root/config/systemd/degen-dogs-publisher.service.in" "$unit_dir/degen-dogs-publisher.service"
render "$source_root/config/systemd/degen-dogs-publisher.path.in" "$unit_dir/degen-dogs-publisher.path"
cp "$source_root/config/systemd/degen-dogs-publisher.timer" "$unit_dir/degen-dogs-publisher.timer"
render "$source_root/config/systemd/degen-dogs-pages-verifier.service.in" "$unit_dir/degen-dogs-pages-verifier.service"
render "$source_root/config/systemd/degen-dogs-pages-verifier.path.in" "$unit_dir/degen-dogs-pages-verifier.path"
cp "$source_root/config/systemd/degen-dogs-pages-verifier.timer" "$unit_dir/degen-dogs-pages-verifier.timer"
cp "$source_root/config/systemd/degen-dogs-runner.target" "$unit_dir/degen-dogs-runner.target"
chmod 0644 "$unit_dir"/*
if grep -R '@[A-Z_]*@' "$unit_dir"; then
  printf 'error: rendered unit retained a placeholder\n' >&2
  exit 81
fi
systemd-analyze verify \
  "$unit_dir/degen-dogs-publisher.service" \
  "$unit_dir/degen-dogs-publisher.path" \
  "$unit_dir/degen-dogs-publisher.timer" \
  "$unit_dir/degen-dogs-pages-verifier.service" \
  "$unit_dir/degen-dogs-pages-verifier.path" \
  "$unit_dir/degen-dogs-pages-verifier.timer" \
  "$unit_dir/degen-dogs-runner.target"

rendered="$unit_dir/degen-dogs-pages-verifier.service"
directive() { sed -n "s/^$1=//p" "$rendered"; }
test "$(directive ExecStart)" = "/bin/bash -p $repo_dir/scripts/run_wsl_runner_job.sh verifier"
test "$(directive ReadOnlyPaths)" = "$repo_dir"
test "$(directive ReadWritePaths)" = "$log_dir $lock_dir"

properties=(
  "--property=UMask=$(directive UMask)"
  "--property=NoNewPrivileges=$(directive NoNewPrivileges)"
  "--property=PrivateTmp=$(directive PrivateTmp)"
  "--property=ProtectSystem=$(directive ProtectSystem)"
  "--property=ProtectHome=$(directive ProtectHome)"
  "--property=ReadOnlyPaths=$(directive ReadOnlyPaths)"
  "--property=ReadWritePaths=$(directive ReadWritePaths)"
  "--property=ProtectKernelTunables=$(directive ProtectKernelTunables)"
  "--property=ProtectKernelModules=$(directive ProtectKernelModules)"
  "--property=ProtectControlGroups=$(directive ProtectControlGroups)"
  "--property=RestrictSUIDSGID=$(directive RestrictSUIDSGID)"
  "--property=RestrictAddressFamilies=$(directive RestrictAddressFamilies)"
  "--property=CapabilityBoundingSet=$(directive CapabilityBoundingSet)"
  "--property=KillMode=$(directive KillMode)"
  "--property=TimeoutStartSec=$(directive TimeoutStartSec)"
  "--property=TimeoutStopSec=$(directive TimeoutStopSec)"
)
set +e
systemd-run --quiet --wait --collect --unit "$unit_name" \
  --uid="$test_user" --gid="$test_group" --working-directory="$repo_dir" \
  --setenv="DEGEN_DOGS_REPO_DIR=$repo_dir" \
  --setenv="DEGEN_DOGS_LOG_DIR=$log_dir" \
  --setenv="DEGEN_DOGS_LOCK_DIR=$lock_dir" \
  "${properties[@]}" \
  /bin/bash -p "$repo_dir/scripts/run_wsl_runner_job.sh" verifier >"$run_output" 2>&1
run_status=$?
set -e
test "$run_status" -ne 0
test -f "$log_dir/isolation-started"
test ! -e "$repo_dir/forbidden-write"
test ! -e "$log_dir/isolation-escaped"
printf 'rendered-verifier-isolation-denied unit=%s status=%s\n' "$unit_name" "$run_status"
'''
    assert harness.index("test_root=$(mktemp -d /srv/degen-dogs-task7.XXXXXX)") < harness.index(
        "trap cleanup EXIT"
    ) < harness.index('test_group=$(id -gn "$test_user")')
    prerequisite_failure = subprocess.run(
        [*command, "force-prerequisite-failure"],
        input=harness.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    prerequisite_output = prerequisite_failure.stdout.decode("utf-8", errors="strict")
    assert prerequisite_failure.returncode != 0, "forced prerequisite failure unexpectedly succeeded"
    assert "load=not-found temp_absent=yes" in prerequisite_output, (
        "forced prerequisite cleanup was not proven\n"
        f"stdout={prerequisite_output}\n"
        f"stderr={prerequisite_failure.stderr.decode('utf-8', errors='replace')}"
    )
    print(prerequisite_output.strip())
    result = subprocess.run(
        command,
        input=harness.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"rendered systemd isolation failed with {result.returncode}\n"
        f"stdout={result.stdout.decode('utf-8', errors='replace')}\n"
        f"stderr={result.stderr.decode('utf-8', errors='replace')}"
    )
    output = result.stdout.decode("utf-8", errors="strict")
    assert "rendered-verifier-isolation-denied" in output
    assert "temp_absent=yes" in output
    print(output.strip())


def test(*, require_rendered_systemd_isolation: bool = False) -> None:
    required = (
        ".gitattributes",
        "config/logrotate/degen-dogs-wsl.in",
        "config/systemd/degen-dogs-watcher.service.in",
        "config/systemd/degen-dogs-watcher.timer",
        "config/systemd/degen-dogs-hourly.service.in",
        "config/systemd/degen-dogs-hourly.timer",
        "config/systemd/degen-dogs-health.service.in",
        "config/systemd/degen-dogs-health.timer",
        "config/systemd/degen-dogs-runner.target",
        "config/wsl-runner.env.template",
        "docs/windows-wsl-runner.md",
        "scripts/check_wsl_runner_health.py",
        "scripts/install_wsl_runner.sh",
        "scripts/install_wsl_startup_task.ps1",
        "scripts/preflight_wsl_rpc.py",
        "scripts/run_wsl_runner_anchor.sh",
        "scripts/run_wsl_runner_job.sh",
        *NEW_UNIT_ASSETS,
        *NEW_RUNTIME_ASSETS,
        *BOOTSTRAP_CORE_TESTS,
    )
    for relative in required:
        assert (ROOT / relative).is_file(), relative
        text(relative)

    attributes = text(".gitattributes")
    assert "*.sh text eol=lf" in attributes
    assert "*.service text eol=lf" in attributes
    assert "*.ps1 text eol=lf" in attributes
    assert "scripts/runtime-bin/* text eol=lf" in attributes

    watcher_timer = text("config/systemd/degen-dogs-watcher.timer")
    assert "OnCalendar=*-*-* *:*:07,22,37,52" in watcher_timer
    assert "Persistent=false" in watcher_timer
    hourly_timer = text("config/systemd/degen-dogs-hourly.timer")
    assert "OnCalendar=*-*-* *:59:00" in hourly_timer
    assert "Persistent=true" in hourly_timer
    health_timer = text("config/systemd/degen-dogs-health.timer")
    assert re.search(r"(?m)^OnActiveSec=5min$", health_timer)
    assert re.search(r"(?m)^OnUnitInactiveSec=5min$", health_timer)

    for relative in (
        "config/systemd/degen-dogs-watcher.service.in",
        "config/systemd/degen-dogs-hourly.service.in",
        "config/systemd/degen-dogs-health.service.in",
    ):
        service = text(relative)
        assert "User=@RUNNER_USER@" in service
        assert "ProtectSystem=strict" in service
        assert "ProtectHome=read-only" in service
        assert "CapabilityBoundingSet=" in service
        assert "@LOCK_DIR@" in service
        assert "ConditionPathExists=/run/degen-dogs/activation-enabled" in service

    watcher_service = text("config/systemd/degen-dogs-watcher.service.in")
    assert "Restart=" not in watcher_service
    assert "RestartSec=" not in watcher_service
    assert "StartLimitIntervalSec=0" in watcher_service
    assert "StartLimitBurst=" not in watcher_service
    assert "Restart=on-failure" in text("config/systemd/degen-dogs-hourly.service.in")
    assert "Restart=on-failure" in text("config/systemd/degen-dogs-health.service.in")
    test_queued_worker_units()

    launcher = text("scripts/run_wsl_runner_job.sh")
    assert "MISSION3_WATCHER_AUTO_PUSH=1" in launcher
    assert "DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0" in launcher
    assert "preflight_wsl_rpc.py" in launcher
    assert "MISSION3_WATCHER_LOG_PATH=-" in launcher
    assert "DEGEN_DOGS_REFRESH_LOCK_PATH" in launcher
    assert 'export DEGEN_DOGS_REFRESH_LOCK_PATH="${lock_dir}/refresh.lock"' in launcher
    assert "export DEGEN_DOGS_REMOTE=origin" in launcher
    assert "export DEGEN_DOGS_BRANCH=main" in launcher
    assert "export DEGEN_DOGS_SKIP_PUSH=0" in launcher
    assert "export DEGEN_DOGS_SKIP_PULL=0" in launcher
    assert 'export DEGEN_DOGS_RUNNER_ID="${DEGEN_DOGS_RUNNER_ID:-windows-wsl}"' in launcher
    assert launcher.count("MISSION3_WATCHER_PUBLICATION_MODE=queue") == 1
    assert launcher.index("degen_dogs_load_runner_env") < launcher.index(
        "MISSION3_WATCHER_PUBLICATION_MODE=queue"
    )
    assert "verify_pages_deployment.py" in launcher
    assert "scrub_fixed_worker_authority" in launcher
    assert "remote.origin.pushurl" in launcher
    test_wsl_launcher_policy(
        launcher,
        text("scripts/load_runner_env.sh"),
    )

    installer = text("scripts/install_wsl_runner.sh")
    assert "Usage: /usr/local/libexec/degen-dogs-wsl-installer" in installer
    assert "--repo-dir is required and must be supplied" in installer
    assert 'python3 - "$runtime_tree"' in installer
    assert "subprocess.check_output" not in installer
    assert 'filesystem_type="$(stat -f -c %T "$repo_dir")"' in installer
    assert "test_refresh_and_publish.sh" in installer
    assert "--expected-head" in installer and "--runtime-tree" in installer
    assert '"$asset_dir" != "$repo_dir"' in installer
    assert '"$(id -u "$runner_user")" != "0"' in installer
    assert "runner checkout parent must be a root-owned" in installer
    assert "/var/lib/degen-dogs/activation-armed" in installer
    assert "git -C \"$repo_dir\" push --dry-run" in installer
    assert 'current_branch="$(run_as_runner_runtime git -C "$repo_dir" branch --show-current)"' in installer
    assert 'remote_head="$(run_as_runner_runtime git -C "$repo_dir" rev-parse refs/remotes/origin/main)"' in installer
    assert 'PATH="${repo_dir}/scripts/runtime-bin:/usr/local/bin:/usr/bin:/bin"' in installer
    assert 'run_as_runner /usr/bin/python3 -m venv' in installer
    assert '"%" in value' in installer
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" in installer
    assert "validate_runner_git_destination" in installer
    test_checkout_attestation_modes(installer)
    test_bootstrap_receipt_gate(installer)

    publisher = text("scripts/refresh_and_publish.sh")
    assert 'QUARANTINE_STATE_DIR="${REPO_DIR}/.local"' in publisher
    assert 'degen_dogs_private_dir "$QUARANTINE_STATE_DIR"' in publisher
    skip_deploy_marker = installer.index('if [[ "$skip_deploy_key" != "1" ]]')
    assert installer.index("config --unset-all remote.origin.pushurl") > skip_deploy_marker
    deploy_key_block = installer.split('if [[ "$skip_deploy_key" != "1" ]]', 1)[1]
    assert deploy_key_block.index("validate_runner_git_destination") > deploy_key_block.index("fi\n")

    validator_marker = "# WSL_SSH_MATERIAL_VALIDATOR"
    validator_marker_offset = installer.index(validator_marker)
    validator_start = installer.rfind("<<'PY'\n", 0, validator_marker_offset)
    assert validator_start >= 0
    validator_start += len("<<'PY'\n")
    validator_end = installer.index("\nPY", validator_marker_offset)
    validator = installer[validator_start:validator_end]
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        known_hosts_path = temporary / "known_hosts"
        config_path = temporary / "config"
        key_path = temporary / "deploy_key"
        known_hosts = (
            "github.com ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
        )
        config = f"""Host github-degen-dogs
    HostName github.com
    User git
    IdentityFile {key_path}
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
    UserKnownHostsFile {known_hosts_path}
    GlobalKnownHostsFile /dev/null
    ProxyCommand none
    ProxyJump none
"""
        known_hosts_path.write_text(known_hosts, encoding="ascii", newline="\n")
        config_path.write_text(config, encoding="ascii", newline="\n")

        def validate_ssh_material() -> subprocess.CompletedProcess[bytes]:
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    validator,
                    str(known_hosts_path),
                    str(config_path),
                    str(key_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

        accepted = validate_ssh_material()
        assert accepted.returncode == 0, accepted.stderr.decode("utf-8", errors="replace")
        mutations = {
            "HostName": config.replace("HostName github.com", "HostName attacker.invalid"),
            "IdentityFile": config.replace(str(key_path), str(temporary / "attacker_key")),
            "StrictHostKeyChecking": config.replace("StrictHostKeyChecking yes", "StrictHostKeyChecking no"),
            "UserKnownHostsFile": config.replace(str(known_hosts_path), str(temporary / "other_hosts")),
            "ProxyCommand": config.replace("ProxyCommand none", "ProxyCommand ssh attacker.invalid -W %h:%p"),
            "ProxyJump": config.replace("ProxyJump none", "ProxyJump attacker.invalid"),
        }
        for field, mutation in mutations.items():
            config_path.write_text(mutation, encoding="ascii", newline="\n")
            rejected = validate_ssh_material()
            assert rejected.returncode != 0, f"unsafe {field} mutation was accepted"
        config_path.write_text(config, encoding="ascii", newline="\n")
        known_hosts_path.write_text(
            known_hosts.replace("github.com", "attacker.invalid"),
            encoding="ascii",
            newline="\n",
        )
        rejected = validate_ssh_material()
        assert rejected.returncode != 0, "non-canonical known_hosts destination was accepted"
        known_hosts_path.write_text(
            known_hosts + "attacker.invalid ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEvil\n",
            encoding="ascii",
            newline="\n",
        )
        rejected = validate_ssh_material()
        assert rejected.returncode != 0, "extra known_hosts entry was accepted"
    for line in installer.splitlines():
        if re.search(r'\bgit\s+-C\s+"\$repo_dir"', line):
            assert "runner_git" in line or "run_as_runner_runtime" in line, line
    assert "systemctl disable --now" in installer
    assert "--uninstall" in installer
    test_queued_worker_lifecycle_inventories()

    powershell = text("scripts/install_wsl_startup_task.ps1")
    assert not powershell.startswith("#Requires -RunAsAdministrator")
    assert "function Assert-WslRunnerInvocationPolicy" in powershell
    assert "function Get-WslRunnerGitPath" in powershell
    assert "$gitPath = Get-WslRunnerGitPath" in powershell
    assert "function Remove-WslRunnerTemporaryGitDirectory" in powershell
    assert "Remove-WslRunnerTemporaryGitDirectory `" in powershell
    policy_gate = powershell.rindex("Assert-WslRunnerInvocationPolicy `")
    source_attestation = powershell.index(
        "Assert-TrustedBootstrapSource -Commit $TrustedInstallerCommit"
    )
    assert policy_gate < source_attestation
    assert "function Invoke-VerifiedWslImport" in powershell
    assert powershell.count("Invoke-VerifiedWslImport") >= 2
    assert "https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-wsl-amd64.wsl" in powershell
    assert "9b2f7730dc68227dd04a9f3e5eab86ad85caf556b8606ad94f1f29ff5c4fd3f5" in powershell
    assert "Get-WslRunnerImportArguments" in powershell
    assert "Get-WslRunnerKnownLocalAppData" in powershell
    assert "New-WslRunnerImportAttempt" in powershell
    assert "Enter-WslRunnerDistroLock" in powershell
    assert "Enter-WslRunnerTaskLock" in powershell
    assert "Invoke-WslRunnerImportRollback" in powershell
    assert "$listArguments = @('--list', '--all', '--quiet')" in powershell
    assert "--proto-redir '=https'" in powershell
    assert "--retry-all-errors" in powershell
    assert "is not Ubuntu 24.04 AMD64" in powershell
    assert "New-ScheduledTaskAction" in powershell
    assert "-Disable `" in powershell
    assert "degen-dogs-wsl-anchor" in powershell
    assert "-AtStartup" in powershell and "-AtLogOn" in powershell
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in powershell
    assert "$AtLogOnOnly" in powershell
    assert "Get-WslRunnerTriggerKinds" in powershell
    assert powershell.count("-Trigger $selectedTriggers.ToArray()") == 3
    registration_blocks = powershell.split("Register-ScheduledTask `")[1:]
    assert len(registration_blocks) == 3
    for block in registration_blocks:
        assert "-Force" not in block.split("\n        }", 1)[0]
    assert powershell.count("-PrepareAction $isolateRegisteredTaskAction") == 2
    assert "-Trigger @($startupTrigger" not in powershell
    assert "Export-ScheduledTask" in powershell
    assert powershell.count("Assert-WslRunnerOwnedTaskDefinition") >= 6
    assert "function Assert-WslRunnerManagedTaskXml" in powershell
    assert "-AllowManagedPredecessor" in powershell
    assert "Assert-CurrentAccountCredential" in powershell
    assert 'merge --ff-only "$runtime_sha"' in powershell
    assert "refs/heads/main" in powershell
    assert "could not quiesce %s before runner upgrade" in powershell
    assert "systemctl is-active --quiet" in powershell
    assert "$UpgradeTrustedBundle" in powershell
    assert "TrustedInstallerCommit" in powershell
    trust_required = powershell.index("if (-not $TrustedInstallerCommit)")
    source_check = powershell.index(
        "Assert-TrustedBootstrapSource -Commit $TrustedInstallerCommit"
    )
    wsl_initialization = powershell.index("$wsl = Join-Path")
    assert trust_required < source_check < wsl_initialization
    assert "if ($TrustedInstallerCommit)" not in powershell[:wsl_initialization]
    assert "$installedTrustedCommit" in powershell
    assert "The installed frozen bundle does not match TrustedInstallerCommit" in powershell
    assert "A trusted bundle is already installed; use -UpgradeTrustedBundle" not in powershell
    assert "$trustedWrapperProvision" in powershell
    assert "wrapper bytes differ after trusted regeneration" in powershell
    assert "unsafe pre-existing privileged installer" in powershell
    task_name_pattern_match = re.search(
        r"\[ValidatePattern\('([^']+)'\)\]\s*\[string\]\$TaskName",
        powershell,
    )
    assert task_name_pattern_match
    task_name_pattern = task_name_pattern_match.group(1)
    assert re.fullmatch(task_name_pattern, "Degen Dogs WSL Runner")
    for unsafe_task_name in (
        "*",
        "Degen Dogs*",
        "\\Degen Dogs",
        "Degen/Dogs",
        " Degen Dogs",
        "Degen Dogs ",
        "Degen Dogs?",
        "Degen[Dogs]",
    ):
        assert not re.fullmatch(task_name_pattern, unsafe_task_name), unsafe_task_name
    assert "function Get-ExactScheduledTask" in powershell
    task_lookup = powershell.split("function Get-ExactScheduledTask", 1)[1].split(
        "function Assert-WslRunnerOwnedTaskDefinition", 1
    )[0]
    assert "-TaskPath '\\'" in task_lookup
    assert "-ErrorAction Stop" in task_lookup
    assert "SilentlyContinue" not in task_lookup
    assert "[StringComparison]::OrdinalIgnoreCase" in task_lookup
    assert "[StringComparison]::Ordinal" in task_lookup
    assert not re.search(
        r"(?m)^\s*(?:Disable|Stop|Unregister|Enable|Start)-ScheduledTask\s+-TaskName\s+\$TaskName",
        powershell,
    )
    source_guard = powershell[:wsl_initialization]
    assert "hash-object" in source_guard and "--no-filters" in source_guard
    assert "'merge-base'" in source_guard and "'--is-ancestor'" in source_guard
    assert "refs/remotes/origin/main" in source_guard
    assert "ROOT_ASSETS.sha256" in powershell
    assert 'git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" archive' in powershell
    assert "/run/degen-dogs/anchor-ready" in powershell
    assert "catch {\n        $activationError = $_" in powershell
    activation_rollback = powershell.rsplit("catch {\n        $activationError = $_", 1)[1]
    assert "$rollbackClean" in activation_rollback
    assert "Invoke-CurrentWslRunnerTaskIsolation -Remove $true" in activation_rollback
    assert "Windows task isolation was unproven" in activation_rollback
    assert "function Invoke-WslRunnerTaskIsolation" in powershell
    assert "Final ownership attestation failed" in powershell
    assert "OperationAttempts" in powershell
    assert "exact Windows task isolation could not be established" in activation_rollback
    assert "--terminate $DistroName" in activation_rollback
    assert "fallback termination failed" in activation_rollback
    assert "Activation failed and clean rollback could not be established" in activation_rollback
    activation_success = powershell.split("if ($Activate) {", 1)[1].split("catch {", 1)[0]
    assert activation_success.count("/run/degen-dogs/anchor-ready") >= 2
    assert "if ($currentTask.State -ne 'Running')" in activation_success
    assert "The final activation liveness proof failed" in activation_success
    test_health_timer_activation(powershell)
    test_activation_liveness_probes_reject_each_inactive_unit(powershell)

    assert "6F71F525282841EEDAF851B42F59B5F99B1BE0B4" in powershell
    key_download = powershell.index("nodesource-repo.gpg.key")
    key_verify = powershell.index("--with-colons", key_download)
    key_trust = powershell.index("--dearmor", key_verify)
    apt_source = powershell.index("nodesource.list", key_trust)
    assert key_download < key_verify < key_trust < apt_source
    powershell_lines = powershell.splitlines()
    for index, line in enumerate(powershell_lines):
        if "git -c core.hooksPath=/dev/null" in line and ("'$RepoDir'" in line or "'$RepoDir/.git'" in line):
            context = "\n".join(powershell_lines[max(0, index - 2) : index + 1])
            assert "runuser -u '$RunnerUser'" in context, context
    embedded_payloads = re.findall(
        r"(?ms)=\s+@(?P<quote>['\"])(?:\r?\n)(?P<body>.*?)(?:\r?\n)(?P=quote)@",
        powershell,
    )
    assert len(embedded_payloads) == 11
    for index, (quote, payload) in enumerate(embedded_payloads, start=1):
        if quote == '"':
            payload = re.sub(r"`(.)", r"\1", payload)
        result = subprocess.run(
            ["bash", "-n"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert result.returncode == 0, (
            f"embedded Bash payload {index}: {result.stderr.decode('utf-8', errors='replace')}"
        )

    anchor = text("scripts/run_wsl_runner_anchor.sh")
    assert "activation-armed" in anchor and "activation-enabled" in anchor
    assert "systemctl is-enabled --quiet" in anchor
    anchor_regression = anchor + r'''

test_root=$(mktemp -d)
state_dir="$test_root/state"
runtime_dir="$test_root/run"
armed_marker="${state_dir}/activation-armed"
active_marker="${runtime_dir}/activation-enabled"
ready_marker="${runtime_dir}/anchor-ready"

id() {
  if [[ "${1:-}" == "-u" ]]; then printf '0\n'; return 0; fi
  command id "$@"
}
install() {
  if [[ "${1:-}" == "-d" ]]; then
    mkdir -p -- "${@: -2}"
    return 0
  fi
  local source="${@: -2:1}"
  local target="${@: -1}"
  cp -- "$source" "$target"
  chmod 0644 "$target"
}
stat() {
  case "${2:-}" in
    %U) printf 'root\n' ;;
    %h) printf '1\n' ;;
    %a) printf '644\n' ;;
    *) command stat "$@" ;;
  esac
}
systemctl() {
  case "${1:-}" in
    is-enabled) return 0 ;;
    is-active) return 1 ;;
    start) return 42 ;;
    *) return 43 ;;
  esac
}
mkdir -p "$state_dir" "$runtime_dir"
printf 'armed=1\n' >"$armed_marker"
chmod 0644 "$armed_marker"
set +e
( set -Eeuo pipefail; anchor_main )
anchor_status=$?
set -e
test "$anchor_status" = 42
test ! -e "$ready_marker"
test ! -e "$active_marker"
printf 'anchor-failure-cleanup-checked\n'
rm -rf -- "$test_root"
'''
    anchor_failure = run_bash(anchor_regression)
    assert anchor_failure.stdout == b"anchor-failure-cleanup-checked\n"

    attestation = powershell_literal_payload(powershell, "trustedBundleAttestation")
    attestation_regression = r'''
set -Eeuo pipefail
attest() (
''' + attestation + r'''
)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT
bundle_root="$test_root/trusted-bundles"
trusted_commit=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
bundle_target="$bundle_root/$trusted_commit"
mkdir -p "$bundle_target"
chmod 0700 "$test_root" "$bundle_root"
printf 'trusted asset\n' >"$bundle_target/asset"
printf '%s\n' "$trusted_commit" >"$bundle_target/TRUSTED_COMMIT"
(cd "$bundle_target" && sha256sum asset >ROOT_ASSETS.sha256)
ln -s "$bundle_target" "$bundle_root/current"
actual=$(attest "$bundle_root" "$(id -un)")
test "$actual" = "$trusted_commit"
chmod 0777 "$bundle_root"
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'writable frozen-bundle root passed attestation\n' >&2
  exit 88
fi
chmod 0700 "$bundle_root"
chmod 0777 "$test_root"
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'writable frozen-bundle parent passed attestation\n' >&2
  exit 89
fi
chmod 0700 "$test_root"
printf 'tampered\n' >>"$bundle_target/asset"
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'tampered frozen bundle passed attestation\n' >&2
  exit 90
fi
printf 'trusted asset\n' >"$bundle_target/asset"
find() { return 66; }
if attest "$bundle_root" "$(id -un)" >/dev/null 2>&1; then
  printf 'failed metadata traversal passed attestation\n' >&2
  exit 91
fi
'''
    run_bash(attestation_regression)

    wrapper_provision = powershell_literal_payload(powershell, "trustedWrapperProvision")
    wrapper_regression = r'''
set -Eeuo pipefail
provision_wrapper() (
''' + wrapper_provision + r'''
)
test_root=$(mktemp -d)
trap 'rm -rf -- "$test_root"' EXIT
bundle_root="$test_root/trusted-bundles"
trusted_commit=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
bundle_target="$bundle_root/$trusted_commit"
wrapper_root="$test_root/libexec"
mkdir -p "$bundle_target/scripts"
chmod 0700 "$test_root" "$bundle_root"
cat >"$bundle_target/scripts/install_wsl_runner.sh" <<'INSTALLER'
#!/usr/bin/env bash
printf 'trusted-wrapper-executed\n'
INSTALLER
chmod 0755 "$bundle_target/scripts/install_wsl_runner.sh"
printf '%s\n' "$trusted_commit" >"$bundle_target/TRUSTED_COMMIT"
(cd "$bundle_target" && sha256sum scripts/install_wsl_runner.sh >ROOT_ASSETS.sha256)
ln -s "$bundle_target" "$bundle_root/current"
provision_wrapper "$wrapper_root" "$bundle_root" "$(id -un)" "$(id -gn)"
test -f "$wrapper_root/degen-dogs-wsl-installer"
test ! -L "$wrapper_root/degen-dogs-wsl-installer"
test "$(stat -c %a "$wrapper_root/degen-dogs-wsl-installer")" = 755
test "$("$wrapper_root/degen-dogs-wsl-installer")" = trusted-wrapper-executed
first_wrapper_inode=$(stat -c %i "$wrapper_root/degen-dogs-wsl-installer")
provision_wrapper "$wrapper_root" "$bundle_root" "$(id -un)" "$(id -gn)"
second_wrapper_inode=$(stat -c %i "$wrapper_root/degen-dogs-wsl-installer")
test "$second_wrapper_inode" != "$first_wrapper_inode"
rm -f "$wrapper_root/degen-dogs-wsl-installer"
ln -s "$test_root/attacker" "$wrapper_root/degen-dogs-wsl-installer"
if provision_wrapper "$wrapper_root" "$bundle_root" "$(id -un)" "$(id -gn)" >/dev/null 2>&1; then
  printf 'unsafe wrapper symlink was regenerated through\n' >&2
  exit 92
fi
'''
    run_bash(wrapper_regression)

    rollback = powershell_literal_payload(powershell, "rollbackPublisher")
    expected_rollback_calls = """disable --now degen-dogs-runner.target
disable --now degen-dogs-watcher.timer
disable --now degen-dogs-hourly.timer
disable --now degen-dogs-health.timer
disable --now degen-dogs-publisher.path
disable --now degen-dogs-publisher.timer
disable --now degen-dogs-pages-verifier.path
disable --now degen-dogs-pages-verifier.timer
stop degen-dogs-watcher.service
stop degen-dogs-hourly.service
stop degen-dogs-health.service
stop degen-dogs-publisher.service
stop degen-dogs-pages-verifier.service
show --property=ActiveState --value degen-dogs-runner.target
show --property=ActiveState --value degen-dogs-watcher.timer
show --property=ActiveState --value degen-dogs-hourly.timer
show --property=ActiveState --value degen-dogs-health.timer
show --property=ActiveState --value degen-dogs-publisher.path
show --property=ActiveState --value degen-dogs-publisher.timer
show --property=ActiveState --value degen-dogs-pages-verifier.path
show --property=ActiveState --value degen-dogs-pages-verifier.timer
show --property=ActiveState --value degen-dogs-watcher.service
show --property=ActiveState --value degen-dogs-hourly.service
show --property=ActiveState --value degen-dogs-health.service
show --property=ActiveState --value degen-dogs-publisher.service
show --property=ActiveState --value degen-dogs-pages-verifier.service
"""

    def rollback_regression(mode: str, expected_returncode: int) -> None:
        harness = r'''
set -Eeuo pipefail
test_root=$(mktemp -d)
calls="$test_root/calls"
state_dir="$test_root/state"
runtime_dir="$test_root/run"
mkdir -p "$state_dir" "$runtime_dir"
touch "$state_dir/activation-armed" "$runtime_dir/activation-enabled" "$runtime_dir/anchor-ready"
set -- "$state_dir" "$runtime_dir"
cat >"$test_root/expected" <<'EXPECTED_CALLS'
''' + expected_rollback_calls + r'''EXPECTED_CALLS
systemctl() {
  printf '%s\n' "$*" >>"$calls"
  if [[ "''' + mode + r'''" == "failure" && "$*" == "stop degen-dogs-hourly.service" ]]; then
    return 9
  fi
  if [[ "${1:-}" == "show" ]]; then
    if [[ "''' + mode + r'''" == "failure" && "${*: -1}" == "degen-dogs-hourly.service" ]]; then
      printf 'active\n'
    else
      printf 'inactive\n'
    fi
  fi
}
rollback_command() {
''' + rollback + r'''
}
set +e
rollback_command "$@"
status=$?
set -e
test ! -e "$state_dir/activation-armed" || status=91
test ! -e "$runtime_dir/activation-enabled" || status=92
test ! -e "$runtime_dir/anchor-ready" || status=93
if ! cmp -s "$calls" "$test_root/expected"; then
  diff -u "$test_root/expected" "$calls" >&2 || true
  status=94
fi
printf 'rollback-cleanup-checked status=%s\n' "$status"
rm -rf -- "$test_root"
exit "$status"
'''
        result = run_bash(harness, expected_returncode=expected_returncode)
        assert b"rollback-cleanup-checked" in result.stdout

    rollback_regression("success", 0)
    rollback_regression("failure", 1)

    runner_docs = text("docs/windows-wsl-runner.md")
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -Credential $credential" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -AtLogOnOnly" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Activate -AtLogOnOnly" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -Uninstall" in runner_docs
    assert "& $bootstrapScript -TrustedInstallerCommit $trustedCommit -AtLogOnOnly -Uninstall" in runner_docs
    assert "cannot recover\nwhile the user is signed out" in runner_docs
    assert "/var/lib/degen-dogs/bootstrap-test-receipt.json" in runner_docs

    preflight = text("scripts/preflight_wsl_rpc.py")
    ast.parse(preflight)
    assert "RARITY_MUTATION_TOPICS" in preflight
    assert "builder.DEGEN_DOGS" in preflight
    assert '"eth_getLogs"' in preflight
    assert "builder.fetch_dog_total_supply(snapshot_tag)" in preflight
    assert "builder.fetch_token_uri_bindings(" in preflight
    assert "[current_token, total_supply]" in preflight
    assert "block_hash=expected_hash" in preflight
    assert "current_present_next_nonexistent" in preflight
    report_source = preflight.split("report = {", 1)[1]
    assert '"token_uri"' not in report_source
    health = text("scripts/check_wsl_runner_health.py")
    health_tree = ast.parse(health)
    assert "current_dog_token_id" in health
    assert "terminal_publication_problem(latest_terminal)" in health
    selected = [
        node
        for node in health_tree.body
        if isinstance(node, (ast.Assign, ast.FunctionDef))
        and (
            isinstance(node, ast.FunctionDef)
            and node.name == "terminal_publication_problem"
            or isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PUBLISHED_RESULTS" for target in node.targets)
        )
    ]
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), "check_wsl_runner_health.py", "exec"), namespace)
    publication_problem = namespace["terminal_publication_problem"]
    assert callable(publication_problem)
    assert publication_problem({"result": "success_pushed"}) == ""
    assert publication_problem({"result": "success_pushed_live_timeout"}) == ""
    assert publication_problem({"result": "success_superseded_by_peer"}) == ""
    assert publication_problem({"result": "success_no_diff"}) == ""
    assert "failed" in publication_problem({"result": "failed"})
    assert "not published" in publication_problem({"result": "success_generated"})
    assert "not published" in publication_problem({"result": "success_skip_push"})
    assert "missing" in publication_problem({})

    runner_env = text("config/wsl-runner.env.template")
    assert "MISSION3_LOG_QUORUM_MAX_BLOCKS=500" in runner_env
    assert re.search(r"(?m)^DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0$", runner_env)
    assert re.search(r"(?m)^MISSION3_WATCHER_PUBLICATION_MODE=inline$", runner_env)
    assert "MISSION3_WATCHER_PUBLICATION_MODE=queue" not in runner_env

    if require_rendered_systemd_isolation:
        test_rendered_verifier_systemd_isolation()

    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]
    assert scripts["refresh:live-snapshot"] == "python3 scripts/build_live_snapshot_bundle.py"
    assert scripts["test:live-snapshot"] == "python3 scripts/test_build_live_snapshot_bundle.py"
    assert "test:pages-validation-runner" in scripts["test:dashboard"]
    assert scripts["test:wsl-runner-assets"] == "python3 scripts/test_wsl_runner_assets.py"
    assert scripts["test:wsl-runner-isolation"] == (
        "python3 scripts/test_wsl_runner_assets.py --require-rendered-systemd-isolation"
    )
    assert scripts["test:wsl-windows-policy"] == "python3 scripts/test_wsl_runner_windows_policy.py"
    assert "test:wsl-runner-isolation" not in scripts["test:ops"]
    assert "test:wsl-windows-policy" in scripts["test:ops"]

    runner_env_loader = (ROOT / "scripts" / "load_runner_env.sh").read_text(encoding="utf-8")
    production_allowlist = runner_env_loader.split("DEGEN_DOGS_RUNNER_COMMON_ENV_ALLOWLIST='", 1)[1].split("'", 1)[0]
    assert production_allowlist.count("DEGEN_DOGS_RUNNER_ID") == 1
    assert "MISSION3_LOG_QUORUM_MAX_BLOCKS" in production_allowlist
    assert "DEGEN_DOGS_HEALTH_REFRESH_RETRY_BASE_SECONDS" in runner_env_loader
    assert "DEGEN_DOGS_HEALTH_REFRESH_RETRY_MAX_SECONDS" in runner_env_loader

    print(f"wsl_runner_asset_tests=pass count={len(required)}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args not in ([], [PRIVILEGED_ISOLATION_FLAG]):
        print(
            f"usage: {Path(sys.argv[0]).name} [{PRIVILEGED_ISOLATION_FLAG}]",
            file=sys.stderr,
        )
        return 2
    test(require_rendered_systemd_isolation=bool(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
