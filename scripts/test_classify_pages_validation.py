#!/usr/bin/env python3
"""Regression tests for fail-closed GitHub Pages validation selection."""
from __future__ import annotations

import ast
import base64
import contextlib
import io
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

MODULE_PATH = Path(__file__).with_name("classify_pages_validation.py")
REPOSITORY = "ael-dev3/Degen-Dogs-Mission-3"
PAGES_RUN_ID = 24680


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("classify_pages_validation", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load scripts/classify_pages_validation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def initialize_repo(repo: Path) -> str:
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "config", "user.name", "Pages classifier test")
    git(repo, "config", "user.email", "pages-classifier-test@example.invalid")
    write(repo, "README.md", "baseline\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "--quiet", "-m", "baseline")
    return git(repo, "rev-parse", "HEAD")


def commit_staged_runner_refresh(
    repo: Path,
    *,
    include_trailers: bool = True,
    run_scope: str = "current",
) -> str:
    args = ["commit", "--quiet", "-m", "[cron] refresh Mission 3 data"]
    if include_trailers:
        args.extend(
            (
                "-m",
                "\n".join(
                    (
                        "Refresh-Runner-ID: windows-wsl",
                        f"Refresh-Run-Scope: {run_scope}",
                        "Refresh-Run-ID: refresh-windows-wsl-20260904T010203Z-42",
                    )
                ),
            )
        )
    git(repo, *args)
    return git(repo, "rev-parse", "HEAD")


def commit_runner_refresh(
    repo: Path,
    relative: str,
    *,
    include_trailers: bool = True,
    index_mode: str = "100644",
    run_scope: str = "current",
) -> str:
    write(repo, relative, "{}\n")
    git(repo, "add", relative)
    if index_mode != "100644":
        blob = git(repo, "hash-object", "-w", relative)
        git(repo, "update-index", "--add", "--cacheinfo", f"{index_mode},{blob},{relative}")
    return commit_staged_runner_refresh(
        repo,
        include_trailers=include_trailers,
        run_scope=run_scope,
    )


def commit_parent_artifact(repo: Path, relative: str) -> str:
    write(repo, relative, "{}\n")
    git(repo, "add", relative)
    git(repo, "commit", "--quiet", "-m", "published artifact baseline")
    return git(repo, "rev-parse", "HEAD")


class PagesRunsServer(ThreadingHTTPServer):
    expected_parent: str
    response_kind: str
    requests: list[str]


class PagesRunsHandler(BaseHTTPRequestHandler):
    server: PagesRunsServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.server.requests.append(self.path)
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        runs_path = (
            "/repos/ael-dev3/Degen-Dogs-Mission-3/actions/workflows/"
            "deploy-pages.yml/runs"
        )
        jobs_path = (
            "/repos/ael-dev3/Degen-Dogs-Mission-3/actions/runs/"
            f"{PAGES_RUN_ID}/jobs"
        )
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_response(401)
            self.end_headers()
            return

        if parsed.path == runs_path:
            valid_query = (
                query.get("head_sha") == [self.server.expected_parent]
                and query.get("status") == ["completed"]
                and query.get("per_page") == ["100"]
            )
            if not valid_query or self.server.response_kind == "error":
                self.send_response(500 if self.server.response_kind == "error" else 400)
                self.end_headers()
                return
            if self.server.response_kind == "redirect":
                self.send_response(302)
                self.send_header("Location", "/unexpected-redirect-target")
                self.end_headers()
                return
            runs = []
            if self.server.response_kind != "empty":
                run_id: object = 0 if self.server.response_kind == "invalid_run_id" else PAGES_RUN_ID
                host, port = self.server.server_address
                runs.append(
                    {
                        "id": run_id,
                        "name": "Deploy GitHub Pages",
                        "head_branch": "main",
                        "head_sha": self.server.expected_parent,
                        "path": ".github/workflows/deploy-pages.yml",
                        "run_number": 42,
                        "event": "push",
                        "status": "completed",
                        "conclusion": "success",
                        "workflow_id": 1234,
                        "jobs_url": f"http://{host}:{port}/unexpected-jobs-url",
                        "run_attempt": 1,
                    }
                )
            self._json(200, {"total_count": len(runs), "workflow_runs": runs})
            return

        if parsed.path == jobs_path:
            if query != {"filter": ["latest"], "per_page": ["100"]}:
                self.send_response(400)
                self.end_headers()
                return
            if self.server.response_kind == "jobs_redirect":
                self.send_response(302)
                self.send_header("Location", "/unexpected-redirect-target")
                self.end_headers()
                return
            if self.server.response_kind == "malformed_jobs":
                self._json(200, {"total_count": 1, "jobs": "not-a-list"})
                return
            deployment_conclusion = "skipped" if self.server.response_kind == "skipped" else "success"
            deploy_name = "publish" if self.server.response_kind == "missing_job" else "deploy"
            deploy_run_id = PAGES_RUN_ID + 1 if self.server.response_kind == "wrong_run_id" else PAGES_RUN_ID
            deploy_head = "c" * 40 if self.server.response_kind == "wrong_head" else self.server.expected_parent
            jobs = [
                {
                    "id": 13579,
                    "run_id": PAGES_RUN_ID,
                    "head_sha": self.server.expected_parent,
                    "status": "completed",
                    "conclusion": "success",
                    "name": "build",
                    "steps": [
                        {
                            "name": "Build",
                            "status": "completed",
                            "conclusion": "success",
                            "number": 1,
                        }
                    ],
                },
                {
                    "id": 13580,
                    "run_id": deploy_run_id,
                    "head_sha": deploy_head,
                    "status": "completed",
                    "conclusion": "success",
                    "name": deploy_name,
                    "steps": [
                        {
                            "name": "Checkout deployment controller",
                            "status": "completed",
                            "conclusion": "success",
                            "number": 1,
                        },
                        {
                            "name": "Verify artifact is still current main",
                            "status": "completed",
                            "conclusion": "success",
                            "number": 2,
                        },
                        {
                            "name": "Deploy to GitHub Pages",
                            "status": "completed",
                            "conclusion": deployment_conclusion,
                            "number": 3,
                        },
                    ],
                },
            ]
            if self.server.response_kind == "duplicate_job":
                duplicate = dict(jobs[-1])
                duplicate["id"] = 13581
                jobs.append(duplicate)
            if self.server.response_kind == "malformed_job_entry":
                jobs.append("not-a-job")
            if self.server.response_kind == "malformed_step_entry":
                jobs[-1]["steps"].append("not-a-step")
            jobs_total = len(jobs) + 1 if self.server.response_kind == "truncated_jobs" else len(jobs)
            self._json(200, {"total_count": jobs_total, "jobs": jobs})
            return

        self.send_response(404)
        self.end_headers()

    def _json(self, status: int, document: object) -> None:
        payload = json.dumps(document).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@contextlib.contextmanager
def pages_runs_api(parent: str, response_kind: str) -> Iterator[PagesRunsServer]:
    server = PagesRunsServer(("127.0.0.1", 0), PagesRunsHandler)
    server.expected_parent = parent
    server.response_kind = response_kind
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def classify(
    module: Any,
    repo: Path,
    server: PagesRunsServer,
    *,
    event_name: str = "push",
    ref: str = "refs/heads/main",
    forced: str = "false",
    before: str | None = None,
    after: str | None = None,
    dashboard_reproducible: bool = True,
) -> tuple[str, str]:
    module.dashboard_is_reproducible = lambda _repo, _head: dashboard_reproducible
    host, port = server.server_address
    return module.classify(
        repo,
        repository=REPOSITORY,
        token="test-token",
        api_url=f"http://{host}:{port}",
        event_name=event_name,
        ref=ref,
        forced=forced,
        before=before or server.expected_parent,
        after=after or git(repo, "rev-parse", "HEAD"),
    )


def classify_cli(
    module: Any,
    repo: Path,
    server: PagesRunsServer,
    *,
    forced: str | None,
) -> tuple[str, str]:
    module.dashboard_is_reproducible = lambda _repo, _head: True
    host, port = server.server_address
    environment = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_TOKEN": "test-token",
        "GITHUB_API_URL": f"http://{host}:{port}",
        "DEGEN_DOGS_PAGES_EVENT_NAME": "push",
        "DEGEN_DOGS_PAGES_EVENT_REF": "refs/heads/main",
        "DEGEN_DOGS_PAGES_EVENT_BEFORE": server.expected_parent,
        "DEGEN_DOGS_PAGES_EVENT_AFTER": git(repo, "rev-parse", "HEAD"),
    }
    if forced is not None:
        environment["DEGEN_DOGS_PAGES_EVENT_FORCED"] = forced
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert module.main([str(repo)], environment) == 0
    mode, reason = output.getvalue().strip().split("\t", 1)
    return mode, reason


