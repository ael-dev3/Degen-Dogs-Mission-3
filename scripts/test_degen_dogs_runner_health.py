#!/usr/bin/env python3
"""Regression tests for the Mission 3 local runner health watchdog."""
from __future__ import annotations

import fcntl
import importlib.util
import io
import json
import os
import plistlib
import shlex
import sys
import tempfile
import time
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import runner_path_security as path_security


SCRIPT = Path(__file__).with_name("degen_dogs_runner_health.py")
REPO_VENV_PYTHON = SCRIPT.parent.parent / ".venv" / "bin" / "python3"
TEST_RUNTIME_PYTHON = REPO_VENV_PYTHON if os.access(REPO_VENV_PYTHON, os.X_OK) else Path(sys.executable)
spec = importlib.util.spec_from_file_location("degen_dogs_runner_health", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load {SCRIPT}")
health = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = health
spec.loader.exec_module(health)


def test_cli_arguments_are_side_effect_free() -> None:
    original_main = health.main
    health.main = lambda: (_ for _ in ()).throw(AssertionError("health main unexpectedly ran"))
    try:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            assert health.cli_main(["--help"]) == 0
        assert "usage:" in stdout.getvalue()

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            assert health.cli_main(["--unknown"]) == 2
        assert "unsupported argument" in stderr.getvalue()
    finally:
        health.main = original_main


class FakeLiveResponse:
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str,
        status_code: int = 200,
        final_url: str = "",
    ) -> None:
        self.body = body
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.status_code = status_code
        self.final_url = final_url
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self) -> int:
        return self.status_code

    def geturl(self) -> str:
        return self.final_url

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        return self.body[:limit]


class FakeLiveOpener:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):  # noqa: ANN001, ANN201, ARG002
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if not response.final_url:
            response.final_url = request.full_url
        return response


