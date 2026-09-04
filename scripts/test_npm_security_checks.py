#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stderr
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_npm_security_checks.py")
CI_WORKFLOW_PATH = MODULE_PATH.parent.parent / ".github" / "workflows" / "ci.yml"


def load_module():
    spec = importlib.util.spec_from_file_location("run_npm_security_checks", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


FAKE_NPM = r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
import time
from pathlib import Path

plan_path = Path(os.environ["FAKE_NPM_PLAN"])
log_path = Path(os.environ["FAKE_NPM_LOG"])
plan = json.loads(plan_path.read_text(encoding="utf-8"))
kind = "signatures" if len(sys.argv) > 2 and sys.argv[2] == "signatures" else "vulnerabilities"

calls = []
if log_path.exists():
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
attempt = sum(1 for call in calls if call["kind"] == kind)
response = plan[kind][min(attempt, len(plan[kind]) - 1)]
required_flags = {
    "--global=false",
    "--package-lock=true",
    "--offline=false",
    "--prefer-offline=false",
    "--prefer-online=true",
    "--strict-ssl=true",
    "--registry=https://registry.npmjs.org/",
    "--include=prod",
    "--include=dev",
    "--include=optional",
    "--include=peer",
}
unsafe_env_keys = sorted(
    key
    for key in os.environ
    if key.upper().startswith("NPM_CONFIG_")
    or key.upper() in {"NODE_ENV", "NODE_OPTIONS", "NODE_PATH"}
)
has_bound_paths = (
    any(arg == f"--prefix={Path.cwd()}" for arg in sys.argv[1:])
    and any(arg.startswith("--userconfig=") for arg in sys.argv[1:])
    and any(arg.startswith("--globalconfig=") for arg in sys.argv[1:])
)
if (
    os.environ.get("FAKE_NPM_SIMULATE_CONFIG_BYPASS") == "1"
    and (unsafe_env_keys or not required_flags.issubset(sys.argv[1:]) or not has_bound_paths)
):
    response = plan["config_bypass"][kind]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(
        json.dumps(
            {
                "kind": kind,
                "args": sys.argv[1:],
                "cwd": os.getcwd(),
                "unsafe_env_keys": unsafe_env_keys,
                "preserved_env": {
                    key: os.environ.get(key)
                    for key in (
                        "HTTPS_PROXY",
                        "NO_PROXY",
                        "NODE_EXTRA_CA_CERTS",
                        "SSL_CERT_FILE",
                    )
                },
            }
        )
        + "\n"
    )

if response.get("hold_pipe_seconds"):
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import time; time.sleep({float(response['hold_pipe_seconds'])!r})",
        ]
    )
time.sleep(response.get("sleep_seconds", 0))
if response.get("stdout"):
    print(response["stdout"])
if response.get("stderr"):
    print(response["stderr"], file=sys.stderr)