class FakeDashboardBuilder:
    OUTPUT_TABLES = ["sample"]
    ROOT = Path(".")
    render_count = 0
    nondeterministic = False

    @staticmethod
    def atomic_write_text(path: Path, payload: str) -> None:
        path.write_text(payload, encoding="utf-8", newline="\n")

    @classmethod
    def write_html(cls, tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]]) -> None:
        cls.render_count += 1
        assert tables == {"sample": (["label", "count"], [("current", 7)])}
        avatar = base64.b64encode((cls.ROOT / "public" / "mark-profile.png").read_bytes()).decode()
        suffix = str(cls.render_count) if cls.nondeterministic else "stable"
        rendered = (
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'\">"
            "<style>body{color:black}</style>"
            "<script>safe()</script>"
            f"<main>current:7:{avatar}:{suffix}</main>\n"
        )
        cls.atomic_write_text(cls.ROOT / "index.html", rendered)


def initialize_dashboard_repo(repo: Path) -> tuple[str, str]:
    parent = initialize_repo(repo)
    write(repo, "generated/sample.csv", "label,count\ncurrent,7\n")
    write(repo, "generated/sample.json", '[{"label":"current","count":7}]\n')
    avatar = repo / "public" / "mark-profile.png"
    avatar.parent.mkdir(parents=True, exist_ok=True)
    avatar.write_bytes(b"fixture-png")
    FakeDashboardBuilder.ROOT = repo
    FakeDashboardBuilder.render_count = 0
    FakeDashboardBuilder.nondeterministic = False
    FakeDashboardBuilder.write_html({"sample": (["label", "count"], [("current", 7)])})
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "dashboard baseline")
    return parent, git(repo, "rev-parse", "HEAD")


