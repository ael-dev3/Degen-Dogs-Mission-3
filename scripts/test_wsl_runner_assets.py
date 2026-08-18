#!/usr/bin/env python3
"""Static regression checks for the WSL2/systemd runner package."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text(relative: str) -> str:
    path = ROOT / relative
    raw = path.read_bytes()
    assert b"\r\n" not in raw, f"{relative} must use LF line endings"
    return raw.decode("utf-8", errors="strict")


def test() -> None:
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
        assert "Restart=on-failure" in service
        assert "@LOCK_DIR@" in service
        assert "ConditionPathExists=/run/degen-dogs/activation-enabled" in service

    launcher = text("scripts/run_wsl_runner_job.sh")
    assert "MISSION3_WATCHER_AUTO_PUSH=1" in launcher
    assert "DEGEN_DOGS_RUN_MISSION3_ARCHIVE=0" in launcher
    assert "DEGEN_DOGS_RUN_MISSION3_ARCHIVE=1" in launcher
    assert "preflight_wsl_rpc.py" in launcher
    assert "MISSION3_WATCHER_LOG_PATH=-" in launcher
    assert "DEGEN_DOGS_REFRESH_LOCK_PATH" in launcher
    assert "export DEGEN_DOGS_REMOTE=origin" in launcher
    assert "export DEGEN_DOGS_BRANCH=main" in launcher
    assert "export DEGEN_DOGS_SKIP_PUSH=0" in launcher
    assert "remote.origin.pushurl" in launcher

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
    assert "systemctl disable --now" in installer
    assert "--uninstall" in installer

    powershell = text("scripts/install_wsl_startup_task.ps1")
    assert "New-ScheduledTaskAction" in powershell
    assert "-Disable `" in powershell
    assert "degen-dogs-wsl-anchor" in powershell
    assert "-AtStartup" in powershell and "-AtLogOn" in powershell
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in powershell
    assert "$AtLogOnOnly" in powershell
    assert "Assert-CurrentAccountCredential" in powershell
    assert 'merge --ff-only "$runtime_sha"' in powershell
    assert "refs/heads/main" in powershell
    assert "could not quiesce %s before runner upgrade" in powershell
    assert "systemctl is-active --quiet" in powershell
    assert "$UpgradeTrustedBundle" in powershell
    assert "TrustedInstallerCommit" in powershell
    assert "ROOT_ASSETS.sha256" in powershell
    assert 'git -c core.hooksPath=/dev/null --git-dir="$stage/repo.git" archive' in powershell
    assert "/run/degen-dogs/anchor-ready" in powershell
    assert "catch {\n        $activationError = $_\n        try {" in powershell

    anchor = text("scripts/run_wsl_runner_anchor.sh")
    assert "activation-armed" in anchor and "activation-enabled" in anchor
    assert "systemctl is-enabled --quiet" in anchor

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
    ast.parse(health)
    assert "current_dog_token_id" in health

    print(f"wsl_runner_asset_tests=pass count={len(required)}")


if __name__ == "__main__":
    test()