raise SystemExit(response["exit_code"])
'''


def invoke_gate(
    module,
    plan: dict,
    *,
    attempts: int = 2,
    timeout_seconds: float = 15.0,
    argv: list[str] | None = None,
    environ_updates: dict[str, str] | None = None,
    bind_checkout: bool = False,
):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_npm = tmp_path / "fake_npm.py"
        fake_npm.write_text(FAKE_NPM, encoding="utf-8")
        plan_path = tmp_path / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        log_path = tmp_path / "calls.jsonl"
        env = os.environ.copy()
        env.update({"FAKE_NPM_PLAN": str(plan_path), "FAKE_NPM_LOG": str(log_path)})
        env.update(environ_updates or {})
        output = io.StringIO()
        kwargs = {
            "command_prefix": [sys.executable, str(fake_npm)],
            "environ": env,
            "output": output,
        }
        if bind_checkout:
            checkout_root = tmp_path / "checkout"
            checkout_root.mkdir()
            kwargs["checkout_root"] = checkout_root
        if argv is None:
            result = module.run_gate(
                attempts=attempts,
                timeout_seconds=timeout_seconds,
                retry_delay_seconds=0,
                **kwargs,
            )
        else:
            result = module.main(argv, **kwargs)
        calls = (
            [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            if log_path.exists()
            else []
        )
        return result, calls, output.getvalue()


def clean_plan() -> dict:
    return {
        "vulnerabilities": [
            {
                "exit_code": 0,
                "stdout": json.dumps(
                    {
                        "auditReportVersion": 2,
                        "vulnerabilities": {},
                        "metadata": {"vulnerabilities": {"high": 0, "critical": 0, "total": 0}},
                    }
                ),
            }
        ],
        "signatures": [{"exit_code": 0, "stdout": '{"invalid": [], "missing": []}'}],
    }


def test_clean_gate_runs_both_checks_with_bounded_npm_network_settings() -> None:
    gate = load_module()
    result, calls, output = invoke_gate(gate, clean_plan())

    assert result == 0
    assert [call["kind"] for call in calls] == ["vulnerabilities", "signatures"]
    for call in calls:
        assert "--fetch-retries=0" in call["args"]
        assert "--fetch-timeout=15000" in call["args"]
        assert "--json" in call["args"]
    assert calls[0]["args"][:2] == ["audit", "--audit-level=high"]
    assert calls[1]["args"][:2] == ["audit", "signatures"]
    assert "npm security checks passed" in output


def test_transient_registry_503_is_retried_then_passes() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 1,
            "stderr": "npm warn audit 503 Service Unavailable - POST registry audit endpoint",
        },
        clean_plan()["vulnerabilities"][0],
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 0
    assert [call["kind"] for call in calls] == ["vulnerabilities", "vulnerabilities", "signatures"]
    assert "transient registry failure (attempt 1/2); retrying" in output


def test_transient_dns_and_socket_failures_are_retryable() -> None:
    gate = load_module()
    for message in (
        "npm error code EAI_AGAIN",
        "npm error code ENOTFOUND",
        "npm error code ECONNRESET",
        "npm error network timeout at: registry audit endpoint",
        "npm error 429 Too Many Requests",
        "npm error 504 Gateway Timeout",
    ):
        plan = clean_plan()
        plan["vulnerabilities"] = [
            {"exit_code": 1, "stderr": message},
            clean_plan()["vulnerabilities"][0],
        ]

        result, calls, _output = invoke_gate(gate, plan)

        assert result == 0, message
        assert [call["kind"] for call in calls].count("vulnerabilities") == 2, message


def test_high_vulnerability_report_is_not_misclassified_as_registry_outage() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 1,
            "stdout": json.dumps(
                {
                    "auditReportVersion": 2,
                    "vulnerabilities": {
                        "bad-package": {
                            "severity": "high",
                            "via": [{"title": "503 Service Unavailable parser issue"}],
                        }
                    },
                    "metadata": {"vulnerabilities": {"high": 1, "critical": 0, "total": 1}},
                }
            ),
        }
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert [call["kind"] for call in calls].count("vulnerabilities") == 1
    assert "high-severity vulnerability policy violation" in output


def test_high_vulnerability_json_fails_even_if_npm_returns_zero() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "auditReportVersion": 2,
                    "vulnerabilities": {"bad-package": {"severity": "critical"}},
                    "metadata": {"vulnerabilities": {"high": 0, "critical": 1, "total": 1}},
                }
            ),
        }
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert [call["kind"] for call in calls].count("vulnerabilities") == 1
    assert "high-severity vulnerability policy violation" in output


def test_invalid_signature_is_not_misclassified_as_transient() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["signatures"] = [
        {
            "exit_code": 1,
            "stderr": "npm error EINTEGRITY invalid registry signature; detail mentions 503 Service Unavailable",
        }
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert [call["kind"] for call in calls].count("signatures") == 1
    assert "signature integrity policy violation" in output


def test_invalid_signature_json_fails_even_if_npm_returns_zero() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["signatures"] = [
        {
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "invalid": [{"name": "bad-package", "version": "1.0.0"}],
                    "missing": [],
                }
            ),
        }
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert [call["kind"] for call in calls].count("signatures") == 1
    assert "signature integrity policy violation" in output


def test_zero_exit_requires_structurally_valid_security_reports() -> None:
    gate = load_module()
    cases = (
        ("vulnerabilities", "not-json"),
        (
            "vulnerabilities",
            json.dumps({"auditReportVersion": 2, "vulnerabilities": {}, "metadata": {"vulnerabilities": {}}}),
        ),
        ("signatures", "not-json"),
        ("signatures", json.dumps({"invalid": []})),
    )
    for kind, stdout in cases:
        plan = clean_plan()
        plan[kind] = [{"exit_code": 0, "stdout": stdout}]

        result, _calls, output = invoke_gate(gate, plan)

        assert result == 1, (kind, stdout)
        assert f"invalid {kind} report from npm" in output


def test_persistent_registry_outage_fails_closed_with_distinct_tempfail() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {"exit_code": 1, "stderr": "npm warn audit 503 Service Unavailable"},
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 75
    assert [call["kind"] for call in calls] == ["vulnerabilities", "vulnerabilities", "signatures"]
    assert "::error title=npm registry unavailable::" in output


def test_hung_npm_process_is_killed_within_the_attempt_budget() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [{"exit_code": 0, "sleep_seconds": 5}]

    started = time.monotonic()
    result, calls, output = invoke_gate(gate, plan, timeout_seconds=2)
    elapsed = time.monotonic() - started

    assert result == 75
    assert elapsed < 6
    assert [call["kind"] for call in calls] == ["vulnerabilities", "vulnerabilities", "signatures"]
    assert "timed out after 2" in output


def test_non_transient_npm_failure_fails_immediately_as_security_error() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [{"exit_code": 1, "stderr": "npm error code E401 Unauthorized"}]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert [call["kind"] for call in calls] == ["vulnerabilities", "signatures"]
    assert "::error title=npm security gate failed::" in output


def test_cli_applies_an_explicit_single_attempt_budget() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [{"exit_code": 1, "stderr": "npm warn audit 503 Service Unavailable"}]

    result, calls, _output = invoke_gate(
        gate,
        plan,
        argv=["--attempts", "1", "--timeout-seconds", "10", "--retry-delay-seconds", "0"],
    )

    assert result == 75
    assert [call["kind"] for call in calls] == ["vulnerabilities", "signatures"]


def test_cli_rejects_values_that_could_disable_or_unbound_the_gate() -> None:
    gate = load_module()
    invalid_arguments = (
        ["--attempts", "0"],
        ["--attempts", "4"],
        ["--timeout-seconds", "0"],
        ["--timeout-seconds", "61"],
        ["--retry-delay-seconds", "-1"],
        ["--retry-delay-seconds", "11"],
    )
    for arguments in invalid_arguments:
        try:
            with redirect_stderr(io.StringIO()):
                gate.main(arguments, command_prefix=["must-not-run"])
        except SystemExit as exc:
            assert exc.code == 2, arguments
        else:
            raise AssertionError(f"unsafe security budget accepted: {arguments}")


def test_programmatic_gate_rejects_values_that_could_disable_or_unbound_it() -> None:
    gate = load_module()
    invalid_budgets = (
        {"attempts": 0},
        {"attempts": 4},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"retry_delay_seconds": -1},
        {"retry_delay_seconds": 11},
    )
    for override in invalid_budgets:
        budget = {"attempts": 2, "timeout_seconds": 30, "retry_delay_seconds": 3}
        budget.update(override)
        try:
            gate.run_gate(command_prefix=["must-not-run"], **budget)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe programmatic security budget accepted: {override}")


def test_missing_npm_executable_fails_closed_with_a_clear_error() -> None:
    gate = load_module()
    output = io.StringIO()

    result = gate.run_gate(
        command_prefix=["degen-dogs-definitely-missing-npm"],
        attempts=1,
        timeout_seconds=1,
        retry_delay_seconds=0,
        output=output,
    )

    assert result == 1
    assert "unable to start npm" in output.getvalue()
    assert "::error title=npm security gate failed::" in output.getvalue()


def test_child_output_is_bounded_and_truncation_is_visible() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 1,
            "stdout": "x" * (gate.MAX_CAPTURE_BYTES + 4096),
            "stderr": "npm error code E401 Unauthorized",
        }
    ]

    result, _calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert f"[npm stdout] [truncated at {gate.MAX_CAPTURE_BYTES} bytes]" in output
    assert len(output.encode("utf-8")) < gate.MAX_CAPTURE_BYTES + 2048


def test_truncated_zero_exit_report_fails_closed() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 0,
            "stdout": clean_plan()["vulnerabilities"][0]["stdout"]
            + "\n"
            + (" " * (gate.MAX_CAPTURE_BYTES + 4096)),
        }
    ]

    result, calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert [call["kind"] for call in calls].count("vulnerabilities") == 1
    assert "output exceeded the bounded capture limit" in output


def test_child_output_lines_are_prefixed_and_sensitive_url_parts_are_removed() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 1,
            "stderr": (
                "::warning title=untrusted::npm failed at "
                "https://alice:secret@registry.npmjs.org/pkg?token=hidden#fragment\n"
                "npm error code E401 Unauthorized"
            ),
        }
    ]

    result, _calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert "[npm stderr] ::warning title=untrusted::npm failed at https://registry.npmjs.org/pkg" in output
    assert "alice" not in output
    assert "secret" not in output
    assert "token=hidden" not in output
    assert not any(line.startswith("::warning title=untrusted::") for line in output.splitlines())


def test_legacy_actions_commands_are_escaped_even_when_embedded_in_prefixed_output() -> None:
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"] = [
        {
            "exit_code": 1,
            "stderr": (
                "diagnostic ##[add-mask]fixture-value\n"
                "diagnostic ##[stop-commands]fixture-token\n"
                "npm error code E401 Unauthorized"
            ),
        }
    ]

    result, _calls, output = invoke_gate(gate, plan)

    assert result == 1
    assert "##[" not in output
    assert "diagnostic # #[add-mask]fixture-value" in output
    assert "diagnostic # #[stop-commands]fixture-token" in output


def test_hostile_inherited_npm_and_node_config_cannot_bypass_the_gate() -> None:
    gate = load_module()
    high_report = {
        "exit_code": 1,
        "stdout": json.dumps(
            {
                "auditReportVersion": 2,
                "vulnerabilities": {"bad-package": {"severity": "high"}},
                "metadata": {"vulnerabilities": {"high": 1, "critical": 0, "total": 1}},
            }
        ),
    }
    plan = clean_plan()
    plan["vulnerabilities"] = [high_report]
    plan["config_bypass"] = clean_plan()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        hostile_user_config = tmp_path / "hostile-user.npmrc"
        hostile_user_config.write_text(
            "offline=true\nregistry=https://attacker.invalid/?token=secret\n",
            encoding="utf-8",
        )
        extra_ca = tmp_path / "company-ca.pem"
        extra_ca.write_text("test fixture", encoding="utf-8")
        ssl_cert_file = tmp_path / "system-ca.pem"
        ssl_cert_file.write_text("test fixture", encoding="utf-8")

        result, calls, output = invoke_gate(
            gate,
            plan,
            bind_checkout=True,
            environ_updates={
                "FAKE_NPM_SIMULATE_CONFIG_BYPASS": "1",
                "nPm_CoNfIg_OfFlInE": "true",
                "NpM_Config_UserConfig": str(hostile_user_config),
                "NODE_ENV": "production",
                "node_options": "--require=./hostile.js",
                "NoDe_PaTh": str(tmp_path / "hostile-modules"),
                "HTTPS_PROXY": "http://proxy.invalid:8080",
                "NO_PROXY": "localhost,127.0.0.1",
                "NODE_EXTRA_CA_CERTS": str(extra_ca),
                "SSL_CERT_FILE": str(ssl_cert_file),
            },
        )

    assert result == 1
    assert "high-severity vulnerability policy violation" in output
    assert calls
    required_flags = {
        "--global=false",
        "--package-lock=true",
        "--offline=false",
        "--prefer-offline=false",
        "--prefer-online=true",
        "--strict-ssl=true",
        "--registry=https://registry.npmjs.org/",
        "--include=prod",
        "--include=dev",
        "--include=optional",
        "--include=peer",
    }
    for call in calls:
        assert call["unsafe_env_keys"] == []
        assert required_flags.issubset(call["args"])
        assert f"--prefix={call['cwd']}" in call["args"]
        user_config_args = [arg for arg in call["args"] if arg.startswith("--userconfig=")]
        global_config_args = [arg for arg in call["args"] if arg.startswith("--globalconfig=")]
        assert len(user_config_args) == 1
        assert len(global_config_args) == 1
        assert "hostile-user.npmrc" not in user_config_args[0]
        assert call["preserved_env"] == {
            "HTTPS_PROXY": "http://proxy.invalid:8080",
            "NO_PROXY": "localhost,127.0.0.1",
            "NODE_EXTRA_CA_CERTS": str(extra_ca),
            "SSL_CERT_FILE": str(ssl_cert_file),
        }


def test_project_npmrc_is_rejected_before_npm_can_run() -> None:
    gate = load_module()
    with tempfile.TemporaryDirectory() as tmp:
        checkout_root = Path(tmp) / "checkout"
        checkout_root.mkdir()
        (checkout_root / ".npmrc").write_text("offline=true\n", encoding="utf-8")
        output = io.StringIO()

        result = gate.run_gate(
            command_prefix=["degen-dogs-must-not-run"],
            attempts=1,
            timeout_seconds=1,
            retry_delay_seconds=0,
            checkout_root=checkout_root,
            output=output,
        )

    assert result == 1
    assert "project .npmrc is forbidden" in output.getvalue()
    assert "unable to start npm" not in output.getvalue()


def test_descendant_holding_output_pipes_cannot_block_reader_joins() -> None:
    if os.name != "posix":
        return
    gate = load_module()
    plan = clean_plan()
    plan["vulnerabilities"][0]["hold_pipe_seconds"] = 5

    started = time.monotonic()
    result, calls, _output = invoke_gate(gate, plan)
    elapsed = time.monotonic() - started

    assert result == 0
    assert [call["kind"] for call in calls] == ["vulnerabilities", "signatures"]
    assert elapsed < 4


def test_ci_runs_bounded_remote_gate_immediately_after_install_before_repo_code() -> None:
    workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")
    npm_install = "npm ci --ignore-scripts --no-audit --no-fund"
    pip_install = "python -m pip install --require-hashes --only-binary=:all: -r requirements.txt"
    security_step = "python3 scripts/run_npm_security_checks.py --attempts 2 --timeout-seconds 30 --retry-delay-seconds 3"
    npm_install_position = workflow.index(npm_install)
    pip_install_position = workflow.index(pip_install)
    security_position = workflow.index(security_step)
    assert npm_install_position < pip_install_position < security_position
    for functional_step in (
        "python3 -m py_compile",
        "npm run test:dashboard",
        "npm run validate:dashboard",
        "npm run check:dashboard-ui",
        "npm run build",
    ):
        assert security_position < workflow.index(functional_step), functional_step
    intervening = workflow[pip_install_position + len(pip_install) : security_position]
    assert intervening.count("- name:") == 1
    assert "- run:" not in intervening
    security_block = workflow[workflow.rfind("- name:", 0, security_position) : security_position]
    assert "timeout-minutes: 3" in security_block


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"npm_security_tests=pass count={len(tests)}")