def test_runner_artifact_commit_is_fast_only_after_parent_pages_success() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        initialize_repo(repo)
        write(repo, "generated/live_snapshot_old.json", '{"snapshot":"old"}\n')
        git(repo, "add", "generated/live_snapshot_old.json")
        git(repo, "commit", "--quiet", "-m", "published artifact baseline")
        parent = git(repo, "rev-parse", "HEAD")
        git(repo, "rm", "--quiet", "generated/live_snapshot_old.json")
        write(repo, "generated/current_auction.json", "{}\n")
        write(repo, "README.md", "updated dashboard metrics\n")
        git(repo, "add", "generated/current_auction.json", "README.md")
        commit_staged_runner_refresh(repo)
        with pages_runs_api(parent, "success") as server:
            assert classify(module, repo, server) == ("fast", "verified runner artifact commit")
            assert len(server.requests) == 2
        with pages_runs_api(parent, "success") as server:
            assert classify(module, repo, server, dashboard_reproducible=False) == (
                "full",
                "committed dashboard is not a deterministic renderer output",
            )
            assert server.requests == []
        with pages_runs_api(parent, "empty") as server:
            mode, reason = classify(module, repo, server)
            assert mode == "full"
            assert reason == "parent Pages deployment is not verified successful"


def test_parent_baseline_requires_one_exact_successful_pages_deploy_step() -> None:
    module = load_module()
    for response_kind in (
        "skipped",
        "missing_job",
        "wrong_run_id",
        "wrong_head",
        "duplicate_job",
    ):
        with pages_runs_api("a" * 40, response_kind) as server:
            assert module.parent_has_successful_pages_run(
                repository=REPOSITORY,
                parent="a" * 40,
                token="test-token",
                api_url=f"http://{server.server_address[0]}:{server.server_address[1]}",
            ) is False
            assert server.requests[0].endswith("status=completed&per_page=100")
            assert server.requests[1].endswith(f"actions/runs/{PAGES_RUN_ID}/jobs?filter=latest&per_page=100")

    for response_kind in (
        "malformed_jobs",
        "malformed_job_entry",
        "malformed_step_entry",
        "truncated_jobs",
        "jobs_redirect",
        "invalid_run_id",
    ):
        with pages_runs_api("a" * 40, response_kind) as server:
            try:
                module.parent_has_successful_pages_run(
                    repository=REPOSITORY,
                    parent="a" * 40,
                    token="test-token",
                    api_url=f"http://{server.server_address[0]}:{server.server_address[1]}",
                )
            except Exception:
                pass
            else:
                raise AssertionError(f"accepted uncertain parent proof: {response_kind}")
            assert all("unexpected-jobs-url" not in request for request in server.requests)


