#!/usr/bin/env python3
"""Behavioral tests for monotonic GitHub Pages deployment control."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(__file__).with_name("pages_deploy_controller.py")
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "deploy-pages.yml"
REPOSITORY = "ael-dev3/Degen-Dogs-Mission-3"
CANDIDATE = "a" * 40
CURRENT = "b" * 40


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("pages_deploy_controller", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load Pages deploy controller")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ControllerServer(ThreadingHTTPServer):
    current_sha: str
    active_statuses: set[str]
    redirect_ref: bool
    malformed_runs: bool
    dispatch_status: int
    runs_total_count: int | None
    requests: list[tuple[str, str, bytes]]


class ControllerHandler(BaseHTTPRequestHandler):
    server: ControllerServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        body = b""
        self.server.requests.append(("GET", self.path, body))
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_response(401)
            self.end_headers()
            return
        parsed = urlparse(self.path)
        if parsed.path == "/repos/ael-dev3/Degen-Dogs-Mission-3/git/ref/heads/main":
            if self.server.redirect_ref:
                self.send_response(302)
                self.send_header("Location", "/credential-capture")
                self.end_headers()
                return
            self._json(200, {"object": {"type": "commit", "sha": self.server.current_sha}})
            return
        if parsed.path == "/repos/ael-dev3/Degen-Dogs-Mission-3/actions/workflows/deploy-pages.yml/runs":
            query = parse_qs(parsed.query)
            status = query.get("status", [""])[0]
            head = query.get("head_sha", [""])[0]
            runs: list[dict[str, object]] = []
            if self.server.malformed_runs:
                runs.append({"head_sha": head})
            elif head == self.server.current_sha:
                matching = (
                    [status]
                    if status and status in self.server.active_statuses
                    else sorted(self.server.active_statuses) if not status else []
                )
                runs.extend(
                    {
                        "id": index + 100,
                        "head_sha": head,
                        "status": run_status,
                        "conclusion": "success" if run_status == "completed" else None,
                    }
                    for index, run_status in enumerate(matching)
                )
            total_count = (
                len(runs) if self.server.runs_total_count is None else self.server.runs_total_count
            )
            self._json(200, {"total_count": total_count, "workflow_runs": runs})
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(("POST", self.path, body))
        if (
            self.headers.get("Authorization") != "Bearer test-token"
            or self.headers.get("Content-Type") != "application/json"
            or self.path
            != "/repos/ael-dev3/Degen-Dogs-Mission-3/actions/workflows/deploy-pages.yml/dispatches"
            or json.loads(body) != {"ref": "main"}
        ):
            self.send_response(400)
            self.end_headers()
            return
        self.send_response(self.server.dispatch_status)
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
def controller_api(
    current_sha: str,
    *,
    active_statuses: set[str] | None = None,
    redirect_ref: bool = False,
    malformed_runs: bool = False,
    dispatch_status: int = 204,
    runs_total_count: int | None = None,
) -> Iterator[ControllerServer]:
    server = ControllerServer(("127.0.0.1", 0), ControllerHandler)
    server.current_sha = current_sha
    server.active_statuses = active_statuses or set()
    server.redirect_ref = redirect_ref
    server.malformed_runs = malformed_runs
    server.dispatch_status = dispatch_status
    server.runs_total_count = runs_total_count
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def client(module: Any, server: ControllerServer) -> Any:
    host, port = server.server_address
    return module.GitHubApi(
        repository=REPOSITORY,
        token="test-token",
        api_url=f"http://{host}:{port}",
    )


def test_preflight_allows_only_the_exact_current_artifact() -> None:
    module = load_module()
    with controller_api(CANDIDATE) as server:
        result = module.preflight(client(module, server), CANDIDATE)
        assert result == {"deploy": True, "current_sha": CANDIDATE, "dispatched": False}
        assert [request[0] for request in server.requests] == ["GET"]

    with controller_api(CURRENT, active_statuses={"queued"}) as server:
        result = module.preflight(client(module, server), CANDIDATE)
        assert result == {"deploy": False, "current_sha": CURRENT, "dispatched": False}
        assert [request[0] for request in server.requests] == ["GET", "GET"]
        assert parse_qs(urlparse(server.requests[1][1]).query) == {
            "head_sha": [CURRENT],
            "status": ["queued"],
            "per_page": ["100"],
        }


def test_stale_preflight_dispatches_main_only_without_an_active_exact_run() -> None:
    module = load_module()
    with controller_api(CURRENT) as server:
        result = module.preflight(client(module, server), CANDIDATE)
        assert result == {"deploy": False, "current_sha": CURRENT, "dispatched": True}
        assert [request[0] for request in server.requests] == ["GET"] * 6 + ["POST"]
        assert "head_sha=" + CURRENT in server.requests[1][1]
        assert [
            parse_qs(urlparse(request[1]).query)["status"][0]
            for request in server.requests[1:-1]
        ] == ["queued", "in_progress", "requested", "waiting", "pending"]


def test_every_nonterminal_workflow_state_deduplicates_dispatches() -> None:
    module = load_module()
    for status in ("requested", "queued", "pending", "waiting", "in_progress"):
        with controller_api(CURRENT, active_statuses={status}) as server:
            result = module.preflight(client(module, server), CANDIDATE)
            assert result == {"deploy": False, "current_sha": CURRENT, "dispatched": False}
            assert all(request[0] != "POST" for request in server.requests)
            queries = [parse_qs(urlparse(request[1]).query) for request in server.requests[1:]]
            assert all(query.get("head_sha") == [CURRENT] for query in queries)
            assert all(query.get("per_page") == ["100"] for query in queries)
            assert all(len(query.get("status", [])) == 1 for query in queries)
            assert any(query["status"] == [status] for query in queries)

    with controller_api(CURRENT, active_statuses={"completed"}) as server:
        result = module.preflight(client(module, server), CANDIDATE)
        assert result == {"deploy": False, "current_sha": CURRENT, "dispatched": True}


def test_paginated_active_status_still_prevents_a_duplicate_dispatch() -> None:
    module = load_module()
    with controller_api(
        CURRENT,
        active_statuses={"queued"},
        runs_total_count=101,
    ) as server:
        result = module.preflight(client(module, server), CANDIDATE)
        assert result == {"deploy": False, "current_sha": CURRENT, "dispatched": False}
        assert all(request[0] != "POST" for request in server.requests)


def test_postflight_recovers_only_when_main_advanced() -> None:
    module = load_module()
    with controller_api(CANDIDATE) as server:
        result = module.postflight(client(module, server), CANDIDATE)
        assert result == {"current_sha": CANDIDATE, "dispatched": False}
        assert [request[0] for request in server.requests] == ["GET"]

    with controller_api(CURRENT, active_statuses={"in_progress"}) as server:
        result = module.postflight(client(module, server), CANDIDATE)
        assert result == {"current_sha": CURRENT, "dispatched": False}
        assert [request[0] for request in server.requests] == ["GET", "GET", "GET"]

    with controller_api(CURRENT) as server:
        result = module.postflight(client(module, server), CANDIDATE)
        assert result == {"current_sha": CURRENT, "dispatched": True}
        assert server.requests[-1][0] == "POST"


def test_controller_rejects_redirects_and_invalid_candidate_identity() -> None:
    module = load_module()
    with controller_api(CURRENT, redirect_ref=True) as server:
        try:
            module.preflight(client(module, server), CANDIDATE)
        except Exception:
            pass
        else:
            raise AssertionError("controller followed a GitHub API redirect")
        assert len(server.requests) == 1

    with controller_api(CURRENT) as server:
        try:
            module.preflight(client(module, server), "not-a-sha")
        except ValueError:
            pass
        else:
            raise AssertionError("controller accepted an invalid candidate SHA")
        assert server.requests == []

    with controller_api(CURRENT, malformed_runs=True) as server:
        try:
            module.preflight(client(module, server), CANDIDATE)
        except ValueError:
            pass
        else:
            raise AssertionError("controller treated a malformed run lookup as no active run")
        assert all(request[0] != "POST" for request in server.requests)


def test_dispatch_requires_githubs_exact_no_content_status() -> None:
    module = load_module()
    with controller_api(CURRENT, dispatch_status=200) as server:
        try:
            module.preflight(client(module, server), CANDIDATE)
        except ValueError:
            pass
        else:
            raise AssertionError("controller accepted an empty non-204 dispatch response")
        assert [request[0] for request in server.requests] == ["GET"] * 6 + ["POST"]


def test_workflow_serializes_only_the_stale_aware_deploy_controller() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "\nconcurrency:\n" not in workflow
    assert workflow.count("\n    concurrency:\n") == 1
    assert workflow.count("      group: pages-deploy\n") == 1
    assert workflow.count("      cancel-in-progress: false\n") == 1
    assert (
        "  deploy:\n"
        "    needs: build\n"
        "    concurrency:\n"
        "      group: pages-deploy\n"
        "      cancel-in-progress: false\n"
    ) in workflow
    assert (
        "    permissions:\n"
        "      actions: write\n"
        "      contents: read\n"
        "      pages: write\n"
        "      id-token: write\n"
    ) in workflow
    assert "id: freshness" in workflow
    ordered_controller = (
        "      - name: Verify artifact is still current main\n"
        "        id: freshness\n"
        "        run: python3 scripts/pages_deploy_controller.py pre\n"
        "        env:\n"
        "          GITHUB_TOKEN: ${{ github.token }}\n\n"
        "      - name: Deploy to GitHub Pages\n"
        "        id: deployment\n"
        "        if: steps.freshness.outputs.deploy == 'true'\n"
        "        uses: actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128 # v5.0.0\n\n"
        "      - name: Recheck main after deployment\n"
        "        if: steps.freshness.outputs.deploy == 'true' && steps.deployment.outcome == 'success'\n"
        "        run: python3 scripts/pages_deploy_controller.py post\n"
    )
    assert ordered_controller in workflow
    assert workflow.count("      actions: write\n") == 1
    assert workflow.count("      pages: write\n") == 1
    assert workflow.count("      id-token: write\n") == 1


def test_cli_writes_a_fail_closed_preflight_output() -> None:
    module = load_module()
    with controller_api(CURRENT) as server, tempfile.TemporaryDirectory() as raw:
        output = Path(raw) / "github-output"
        host, port = server.server_address
        environment = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_TOKEN": "test-token",
            "GITHUB_API_URL": f"http://{host}:{port}",
            "GITHUB_SHA": CANDIDATE,
            "GITHUB_OUTPUT": str(output),
        }
        assert module.main(["pre"], environment) == 0
        assert output.read_text(encoding="utf-8") == f"deploy=false\ncurrent_sha={CURRENT}\n"


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"pages_deploy_controller_tests=pass count={len(tests)}")