def test_live_site_transport_accepts_only_exact_bounded_targets() -> None:
    original_opener = health.LIVE_OPENER
    html = f"<html>auction_feed {health.LIVE_URL.rstrip('/')}</html>".encode()
    status = json.dumps(
        {
            "kind": "refresh_status",
            "site_url": health.LIVE_URL,
            "latest_generated_block": 123,
            "last_successful_refresh_time_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
    ).encode()
    html_response = FakeLiveResponse(html, content_type="text/html; charset=utf-8")
    status_response = FakeLiveResponse(status, content_type="application/json; charset=utf-8")
    opener = FakeLiveOpener(html_response, status_response)
    try:
        health.LIVE_OPENER = opener
        ok, message = health.live_site_ok()
        assert ok is True
        assert message == "live site/status ok at block 123"
    finally:
        health.LIVE_OPENER = original_opener

    assert len(opener.requests) == 2
    assert html_response.read_limit == health.LIVE_HTML_MAX_BYTES + 1
    assert status_response.read_limit == health.LIVE_STATUS_MAX_BYTES + 1
    assert health.NoLiveRedirectHandler().redirect_request(None, None, 302, "", {}, "https://attacker.example") is None


def test_default_live_status_freshness_window_is_ninety_minutes() -> None:
    assert health.DEFAULT_LIVE_STALE_SECONDS == 90 * 60


def test_live_site_transport_rejects_unapproved_redirect_status_mime_and_oversize() -> None:
    original_opener = health.LIVE_OPENER
    never_called = FakeLiveOpener()
    try:
        health.LIVE_OPENER = never_called
        try:
            health.fetch_fixed_live_text("https://attacker.example/", cache_buster=1)
        except RuntimeError as exc:
            assert "approved fixed target" in str(exc)
        else:
            raise AssertionError("unapproved live-health endpoint was fetched")
        assert never_called.requests == []

        cases = [
            (FakeLiveResponse(b"ok", content_type="text/html", final_url="https://attacker.example/"), "URL changed"),
            (FakeLiveResponse(b"ok", content_type="text/html", status_code=206), "HTTP status"),
            (FakeLiveResponse(b"ok", content_type="application/json"), "content type"),
        ]
        for response, expected_error in cases:
            health.LIVE_OPENER = FakeLiveOpener(response)
            try:
                health.fetch_fixed_live_text(health.LIVE_URL, cache_buster=1)
            except RuntimeError as exc:
                assert expected_error in str(exc)
            else:
                raise AssertionError(f"unsafe live-health response was accepted: {expected_error}")

        declared = FakeLiveResponse(b"ok", content_type="text/html")
        declared.headers["Content-Length"] = str(health.LIVE_HTML_MAX_BYTES + 1)
        health.LIVE_OPENER = FakeLiveOpener(declared)
        try:
            health.fetch_fixed_live_text(health.LIVE_URL, cache_buster=1)
        except RuntimeError as exc:
            assert "size limit" in str(exc)
        else:
            raise AssertionError("oversize declared live-health response was accepted")
        assert declared.read_limit is None

        streamed = FakeLiveResponse(b"x" * (health.LIVE_HTML_MAX_BYTES + 1), content_type="text/html")
        streamed.headers.pop("Content-Length")
        health.LIVE_OPENER = FakeLiveOpener(streamed)
        try:
            health.fetch_fixed_live_text(health.LIVE_URL, cache_buster=1)
        except RuntimeError as exc:
            assert "size limit" in str(exc)
        else:
            raise AssertionError("oversize streamed live-health response was accepted")

        invalid_utf8 = FakeLiveResponse(b"\xff", content_type="text/html")
        health.LIVE_OPENER = FakeLiveOpener(invalid_utf8)
        try:
            health.fetch_fixed_live_text(health.LIVE_URL, cache_buster=1)
        except RuntimeError as exc:
            assert "valid UTF-8" in str(exc)
        else:
            raise AssertionError("invalid UTF-8 live-health response was accepted")
    finally:
        health.LIVE_OPENER = original_opener


def test_live_site_transport_sanitizes_http_errors_and_rejects_invalid_json() -> None:
    original_opener = health.LIVE_OPENER
    secret_url = "https://provider.example/path-secret?api_key=query-secret"
    http_error = health.urllib.error.HTTPError(secret_url, 401, "reason-secret", {}, None)
    try:
        health.LIVE_OPENER = FakeLiveOpener(http_error)
        ok, message = health.live_site_ok()
        assert ok is False
        assert message == "live HTTP check failed: live endpoint HTTP 401"
        assert "secret" not in message

        html = f"<html>auction_feed {health.LIVE_URL.rstrip('/')}</html>".encode()
        health.LIVE_OPENER = FakeLiveOpener(
            FakeLiveResponse(html, content_type="text/html"),
            FakeLiveResponse(b"not-json", content_type="application/json"),
        )
        assert health.live_site_ok() == (False, "live refresh status check failed: invalid JSON")
    finally:
        health.LIVE_OPENER = original_opener


def test_refresh_lock_detection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "refresh.lock"
        setattr(health, "REFRESH_LOCK_PATH", lock_path)

        # A stale/unlocked lock file must not suppress a real dirty-worktree alert.
        lock_path.touch()
        assert health.refresh_is_active() is False

        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert health.refresh_is_active() is True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert health.refresh_is_active() is False


def test_active_lock_metadata_requires_a_held_flock() -> None:
    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "watcher.lock"
        lock_path.write_text(
            "kind=watcher\npid=123\nstarted_at_utc=2026-08-02T12:00:00Z\n",
            encoding="utf-8",
        )
        assert health.inspect_active_lock(lock_path) == (False, None)

        fd = os.open(lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            active, started_ts = health.inspect_active_lock(lock_path)
            assert active is True
            assert started_ts == datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        assert health.inspect_active_lock(lock_path) == (False, None)


def test_fresh_active_attempt_requires_new_held_bounded_run() -> None:
    now = datetime(2026, 8, 2, 12, 10, tzinfo=timezone.utc).timestamp()
    started = now - 60
    completed = now - 120
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=started,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is True
    assert health.fresh_active_attempt(
        lock_held=False,
        started_ts=started,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=completed,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=now - 90,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False
    assert health.fresh_active_attempt(
        lock_held=True,
        started_ts=now + 1,
        completed_ts=completed,
        now=now,
        grace_seconds=90,
    ) is False


def test_alert_state_is_atomic_private_and_rejects_untrusted_files() -> None:
    originals = {
        "ALERT_STATE_PATH": health.ALERT_STATE_PATH,
        "ALERT_DRY_RUN": health.ALERT_DRY_RUN,
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state_path = root / "state" / "alert.json"
        try:
            health.ALERT_STATE_PATH = state_path
            health.ALERT_DRY_RUN = False
            health.save_alert_state(
                {
                    "active": True,
                    "issue_number": 42,
                    health.TRUSTED_STATE_KEY: True,
                }
            )

            assert state_path.stat().st_mode & 0o777 == 0o600
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            assert health.TRUSTED_STATE_KEY not in persisted
            loaded = health.load_alert_state()
            assert loaded["issue_number"] == 42
            assert loaded[health.TRUSTED_STATE_KEY] is True

            state_path.chmod(0o666)
            assert health.load_alert_state() == {}

            state_path.unlink()
            target = root / "outsider.json"
            target.write_text('{"active": true, "issue_number": 666}\n', encoding="utf-8")
            target.chmod(0o600)
            state_path.symlink_to(target)
            assert health.load_alert_state() == {}

            nested_parent = root / "nested-parent"
            nested_target = root / "nested-target"
            nested_parent.mkdir()
            nested_target.mkdir()
            (nested_parent / "redirect").symlink_to(nested_target, target_is_directory=True)
            health.ALERT_STATE_PATH = nested_parent / "redirect" / "created" / "alert.json"
            try:
                health.save_alert_state({"active": True})
            except (OSError, PermissionError, health.SecurePathError):
                pass
            else:
                raise AssertionError("nested symlink alert-state ancestor was accepted")
            assert not (nested_target / "created").exists()
        finally:
            for name, value in originals.items():
                setattr(health, name, value)


def test_issue_discovery_requires_marker_and_authenticated_author() -> None:
    original_run_gh = health.run_gh
    calls: list[list[str]] = []

    def fake_run_gh(args: list[str], *, body: str | None = None, timeout: int = 45) -> object:
        del body, timeout
        calls.append(args)
        if args[:2] == ["api", "user"]:
            return health.Result(0, "watchdog-bot\n", "")
        issues = [
            {
                "number": 10,
                "url": "https://github.com/example/repo/issues/10",
                "title": health.RUNNER_ISSUE_TITLE,
                "body": health.RUNNER_ISSUE_MARKER,
                "author": {"login": "outsider"},
            },
            {
                "number": 11,
                "url": "https://github.com/example/repo/issues/11",
                "title": health.RUNNER_ISSUE_TITLE,
                "body": "same title but no watchdog marker",
                "author": {"login": "watchdog-bot"},
            },
            {
                "number": 12,
                "url": "https://github.com/example/repo/issues/12",
                "title": health.RUNNER_ISSUE_TITLE,
                "body": f"{health.RUNNER_ISSUE_MARKER}\nowned incident",
                "author": {"login": "watchdog-bot"},
            },
        ]
        return health.Result(0, json.dumps(issues), "")

    try:
        health.run_gh = fake_run_gh
        number, url = health.find_open_runner_issue()
        assert number == 12
        assert url == health.canonical_issue_url(12)
        list_call = calls[-1]
        assert list_call[list_call.index("--author") + 1] == "watchdog-bot"
        assert "body" in list_call[list_call.index("--json") + 1]
    finally:
        health.run_gh = original_run_gh


def test_issue_update_and_close_ignore_untrusted_state_ids() -> None:
    assert health.trusted_state_issue_number({"issue_number": 99}) is None
    trusted = {health.TRUSTED_STATE_KEY: True, "issue_number": 99}
    assert health.trusted_state_issue_number(trusted) == 99

    original_run_gh = health.run_gh
    original_enabled = health.GITHUB_ALERTS_ENABLED
    original_dry_run = health.ALERT_DRY_RUN
    calls: list[list[str]] = []

    def fake_run_gh(args: list[str], *, body: str | None = None, timeout: int = 45) -> object:
        del body, timeout
        calls.append(args)
        return health.Result(0, "", "")

    try:
        health.run_gh = fake_run_gh
        health.GITHUB_ALERTS_ENABLED = True
        health.ALERT_DRY_RUN = False
        assert health.close_github_issue({"issue_number": 99}, "recovered") is None
        assert calls == []
        message = health.close_github_issue(trusted, "recovered")
        assert message == f"GitHub issue closed: {health.canonical_issue_url(99)}"
        assert [call[:2] for call in calls] == [["issue", "comment"], ["issue", "close"]]
    finally:
        health.run_gh = original_run_gh
        health.GITHUB_ALERTS_ENABLED = original_enabled
        health.ALERT_DRY_RUN = original_dry_run


def test_failed_issue_recovery_remains_active_for_retry() -> None:
    originals = {
        "load_alert_state": health.load_alert_state,
        "save_alert_state": health.save_alert_state,
        "close_github_issue": health.close_github_issue,
    }
    state = {
        health.TRUSTED_STATE_KEY: True,
        "active": True,
        "issue_number": 99,
    }
    saved: list[dict[str, object]] = []
    try:
        health.load_alert_state = lambda: dict(state)
        health.save_alert_state = lambda value: saved.append(dict(value))
        health.close_github_issue = lambda _state, _body: "GitHub recovery update failed: transient outage"

        message = health.handle_recovery_alert(
            {
                "detected_at_utc": "2026-08-02T20:00:00Z",
                "last_success_at_utc": "2026-08-02T19:59:00Z",
                "live_ok": True,
            }
        )

        assert message is not None and "closure will retry" in message
        assert saved[-1]["active"] is True
        assert saved[-1]["issue_number"] == 99
        assert "recovered_at_utc" not in saved[-1]
        assert saved[-1]["github_recovery_update"] == "GitHub recovery update failed: transient outage"
    finally:
        for name, value in originals.items():
            setattr(health, name, value)


def test_mutation_guard_rechecks_refresh_lock_and_running_service() -> None:
    originals = {
        "REFRESH_LOCK_PATH": health.REFRESH_LOCK_PATH,
        "DRY_RUN": health.DRY_RUN,
        "maybe_run": health.maybe_run,
        "launchctl_print": health.launchctl_print,
    }
    with tempfile.TemporaryDirectory() as directory:
        lock_path = Path(directory) / "locks" / "refresh.lock"
        lock_path.parent.mkdir(mode=0o700)
        lock_path.touch(mode=0o600)
        calls: list[list[str]] = []

        def fake_maybe_run(
            lines: list[str],
            description: str,
            cmd: list[str],
            *,
            cwd: Path | None = health.REPO_DIR,
            timeout: int = 90,
        ) -> object:
            del lines, description, cwd, timeout
            calls.append(cmd)
            return health.Result(0, "", "")

        try:
            health.REFRESH_LOCK_PATH = lock_path
            health.DRY_RUN = False
            health.maybe_run = fake_maybe_run
            health.launchctl_print = lambda _label: health.Result(0, "state = not running", "")

            fd = os.open(lock_path, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                lines: list[str] = []
                result = health.maybe_run_with_refresh_guard(lines, "mutate git", ["git", "switch", "main"])
                assert result.code == 75
                assert calls == []
                assert any("refresh lock is active" in line for line in lines)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

            health.launchctl_print = lambda _label: health.Result(0, "state = running", "")
            lines = []
            result = health.maybe_run_with_refresh_guard(
                lines,
                "reinstall worker",
                ["bash", "installer.sh"],
                require_idle_label="worker.label",
            )
            assert result.code == 75
            assert calls == []
            assert any("currently running" in line for line in lines)

            health.launchctl_print = lambda _label: health.Result(0, "state = not running", "")
            lines = []
            result = health.maybe_run_with_refresh_guard(
                lines,
                "kickstart worker",
                ["launchctl", "kickstart", "worker.label"],
                require_idle_label="worker.label",
                release_before_run=True,
            )
            assert result.code == 0
            assert calls == [["launchctl", "kickstart", "worker.label"]]
            assert health.refresh_is_active() is False
        finally:
            for name, value in originals.items():
                setattr(health, name, value)


def test_active_watcher_filters_only_completion_lag_issues() -> None:
    issues = [
        "watcher state missing: /tmp/state.json",
        "watcher state has no valid last_checked_at_utc",
        "watcher state age=12m exceeds threshold=5m",
        "watcher has 3 consecutive RPC failures",
        "watcher has 4 consecutive refresh failures",
        "watcher pending refresh age=20m exceeds threshold=15m",
        "watcher state unreadable: JSONDecodeError",
    ]
    filtered = health.filter_watcher_issues_for_active_attempt(issues, True)
    assert filtered == issues[3:]
    assert health.filter_watcher_issues_for_active_attempt(issues, False) == issues


def test_launchd_cause_requires_an_explicit_launchd_fault() -> None:
    benign = health.derive_causes(
        issues=[
            "issue: refresh appears stale/failed, but launchd job is currently running; left it alone",
            "issue: watcher state is unhealthy, but the watcher job is currently running; left it alone",
        ],
        dirty_paths=[],
        log_details={},
        stale=False,
        failed_last=False,
        live_ok=True,
        launch_output="state = running",
        now=0,
    )
    assert "launchd_agent_unhealthy_or_drifted" not in benign

    drifted = health.derive_causes(
        issues=["issue: onchain auction watcher launchd plist drift: StartInterval"],
        dirty_paths=[],
        log_details={},
        stale=False,
        failed_last=False,
        live_ok=True,
        launch_output="state = not running",
        now=0,
    )
    assert "launchd_agent_unhealthy_or_drifted" in drifted


def test_expected_live_publish_lag_is_narrow() -> None:
    assert health.expected_live_publish_lag(
        "live refresh status block 100 trails local generated block 101"
    ) is True
    assert health.expected_live_publish_lag(
        "live refresh status current_bid_eth differs from local validated status at block 101"
    ) is True
    assert health.expected_live_publish_lag("live HTTP status 503") is False
    assert health.expected_live_publish_lag("live refresh status payload is invalid") is False


def valid_plist(spec: object) -> dict[str, object]:
    required_environment = dict(getattr(spec, "required_environment"))
    return {
        "Label": getattr(spec, "label"),
        "ProgramArguments": list(getattr(spec, "program_arguments")),
        "WorkingDirectory": str(health.REPO_DIR),
        "StartInterval": getattr(spec, "interval_seconds"),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "ThrottleInterval": getattr(spec, "throttle_interval"),
        "Umask": 0o077,
        "StandardOutPath": str(getattr(spec, "standard_out_path")),
        "StandardErrorPath": str(getattr(spec, "standard_error_path")),
        "EnvironmentVariables": required_environment,
    }


def test_runner_path_prefers_executable_repo_virtualenv() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        python_path = root / ".venv" / "bin" / "python3"
        python_path.parent.mkdir(parents=True)
        python_path.write_text(
            f"#!/bin/sh\nexec {shlex.quote(str(TEST_RUNTIME_PYTHON))} \"$@\"\n",
            encoding="utf-8",
        )
        python_path.chmod(0o700)
        shim_path = root / "scripts" / "runtime-bin" / "python3"
        shim_path.parent.mkdir(parents=True)
        shim_path.write_text("#!/bin/sh\n", encoding="utf-8")
        shim_path.chmod(0o700)
        assert health.runner_python_ready(root) is True
        assert health.runner_path_value(root).startswith(f"{root}/scripts/runtime-bin:")
        python_path.unlink()
        python_path.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        python_path.chmod(0o700)
        assert health.runner_python_ready(root) is False
        assert health.runner_path_value(root) == health.BASE_PATH_VALUE
        assert health.runner_path_value(Path(f"{root}:unsafe")) == health.BASE_PATH_VALUE


def test_launchd_plist_validation_covers_hourly_and_watcher() -> None:
    original = {
        "HOME": health.HOME,
        "REPO_DIR": health.REPO_DIR,
        "REFRESH_SCRIPT": health.REFRESH_SCRIPT,
        "WATCHER_SCRIPT": health.WATCHER_SCRIPT,
        "HOURLY_INSTALL_SCRIPT": health.HOURLY_INSTALL_SCRIPT,
        "WATCHER_INSTALL_SCRIPT": health.WATCHER_INSTALL_SCRIPT,
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            health.HOME = root / "home"
            health.REPO_DIR = root / "repo"
            health.REFRESH_SCRIPT = health.REPO_DIR / "scripts" / "refresh_and_publish.sh"
            health.WATCHER_SCRIPT = health.REPO_DIR / "scripts" / "watch_mission3_onchain_activity.py"
            health.HOURLY_INSTALL_SCRIPT = health.REPO_DIR / "scripts" / "install_hourly_refresh_launchd.sh"
            health.WATCHER_INSTALL_SCRIPT = health.REPO_DIR / "scripts" / "install_auction_watcher_launchd.sh"
            plist_dir = health.HOME / "Library" / "LaunchAgents"
            plist_dir.mkdir(parents=True)
            python_path = health.REPO_DIR / ".venv" / "bin" / "python3"
            python_path.parent.mkdir(parents=True)
            python_path.write_text(
                f"#!/bin/sh\nexec {shlex.quote(str(TEST_RUNTIME_PYTHON))} \"$@\"\n",
                encoding="utf-8",
            )
            python_path.chmod(0o700)
            shim_path = health.REPO_DIR / "scripts" / "runtime-bin" / "python3"
            shim_path.parent.mkdir(parents=True)
            shim_path.write_text("#!/bin/sh\n", encoding="utf-8")
            shim_path.chmod(0o700)

            hourly, watcher = health.launchd_specs()
            hourly_environment = dict(hourly.required_environment)
            watcher_environment = dict(watcher.required_environment)
            assert hourly.name == "hourly reconcile refresh"
            assert hourly_environment["PATH"] == f"{health.REPO_DIR}/scripts/runtime-bin:{health.BASE_PATH_VALUE}"
            assert hourly_environment["DEGEN_DOGS_FULL_REFRESH"] == health.HOURLY_FULL_REFRESH
            assert hourly_environment["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == health.HOURLY_RUN_MISSION3_ARCHIVE
            assert watcher_environment["DEGEN_DOGS_FULL_REFRESH"] == "0"
            assert watcher_environment["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "0"
            assert (
                watcher_environment["MISSION3_WATCHER_REQUIRE_CLEAN_TREE"]
                == health.WATCHER_REQUIRE_CLEAN_TREE
            )
            assert (
                watcher_environment["MISSION3_WATCHER_REFRESH_TIMEOUT_SECONDS"]
                == health.WATCHER_REFRESH_TIMEOUT_SECONDS
            )

            for service in (hourly, watcher):
                service.plist_path.write_bytes(plistlib.dumps(valid_plist(service)))
                issues: list[str] = []
                assert health.plist_needs_reinstall(issues, service) is False
                assert issues == []

                drifted = valid_plist(service)
                drifted["EnvironmentVariables"]["PATH"] = health.BASE_PATH_VALUE  # type: ignore[index]
                service.plist_path.write_bytes(plistlib.dumps(drifted))
                issues = []
                assert health.plist_needs_reinstall(issues, service) is True
                assert "EnvironmentVariables.PATH" in issues[0]

                drifted = valid_plist(service)
                drifted["RunAtLoad"] = False
                service.plist_path.write_bytes(plistlib.dumps(drifted))
                issues = []
                assert health.plist_needs_reinstall(issues, service) is True
                assert "RunAtLoad" in issues[0]

                drifted = valid_plist(service)
                drifted["Umask"] = 0o022
                drifted["StandardOutPath"] = "/tmp/public-runner.log"
                drifted["EnvironmentVariables"]["DEGEN_DOGS_LOCK_DIR"] = "/tmp/wrong-locks"  # type: ignore[index]
                drifted["EnvironmentVariables"]["DYLD_INSERT_LIBRARIES"] = "/tmp/inject.dylib"  # type: ignore[index]
                service.plist_path.write_bytes(plistlib.dumps(drifted))
                issues = []
                assert health.plist_needs_reinstall(issues, service) is True
                assert "Umask" in issues[0]
                assert "StandardOutPath" in issues[0]
                assert "EnvironmentVariables.DEGEN_DOGS_LOCK_DIR" in issues[0]
                assert "EnvironmentVariables.DYLD_INSERT_LIBRARIES" in issues[0]

            drifted = valid_plist(watcher)
            drifted["EnvironmentVariables"]["MISSION3_WATCHER_REQUIRE_CLEAN_TREE"] = (  # type: ignore[index]
                "0" if health.WATCHER_REQUIRE_CLEAN_TREE == "1" else "1"
            )
            watcher.plist_path.write_bytes(plistlib.dumps(drifted))
            issues = []
            assert health.plist_needs_reinstall(issues, watcher) is True
            assert "EnvironmentVariables.MISSION3_WATCHER_REQUIRE_CLEAN_TREE" in issues[0]
        finally:
            for name, value in original.items():
                setattr(health, name, value)


def test_launchd_hourly_policy_override_is_dynamic_but_watcher_is_fixed() -> None:
    original_full = health.HOURLY_FULL_REFRESH
    original_archive = health.HOURLY_RUN_MISSION3_ARCHIVE
    try:
        health.HOURLY_FULL_REFRESH = "1"
        health.HOURLY_RUN_MISSION3_ARCHIVE = "0"
        hourly, watcher = health.launchd_specs()
        hourly_environment = dict(hourly.required_environment)
        watcher_environment = dict(watcher.required_environment)
        assert hourly_environment["DEGEN_DOGS_FULL_REFRESH"] == "1"
        assert hourly_environment["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "0"
        assert watcher_environment["DEGEN_DOGS_FULL_REFRESH"] == "0"
        assert watcher_environment["DEGEN_DOGS_RUN_MISSION3_ARCHIVE"] == "0"
    finally:
        health.HOURLY_FULL_REFRESH = original_full
        health.HOURLY_RUN_MISSION3_ARCHIVE = original_archive


def test_timestamp_only_cleanup_revalidates_after_lock_acquisition() -> None:
    originals = {
        "DRY_RUN": health.DRY_RUN,
        "acquire_refresh_mutation_lock": health.acquire_refresh_mutation_lock,
        "release_refresh_mutation_lock": health.release_refresh_mutation_lock,
        "generated_price_change_is_timestamp_only": health.generated_price_change_is_timestamp_only,
        "maybe_run": health.maybe_run,
    }
    lock_owned = False
    calls: list[list[str]] = []

    def fake_acquire() -> tuple[int | None, str | None]:
        nonlocal lock_owned
        assert lock_owned is False
        lock_owned = True
        return 123, None

    def fake_release(descriptor: int | None) -> None:
        nonlocal lock_owned
        assert descriptor == 123
        assert lock_owned is True
        lock_owned = False

    def fake_compare(_path: str) -> bool:
        assert lock_owned is True
        return True

    def fake_run(
        lines: list[str],
        description: str,
        cmd: list[str],
        *,
        cwd: Path | None = health.REPO_DIR,
        timeout: int = 90,
    ) -> object:
        del lines, description, cwd, timeout
        assert lock_owned is True
        calls.append(cmd)
        return health.Result(0, "", "")

    try:
        health.DRY_RUN = False
        health.acquire_refresh_mutation_lock = fake_acquire
        health.release_refresh_mutation_lock = fake_release
        health.generated_price_change_is_timestamp_only = fake_compare
        health.maybe_run = fake_run
        path = sorted(health.PRICE_TIMESTAMP_ONLY_PATHS)[0]
        lines: list[str] = []
        assert health.clean_timestamp_only_price_changes(lines, [path]) is True
        assert calls == [["git", "checkout", "--", path]]
        assert lock_owned is False
    finally:
        for name, value in originals.items():
            setattr(health, name, value)


def test_watcher_state_health() -> None:
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "watcher-state.json"
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc).timestamp()
        state_path.write_text(
            json.dumps(
                {
                    "last_checked_at_utc": "2026-08-02T11:59:30Z",
                    "last_checked_block": 123,
                    "last_observed_block": 122,
                    "consecutive_rpc_failures": 0,
                    "consecutive_refresh_failures": 0,
                    "pending_refresh": False,
                    "last_refresh_status": "success",
                }
            ),
            encoding="utf-8",
        )
        issues, summary = health.inspect_watcher_state(now, state_path)
        assert issues == []
        assert summary["last_checked_age_seconds"] == 30

        state_path.write_text(
            json.dumps(
                {
                    "last_checked_at_utc": "2026-08-02T11:30:00Z",
                    "consecutive_rpc_failures": 3,
                    "consecutive_refresh_failures": 4,
                    "pending_refresh": True,
                    "pending_refresh_since_utc": "2026-08-02T11:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        issues, summary = health.inspect_watcher_state(now, state_path)
        assert any("state age" in issue for issue in issues)
        assert any("3 consecutive RPC failures" in issue for issue in issues)
        assert any("4 consecutive refresh failures" in issue for issue in issues)
        assert any("pending refresh age" in issue for issue in issues)
        assert summary["pending_refresh"] is True


def test_watcher_state_health_rejects_broad_files_and_symlinks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        state_path = root / "watcher-state.json"
        state_path.write_text("{}", encoding="utf-8")
        state_path.chmod(0o644)
        issues, _summary = health.inspect_watcher_state(time.time(), state_path)
        assert any("unsafe/unreadable" in issue or "not a protected owned regular file" in issue for issue in issues)

        target = root / "target.json"
        target.write_text("{}", encoding="utf-8")
        target.chmod(0o600)
        linked = root / "linked.json"
        linked.symlink_to(target)
        issues, _summary = health.inspect_watcher_state(time.time(), linked)
        assert any("unsafe/unreadable" in issue for issue in issues)


def test_log_compaction_is_bounded_and_preserves_launchd_inode() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "launchd.out.log"
        path.write_text("".join(f"line-{index:03d}-{'x' * 24}\n" for index in range(100)), encoding="utf-8")
        inode_before = path.stat().st_ino

        rotated, before, after = health.compact_log_in_place(path, max_bytes=512, retain_bytes=320)

        content = path.read_text(encoding="utf-8")
        assert rotated is True
        assert before > 512
        assert after <= 512
        assert path.stat().st_ino == inode_before
        assert "log compacted in place" in content
        assert "line-099" in content
        assert "line-000" not in content


def test_managed_log_inventory_includes_all_high_growth_jsonl_files() -> None:
    paths = {item.path for item in health.managed_logs()}
    assert health.REPO_DIR / ".local" / "watcher_checks.jsonl" in paths
    assert health.REPO_DIR / ".local" / "refresh_runs.jsonl" in paths
    assert health.REPO_DIR / "logs" / "refresh-metrics.jsonl" in paths


def test_runner_permission_hardening_repairs_modes_and_refuses_symlinks() -> None:
    originals = {
        "DRY_RUN": health.DRY_RUN,
        "private_runner_directories": health.private_runner_directories,
        "private_runner_files": health.private_runner_files,
    }
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        private_dir = root / "state"
        private_dir.mkdir(mode=0o755)
        private_dir.chmod(0o755)
        private_file = private_dir / "telemetry.jsonl"
        private_file.write_text('{"result":"healthy"}\n', encoding="utf-8")
        private_file.chmod(0o644)
        inode = private_file.stat().st_ino
        try:
            health.DRY_RUN = False
            health.private_runner_directories = lambda: (private_dir,)
            health.private_runner_files = lambda: (private_file,)
            lines: list[str] = []

            assert health.harden_runner_permissions(lines) is False
            assert private_dir.stat().st_mode & 0o777 == 0o700
            assert private_file.stat().st_mode & 0o777 == 0o600
            assert private_file.stat().st_ino == inode
            assert json.loads(private_file.read_text(encoding="utf-8"))["result"] == "healthy"

            target = root / "unrelated.json"
            target.write_text('{"secret":true}\n', encoding="utf-8")
            target.chmod(0o644)
            linked = private_dir / "linked-state.json"
            linked.symlink_to(target)
            health.private_runner_files = lambda: (linked,)
            lines = []
            assert health.harden_runner_permissions(lines) is True
            assert target.stat().st_mode & 0o777 == 0o644
            assert any("permission hardening failed" in line for line in lines)

            nested_parent = root / "nested-permission-parent"
            nested_target = root / "nested-permission-target"
            nested_parent.mkdir()
            nested_target.mkdir()
            nested_file = nested_target / "private.json"
            nested_file.write_text('{"keep":true}\n', encoding="utf-8")
            nested_file.chmod(0o644)
            (nested_parent / "redirect").symlink_to(nested_target, target_is_directory=True)
            health.private_runner_directories = lambda: (nested_parent / "redirect" / "created",)
            health.private_runner_files = lambda: (nested_parent / "redirect" / "private.json",)
            lines = []
            assert health.harden_runner_permissions(lines) is True
            assert not (nested_target / "created").exists()
            assert nested_file.stat().st_mode & 0o777 == 0o644
            assert json.loads(nested_file.read_text(encoding="utf-8"))["keep"] is True
        finally:
            for name, value in originals.items():
                setattr(health, name, value)


def test_runner_environment_file_monitoring_is_descriptor_safe_and_optional_by_default() -> None:
    original_repo = health.REPO_DIR
    original_dry_run = health.DRY_RUN
    original_env_file = os.environ.get("DEGEN_DOGS_ENV_FILE")
    secret_sentinel = "ENV_FILE_SECRET_MUST_NOT_APPEAR"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        repo = root / "repo"
        repo.mkdir()
        default_env = repo / ".env.local"
        try:
            health.REPO_DIR = repo
            health.DRY_RUN = False
            os.environ.pop("DEGEN_DOGS_ENV_FILE", None)

            # The default file is optional when it has never been created.
            lines: list[str] = []
            assert health.harden_runner_environment_file(lines) is False
            assert lines == []

            # Mode drift is diagnosed without mutation in dry-run mode, then
            # repaired in place through the descriptor-safe private-file helper.
            default_env.write_text(f"BASE_RPC_URL={secret_sentinel}\n", encoding="utf-8")
            default_env.chmod(0o644)
            inode = default_env.stat().st_ino
            health.DRY_RUN = True
            lines = []
            assert health.harden_runner_environment_file(lines) is False
            assert default_env.stat().st_mode & 0o777 == 0o644
            assert any("DRY-RUN would set default runner environment file" in line for line in lines)
            assert secret_sentinel not in "\n".join(lines)

            health.DRY_RUN = False
            lines = []
            assert health.harden_runner_environment_file(lines) is False
            assert default_env.stat().st_mode & 0o777 == 0o600
            assert default_env.stat().st_ino == inode
            assert secret_sentinel not in "\n".join(lines)

            # A final-component symlink must never chmod or disclose its target.
            default_env.unlink()
            symlink_target = root / "symlink-target.env"
            symlink_target.write_text(secret_sentinel, encoding="utf-8")
            symlink_target.chmod(0o640)
            default_env.symlink_to(symlink_target)
            lines = []
            assert health.harden_runner_environment_file(lines) is True
            assert symlink_target.stat().st_mode & 0o777 == 0o640
            assert symlink_target.read_text(encoding="utf-8") == secret_sentinel
            assert secret_sentinel not in "\n".join(lines)

            # A hard-linked file has ambiguous identity and is not repairable.
            default_env.unlink()
            hardlink_source = root / "hardlink-source.env"
            hardlink_source.write_text(secret_sentinel, encoding="utf-8")
            hardlink_source.chmod(0o600)
            os.link(hardlink_source, default_env)
            lines = []
            assert health.harden_runner_environment_file(lines) is True
            assert hardlink_source.stat().st_nlink == 2
            assert secret_sentinel not in "\n".join(lines)
            default_env.unlink()
            hardlink_source.unlink()

            # An explicit path is monitored and repaired; unlike the default,
            # its later absence is actionable configuration drift.
            configured_dir = repo / "configured"
            configured_dir.mkdir()
            path_secret_sentinel = "PATH_SECRET_MUST_NOT_APPEAR"
            configured_env = configured_dir / f"{path_secret_sentinel}.env"
            configured_env.write_text(f"BASE_LOG_RPC_URLS={secret_sentinel}\n", encoding="utf-8")
            configured_env.chmod(0o640)
            os.environ["DEGEN_DOGS_ENV_FILE"] = str(configured_env)
            lines = []
            assert health.harden_runner_environment_file(lines) is False
            assert configured_env.stat().st_mode & 0o777 == 0o600
            assert secret_sentinel not in "\n".join(lines)
            assert path_secret_sentinel not in "\n".join(lines)

            configured_env.unlink()
            lines = []
            assert health.harden_runner_environment_file(lines) is True
            assert any("configured runner environment file is missing" in line for line in lines)
            assert secret_sentinel not in "\n".join(lines)
            assert path_secret_sentinel not in "\n".join(lines)
            incident = health.build_incident_body({"issues": lines})
            assert secret_sentinel not in incident
            assert path_secret_sentinel not in incident

            # Installer stderr can be relayed through health repair findings.
            # Global sanitization must remove the configured path before an
            # outside-HOME secret filename can enter a GitHub incident body.
            installer_error = f"runner env file must be mode 600: {configured_env}"
            sanitized_error = health.sanitize(installer_error)
            assert "<runner-env>" in sanitized_error
            assert path_secret_sentinel not in sanitized_error
            incident = health.build_incident_body({"issues": [installer_error]})
            assert "<runner-env>" in incident
            assert path_secret_sentinel not in incident

            os.environ["DEGEN_DOGS_ENV_FILE"] = "e"
            ordinary_alert = "service remains healthy despite an unrelated retry"
            assert health.sanitize(ordinary_alert) == ordinary_alert
            assert health.sanitize(f"installer rejected path: {health.REPO_DIR / 'e'}").endswith(
                "<runner-env>"
            )
            assert health.sanitize("installer rejected path: e").endswith("<runner-env>")
            os.environ["DEGEN_DOGS_ENV_FILE"] = os.sep
            root_path_alert = "root path / must not erase ordinary slash-delimited output"
            assert health.sanitize(root_path_alert) == root_path_alert
            os.environ["DEGEN_DOGS_ENV_FILE"] = "~missing-runner-user/secret.env"
            assert health.sanitize(ordinary_alert) == ordinary_alert
            os.environ["DEGEN_DOGS_ENV_FILE"] = str(configured_env)

            # Unsafe parent substitution is rejected before the target is opened.
            configured_env.write_text(secret_sentinel, encoding="utf-8")
            configured_env.chmod(0o600)
            redirected_parent = repo / "redirected-config"
            redirected_parent.symlink_to(configured_dir, target_is_directory=True)
            os.environ["DEGEN_DOGS_ENV_FILE"] = str(redirected_parent / f"{path_secret_sentinel}.env")
            lines = []
            assert health.harden_runner_environment_file(lines) is True
            assert configured_env.read_text(encoding="utf-8") == secret_sentinel
            assert secret_sentinel not in "\n".join(lines)
            assert path_secret_sentinel not in "\n".join(lines)
            incident = health.build_incident_body({"issues": lines})
            assert secret_sentinel not in incident
            assert path_secret_sentinel not in incident

            # Exercise the unexpected-owner branch without requiring privileged
            # chown: /tmp is root-owned, so changing the helper's expected uid
            # reaches the final-file ownership check rather than an ancestor.
            owner_fd, owner_name = tempfile.mkstemp(prefix="degen-dogs-env-owner-", dir="/tmp")
            owner_path = Path(owner_name)
            try:
                os.write(owner_fd, secret_sentinel.encode("utf-8"))
            finally:
                os.close(owner_fd)
            owner_path.chmod(0o600)
            original_expected_uid = path_security._CURRENT_UID
            try:
                path_security._CURRENT_UID = os.getuid() + 1
                os.environ["DEGEN_DOGS_ENV_FILE"] = str(owner_path)
                lines = []
                assert health.harden_runner_environment_file(lines) is True
                assert any(
                    "configured runner environment file: SecurePathError" in line
                    for line in lines
                )
                assert owner_path.read_text(encoding="utf-8") == secret_sentinel
                assert secret_sentinel not in "\n".join(lines)
            finally:
                path_security._CURRENT_UID = original_expected_uid
                owner_path.unlink(missing_ok=True)
        finally:
            health.REPO_DIR = original_repo
            health.DRY_RUN = original_dry_run
            if original_env_file is None:
                os.environ.pop("DEGEN_DOGS_ENV_FILE", None)
            else:
                os.environ["DEGEN_DOGS_ENV_FILE"] = original_env_file


def test_log_compaction_refuses_symlinks() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        target = root / "target.log"
        link = root / "health.log"
        target.write_bytes(b"sensitive" * 100)
        link.symlink_to(target)
        try:
            health.compact_log_in_place(link, max_bytes=100, retain_bytes=20)
        except ValueError as exc:
            assert "non-regular" in str(exc)
        else:
            raise AssertionError("expected symlink log compaction to be refused")
        assert target.read_bytes() == b"sensitive" * 100

        nested_parent = root / "nested-log-parent"
        nested_target = root / "nested-log-target"
        nested_parent.mkdir()
        nested_target.mkdir()
        nested_log = nested_target / "health.log"
        nested_log.write_bytes(b"keep" * 100)
        nested_log.chmod(0o600)
        (nested_parent / "redirect").symlink_to(nested_target, target_is_directory=True)
        try:
            health.compact_log_in_place(
                nested_parent / "redirect" / "health.log",
                max_bytes=100,
                retain_bytes=20,
            )
        except ValueError as exc:
            assert "unsafe/non-regular" in str(exc)
        else:
            raise AssertionError("expected nested symlink log ancestor to be refused")
        assert nested_log.read_bytes() == b"keep" * 100


def test_jsonl_compaction_retains_complete_latest_rows() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "watcher_checks.jsonl"
        path.write_text(
            "".join(json.dumps({"id": index, "result": "no_refresh"}) + "\n" for index in range(100)),
            encoding="utf-8",
        )

        rotated, _before, after = health.compact_log_in_place(path, max_bytes=600, retain_bytes=420)
        rows = health.read_jsonl_tail(path, 100)

        assert rotated is True
        assert after <= 600
        assert rows
        assert rows[-1]["id"] == 99
        assert all(isinstance(row.get("id"), int) for row in rows)


def test_active_log_defers_until_emergency_cap() -> None:
    originals = {
        "managed_logs": health.managed_logs,
        "LOG_MAX_BYTES": health.LOG_MAX_BYTES,
        "LOG_RETAIN_BYTES": health.LOG_RETAIN_BYTES,
        "LOG_EMERGENCY_MAX_BYTES": health.LOG_EMERGENCY_MAX_BYTES,
        "DRY_RUN": health.DRY_RUN,
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "watcher.log"
        try:
            health.LOG_MAX_BYTES = 100
            health.LOG_RETAIN_BYTES = 30
            health.LOG_EMERGENCY_MAX_BYTES = 300
            health.DRY_RUN = False
            health.managed_logs = lambda: (
                health.ManagedLog(path, (health.WATCHER_LABEL,), "watcher test log"),
            )

            path.write_bytes(b"x" * 200)
            lines: list[str] = []
            assert health.rotate_managed_logs(lines, {health.WATCHER_LABEL}) is False
            assert path.stat().st_size == 200
            assert lines == []

            path.write_bytes(b"y" * 400)
            lines = []
            assert health.rotate_managed_logs(lines, {health.WATCHER_LABEL}) is False
            assert path.stat().st_size <= 100
            assert any("emergency compacted" in line for line in lines)
        finally:
            for name, value in originals.items():
                setattr(health, name, value)


def test_disk_free_thresholds() -> None:
    DiskUsage = namedtuple("DiskUsage", "total used free")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        healthy_issues, healthy_summary = health.inspect_disk_free(
            [path],
            min_free_bytes=200,
            min_free_percent=15,
            usage_fn=lambda _path: DiskUsage(1000, 500, 500),
        )
        assert healthy_issues == []
        assert healthy_summary[0]["free_percent"] == 50.0

        low_issues, low_summary = health.inspect_disk_free(
            [path],
            min_free_bytes=200,
            min_free_percent=15,
            usage_fn=lambda _path: DiskUsage(1000, 900, 100),
        )
        assert len(low_issues) == 1
        assert "runner disk free space low" in low_issues[0]
        assert low_summary[0]["free_bytes"] == 100


if __name__ == "__main__":
    test_live_site_transport_accepts_only_exact_bounded_targets()
    test_default_live_status_freshness_window_is_ninety_minutes()
    test_live_site_transport_rejects_unapproved_redirect_status_mime_and_oversize()
    test_live_site_transport_sanitizes_http_errors_and_rejects_invalid_json()
    test_refresh_lock_detection()
    test_active_lock_metadata_requires_a_held_flock()
    test_fresh_active_attempt_requires_new_held_bounded_run()
    test_alert_state_is_atomic_private_and_rejects_untrusted_files()
    test_issue_discovery_requires_marker_and_authenticated_author()
    test_issue_update_and_close_ignore_untrusted_state_ids()
    test_failed_issue_recovery_remains_active_for_retry()
    test_mutation_guard_rechecks_refresh_lock_and_running_service()
    test_active_watcher_filters_only_completion_lag_issues()
    test_launchd_cause_requires_an_explicit_launchd_fault()
    test_expected_live_publish_lag_is_narrow()
    test_launchd_plist_validation_covers_hourly_and_watcher()
    test_launchd_hourly_policy_override_is_dynamic_but_watcher_is_fixed()
    test_timestamp_only_cleanup_revalidates_after_lock_acquisition()
    test_watcher_state_health()
    test_log_compaction_is_bounded_and_preserves_launchd_inode()
    test_managed_log_inventory_includes_all_high_growth_jsonl_files()
    test_runner_permission_hardening_repairs_modes_and_refuses_symlinks()
    test_runner_environment_file_monitoring_is_descriptor_safe_and_optional_by_default()
    test_log_compaction_refuses_symlinks()
    test_jsonl_compaction_retains_complete_latest_rows()
    test_active_log_defers_until_emergency_cap()
    test_disk_free_thresholds()
    print("degen dogs runner health tests passed")