def test_source_change_forces_full_before_parent_api_lookup() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        parent = initialize_repo(repo)
        commit_runner_refresh(repo, "scripts/unsafe_source_change.py")
        with pages_runs_api(parent, "success") as server:
            mode, reason = classify(module, repo, server)
            assert mode == "full"
            assert reason == "commit changes paths outside the runner artifact allowlist"
            assert server.requests == []


def test_missing_runner_attribution_forces_full() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        parent = initialize_repo(repo)
        commit_runner_refresh(repo, "generated/current_auction.json", include_trailers=False)
        with pages_runs_api(parent, "success") as server:
            mode, reason = classify(module, repo, server)
            assert mode == "full"
            assert reason == "runner commit attribution is missing or invalid"
            assert server.requests == []


def test_only_one_exact_main_push_is_eligible() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        parent = initialize_repo(repo)
        head = commit_runner_refresh(repo, "generated/current_auction.json")
        rejected_events = (
            {"event_name": "workflow_dispatch"},
            {"ref": "refs/heads/feature"},
            {"before": "0" * 40},
            {"after": "0" * 40},
            {"after": parent},
        )
        for overrides in rejected_events:
            with pages_runs_api(parent, "success") as server:
                mode, _reason = classify(module, repo, server, **overrides)
                assert mode == "full"
                assert server.requests == []

        write(repo, "generated/current_latest_bid.json", '{"new":true}\n')
        git(repo, "add", "generated/current_latest_bid.json")
        second = commit_staged_runner_refresh(repo)
        with pages_runs_api(parent, "success") as server:
            mode, _reason = classify(
                module,
                repo,
                server,
                before=parent,
                after=second,
            )
            assert mode == "full"
            assert server.requests == []
        assert head != second


def test_forced_or_ambiguous_pushes_never_use_fast_validation() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        parent = initialize_repo(repo)
        commit_runner_refresh(repo, "generated/current_auction.json")
        for forced in ("true", "", "False", None):
            with pages_runs_api(parent, "success") as server:
                assert classify_cli(module, repo, server, forced=forced) == (
                    "full",
                    "push event forced identity is not exactly false",
                )
                assert server.requests == []

        with pages_runs_api(parent, "success") as server:
            assert classify_cli(module, repo, server, forced="false") == (
                "fast",
                "verified runner artifact commit",
            )

    workflow = (MODULE_PATH.parents[1] / ".github" / "workflows" / "deploy-pages.yml").read_text(
        encoding="utf-8"
    )
    assert "DEGEN_DOGS_PAGES_EVENT_FORCED: ${{ github.event.forced }}" in workflow


def test_only_exact_terminal_current_scope_trailers_are_eligible() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)

        body_repo = root / "body"
        body_parent = initialize_repo(body_repo)
        write(body_repo, "generated/current_auction.json", "{}\n")
        git(body_repo, "add", ".")
        git(
            body_repo,
            "commit",
            "--quiet",
            "-m",
            "[cron] refresh Mission 3 data",
            "-m",
            "Refresh-Runner-ID: windows-wsl\n"
            "Refresh-Run-Scope: current\n"
            "Refresh-Run-ID: refresh-windows-wsl-20260904T010203Z-42\n\n"
            "not a trailer",
        )
        with pages_runs_api(body_parent, "success") as server:
            mode, reason = classify(module, body_repo, server)
            assert mode == "full"
            assert reason == "runner commit attribution is missing or invalid"
            assert server.requests == []

        archive_repo = root / "archive"
        archive_parent = initialize_repo(archive_repo)
        commit_runner_refresh(
            archive_repo,
            "generated/current_auction.json",
            run_scope="archive",
        )
        with pages_runs_api(archive_parent, "success") as server:
            mode, reason = classify(module, archive_repo, server)
            assert mode == "full"
            assert reason == "runner commit attribution is missing or invalid"
            assert server.requests == []

        duplicate_repo = root / "duplicate"
        duplicate_parent = initialize_repo(duplicate_repo)
        write(duplicate_repo, "generated/current_auction.json", "{}\n")
        git(duplicate_repo, "add", ".")
        git(
            duplicate_repo,
            "commit",
            "--quiet",
            "-m",
            "[cron] refresh Mission 3 data",
            "-m",
            "Refresh-Runner-ID: windows-wsl\n"
            "Refresh-Runner-ID: peer\n"
            "Refresh-Run-Scope: current\n"
            "Refresh-Run-ID: refresh-windows-wsl-20260904T010203Z-42",
        )
        with pages_runs_api(duplicate_parent, "success") as server:
            mode, reason = classify(module, duplicate_repo, server)
            assert mode == "full"
            assert reason == "runner commit attribution is missing or invalid"
            assert server.requests == []


def test_parent_api_failure_and_uncertain_history_force_full() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        parent = initialize_repo(repo)
        commit_runner_refresh(repo, "generated/current_auction.json")
        with pages_runs_api(parent, "error") as server:
            mode, reason = classify(module, repo, server)
            assert mode == "full"
            assert reason == "parent Pages deployment lookup failed"

        with pages_runs_api(parent, "redirect") as server:
            mode, reason = classify(module, repo, server)
            assert mode == "full"
            assert reason == "parent Pages deployment lookup failed"
            assert len(server.requests) == 1

        shallow_repo = Path(raw) / "shallow-repo"
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", repo.as_uri(), str(shallow_repo)],
            check=True,
        )
        with pages_runs_api(parent, "success") as server:
            mode, reason = classify(module, shallow_repo, server)
            assert mode == "full"
            assert reason == "HEAD is not a single-parent commit with available history"
            assert server.requests == []

        root_repo = Path(raw) / "root-repo"
        root_repo.mkdir()
        git(root_repo, "init", "--quiet")
        git(root_repo, "config", "user.name", "Pages classifier test")
        git(root_repo, "config", "user.email", "pages-classifier-test@example.invalid")
        write(root_repo, "generated/current_auction.json", "{}\n")
        git(root_repo, "add", ".")
        git(
            root_repo,
            "commit",
            "--quiet",
            "-m",
            "[cron] refresh Mission 3 data",
            "-m",
            "Refresh-Runner-ID: windows-wsl",
            "-m",
            "Refresh-Run-Scope: current",
            "-m",
            "Refresh-Run-ID: refresh-windows-wsl-20260904T010203Z-42",
        )
        with pages_runs_api(parent, "success") as server:
            mode, reason = classify(module, root_repo, server)
            assert mode == "full"
            assert reason == "HEAD is not a single-parent commit with available history"
            assert server.requests == []


def test_modes_types_renames_and_copies_force_full() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)

        executable_repo = root / "executable"
        executable_parent = initialize_repo(executable_repo)
        commit_runner_refresh(
            executable_repo,
            "generated/current_auction.json",
            index_mode="100755",
        )
        with pages_runs_api(executable_parent, "success") as server:
            mode, reason = classify(module, executable_repo, server)
            assert mode == "full"
            assert reason == "commit includes a non-content-only artifact change"
            assert server.requests == []

        symlink_repo = root / "symlink"
        symlink_parent = initialize_repo(symlink_repo)
        commit_runner_refresh(
            symlink_repo,
            "generated/current_auction.json",
            index_mode="120000",
        )
        with pages_runs_api(symlink_parent, "success") as server:
            mode, reason = classify(module, symlink_repo, server)
            assert mode == "full"
            assert reason == "commit includes a non-content-only artifact change"
            assert server.requests == []

        typechange_repo = root / "typechange"
        initialize_repo(typechange_repo)
        typechange_parent = commit_parent_artifact(typechange_repo, "generated/current_auction.json")
        write(typechange_repo, "generated/current_auction.json", "target.json\n")
        blob = git(typechange_repo, "hash-object", "-w", "generated/current_auction.json")
        git(
            typechange_repo,
            "update-index",
            "--cacheinfo",
            f"120000,{blob},generated/current_auction.json",
        )
        commit_staged_runner_refresh(typechange_repo)
        with pages_runs_api(typechange_parent, "success") as server:
            mode, reason = classify(module, typechange_repo, server)
            assert mode == "full"
            assert reason == "commit includes a non-content-only artifact change"
            assert server.requests == []

        rename_repo = root / "rename"
        initialize_repo(rename_repo)
        rename_parent = commit_parent_artifact(rename_repo, "generated/current_auction.json")
        git(
            rename_repo,
            "mv",
            "generated/current_auction.json",
            "generated/current_latest_bid.json",
        )
        commit_staged_runner_refresh(rename_repo)
        with pages_runs_api(rename_parent, "success") as server:
            mode, reason = classify(module, rename_repo, server)
            assert mode == "full"
            assert reason == "commit includes a non-content-only artifact change"
            assert server.requests == []

        copy_repo = root / "copy"
        initialize_repo(copy_repo)
        copy_parent = commit_parent_artifact(copy_repo, "generated/current_auction.json")
        write(copy_repo, "generated/current_latest_bid.json", "{}\n")
        git(copy_repo, "add", "generated/current_latest_bid.json")
        commit_staged_runner_refresh(copy_repo)
        with pages_runs_api(copy_parent, "success") as server:
            mode, reason = classify(module, copy_repo, server)
            assert mode == "full"
            assert reason == "commit includes a non-content-only artifact change"
            assert server.requests == []


def test_dashboard_reconstruction_rejects_every_index_tamper_and_schema_error() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        tamper_replacements = (
            ("<script>safe()</script>", "<script>evil()</script>"),
            ("<style>body{color:black}</style>", "<style>body{color:red}</style>"),
            ("default-src 'none'", "default-src *"),
            ("<main>current:7", "<main>tampered:7"),
        )
        for index, (before_text, after_text) in enumerate(tamper_replacements):
            repo = root / f"tamper-{index}"
            _parent, head = initialize_dashboard_repo(repo)
            committed = (repo / "index.html").read_text(encoding="utf-8")
            write(repo, "index.html", committed.replace(before_text, after_text, 1))
            git(repo, "add", "index.html")
            git(repo, "commit", "--quiet", "-m", "tamper index")
            tampered_head = git(repo, "rev-parse", "HEAD")
            assert tampered_head != head
            FakeDashboardBuilder.render_count = 0
            assert module.dashboard_is_reproducible(
                repo,
                tampered_head,
                dashboard_module=FakeDashboardBuilder,
            ) is False

        malformed_repo = root / "malformed"
        _parent, _head = initialize_dashboard_repo(malformed_repo)
        write(malformed_repo, "generated/sample.json", '[{"wrong":"schema"}]\n')
        git(malformed_repo, "add", "generated/sample.json")
        git(malformed_repo, "commit", "--quiet", "-m", "malformed schema")
        FakeDashboardBuilder.render_count = 0
        assert module.dashboard_is_reproducible(
            malformed_repo,
            git(malformed_repo, "rev-parse", "HEAD"),
            dashboard_module=FakeDashboardBuilder,
        ) is False

        missing_image_repo = root / "missing-image"
        _parent, _head = initialize_dashboard_repo(missing_image_repo)
        git(missing_image_repo, "rm", "--quiet", "public/mark-profile.png")
        git(missing_image_repo, "commit", "--quiet", "-m", "remove required image")
        FakeDashboardBuilder.render_count = 0
        assert module.dashboard_is_reproducible(
            missing_image_repo,
            git(missing_image_repo, "rev-parse", "HEAD"),
            dashboard_module=FakeDashboardBuilder,
        ) is False


def test_dashboard_reconstruction_is_two_run_deterministic_and_matches_real_head() -> None:
    module = load_module()
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw) / "repo"
        _parent, head = initialize_dashboard_repo(repo)
        FakeDashboardBuilder.render_count = 0
        FakeDashboardBuilder.nondeterministic = False
        assert module.dashboard_is_reproducible(
            repo,
            head,
            dashboard_module=FakeDashboardBuilder,
        ) is True
        assert FakeDashboardBuilder.render_count == 2

        FakeDashboardBuilder.render_count = 0
        FakeDashboardBuilder.nondeterministic = True
        assert module.dashboard_is_reproducible(
            repo,
            head,
            dashboard_module=FakeDashboardBuilder,
        ) is False
        assert FakeDashboardBuilder.render_count == 2

    project_root = MODULE_PATH.parents[1]
    real_dashboard = module.load_dashboard_module(project_root)
    with tempfile.TemporaryDirectory() as raw:
        committed_repo = Path(raw) / "committed-dashboard"
        committed_repo.mkdir()
        git(committed_repo, "init", "--quiet")
        git(committed_repo, "config", "user.name", "Pages classifier test")
        git(committed_repo, "config", "user.email", "pages-classifier-test@example.invalid")
        paths = [
            "README.md",
            "index.html",
            "public/mark-profile.png",
            "scripts/build_dashboard.py",
        ]
        for table in real_dashboard.OUTPUT_TABLES:
            paths.extend((f"generated/{table}.csv", f"generated/{table}.json"))
        for relative in paths:
            destination = committed_repo / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith(".png"):
                shutil.copyfile(project_root / relative, destination)
            else:
                destination.write_bytes((project_root / relative).read_bytes().replace(b"\r\n", b"\n"))
        git(committed_repo, "add", ".")
        git(committed_repo, "commit", "--quiet", "-m", "real dashboard fixture")
        project_head = git(committed_repo, "rev-parse", "HEAD")
        assert module.dashboard_is_reproducible(
            committed_repo,
            project_head,
            dashboard_module=real_dashboard,
        ) is True
        write(committed_repo, "README.md", "eligible current refresh fixture\n")
        git(committed_repo, "add", "README.md")
        current_head = commit_staged_runner_refresh(committed_repo)
        with pages_runs_api(project_head, "success") as server:
            host, port = server.server_address
            assert module.classify(
                committed_repo,
                repository=REPOSITORY,
                token="test-token",
                api_url=f"http://{host}:{port}",
                event_name="push",
                ref="refs/heads/main",
                forced="false",
                before=project_head,
                after=current_head,
            ) == ("fast", "verified runner artifact commit")
            assert len(server.requests) == 2
        assert module.dashboard_is_reproducible(
            committed_repo,
            project_head,
            dashboard_module=real_dashboard,
        ) is True


def test_classifier_publish_allowlist_matches_every_publisher_policy_block() -> None:
    module = load_module()
    source = (MODULE_PATH.parent / "refresh_and_publish.sh").read_text(encoding="utf-8")
    blocks = re.findall(
        r"allowed_exact = \{.*?\}\nallowed_patterns = \(.*?\)\n",
        source,
        flags=re.DOTALL,
    )
    assert len(blocks) == 2
    for block in blocks:
        tree = ast.parse(block)
        exact: set[str] | None = None
        patterns: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "allowed_exact"
                for target in node.targets
            ):
                exact = ast.literal_eval(node.value)
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "allowed_patterns"
                for target in node.targets
            ):
                assert isinstance(node.value, ast.Tuple)
                for item in node.value.elts:
                    assert isinstance(item, ast.Call) and item.args
                    patterns.append(ast.literal_eval(item.args[0]))
        assert exact == module.ALLOWED_EXACT
        assert tuple(patterns) == tuple(pattern.pattern for pattern in module.ALLOWED_PATTERNS)

    staged_policy = re.search(
        r"staged = .*?\.splitlines\(\)\n(?P<policy>allowed_exact = .*?)\nunexpected = \[",
        source,
        flags=re.DOTALL,
    )
    assert staged_policy is not None
    tree = ast.parse(staged_policy.group("policy"))
    staged_exact: set[str] | None = None
    staged_patterns: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id == "allowed_exact":
            staged_exact = ast.literal_eval(node.value)
        elif target.id.startswith("allowed_"):
            assert isinstance(node.value, ast.Call) and node.value.args
            staged_patterns.append(ast.literal_eval(node.value.args[0]))
    assert staged_exact == module.ALLOWED_EXACT
    assert tuple(staged_patterns) == tuple(pattern.pattern for pattern in module.ALLOWED_PATTERNS)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"pages_validation_classifier_tests=pass count={len(tests)}")
