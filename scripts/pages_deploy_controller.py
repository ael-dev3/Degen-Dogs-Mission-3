#!/usr/bin/env python3
"""Prevent stale Pages artifacts from deploying and recover the current main run."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
MAX_API_RESPONSE_BYTES = 1_000_000
WORKFLOW = "deploy-pages.yml"
ACTIVE_RUN_STATUSES = ("queued", "in_progress", "requested", "waiting", "pending")


class RejectRedirectHandler(HTTPRedirectHandler):
    """Never forward the controller token through an HTTP redirect."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class GitHubApi:
    def __init__(self, *, repository: str, token: str, api_url: str) -> None:
        owner, separator, name = repository.partition("/")
        if not separator or not owner or not name or "/" in name:
            raise ValueError("invalid GitHub repository identity")
        if not token or not api_url:
            raise ValueError("missing GitHub API context")
        self._repository_path = f"repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        self._token = token
        self._api_url = api_url.rstrip("/")

    def request(
        self,
        method: str,
        relative: str,
        document: object | None = None,
        *,
        expected_status: int = 200,
    ) -> object | None:
        payload = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "degen-dogs-pages-deploy-controller",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if document is not None:
            payload = json.dumps(document, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._api_url}/{self._repository_path}/{relative.lstrip('/')}",
            data=payload,
            headers=headers,
            method=method,
        )
        with build_opener(RejectRedirectHandler()).open(request, timeout=5) as response:
            if response.status != expected_status:
                raise ValueError("GitHub API returned an unexpected status")
            response_payload = response.read(MAX_API_RESPONSE_BYTES + 1)
        if len(response_payload) > MAX_API_RESPONSE_BYTES:
            raise ValueError("GitHub API response is too large")
        if not response_payload:
            return None
        return json.loads(response_payload)

    def current_main_sha(self) -> str:
        document = self.request("GET", "git/ref/heads/main")
        if not isinstance(document, dict):
            raise ValueError("main ref response is invalid")
        target = document.get("object")
        sha = target.get("sha") if isinstance(target, dict) and target.get("type") == "commit" else None
        if not isinstance(sha, str) or SHA_PATTERN.fullmatch(sha) is None or sha == "0" * 40:
            raise ValueError("main ref does not identify a commit")
        return sha

    def has_active_pages_run(self, sha: str) -> bool:
        require_sha(sha)
        workflow = quote(WORKFLOW, safe="")
        for status in ACTIVE_RUN_STATUSES:
            query = urlencode({"head_sha": sha, "status": status, "per_page": 100})
            document = self.request("GET", f"actions/workflows/{workflow}/runs?{query}")
            runs = document.get("workflow_runs") if isinstance(document, dict) else None
            total_count = document.get("total_count") if isinstance(document, dict) else None
            if (
                not isinstance(runs, list)
                or type(total_count) is not int
                or total_count < len(runs)
                or (total_count > 0 and not runs)
                or len(runs) > 100
            ):
                raise ValueError("workflow runs response is invalid or incomplete")
            if any(
                not isinstance(run, dict)
                or run.get("head_sha") != sha
                or run.get("status") != status
                for run in runs
            ):
                raise ValueError("workflow runs response is inconsistent with its query")
            if runs:
                return True
        return False

    def dispatch_main(self) -> None:
        workflow = quote(WORKFLOW, safe="")
        response = self.request(
            "POST",
            f"actions/workflows/{workflow}/dispatches",
            {"ref": "main"},
            expected_status=204,
        )
        if response is not None:
            raise ValueError("workflow dispatch returned an unexpected body")


def require_sha(value: str) -> str:
    if SHA_PATTERN.fullmatch(value) is None or value == "0" * 40:
        raise ValueError("candidate is not a canonical commit SHA")
    return value


def ensure_current_run(api: GitHubApi, current_sha: str) -> bool:
    if api.has_active_pages_run(current_sha):
        return False
    api.dispatch_main()
    return True


def preflight(api: GitHubApi, candidate_sha: str) -> dict[str, str | bool]:
    candidate = require_sha(candidate_sha)
    current = api.current_main_sha()
    deploy = current == candidate
    dispatched = False if deploy else ensure_current_run(api, current)
    return {"deploy": deploy, "current_sha": current, "dispatched": dispatched}


def postflight(api: GitHubApi, candidate_sha: str) -> dict[str, str | bool]:
    candidate = require_sha(candidate_sha)
    current = api.current_main_sha()
    dispatched = False if current == candidate else ensure_current_run(api, current)
    return {"current_sha": current, "dispatched": dispatched}


def append_preflight_outputs(path: Path, result: dict[str, str | bool]) -> None:
    deploy = "true" if result["deploy"] is True else "false"
    current = require_sha(str(result["current_sha"]))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"deploy={deploy}\ncurrent_sha={current}\n")


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    environment = os.environ if environ is None else environ
    if len(args) != 1 or args[0] not in {"pre", "post"}:
        print("usage: pages_deploy_controller.py {pre|post}", file=sys.stderr)
        return 64
    try:
        api = GitHubApi(
            repository=environment.get("GITHUB_REPOSITORY", ""),
            token=environment.get("GITHUB_TOKEN", ""),
            api_url=environment.get("GITHUB_API_URL", ""),
        )
        candidate = environment.get("GITHUB_SHA", "")
        if args[0] == "pre":
            result = preflight(api, candidate)
            output_path = environment.get("GITHUB_OUTPUT", "")
            if not output_path:
                raise ValueError("GitHub output path is missing")
            append_preflight_outputs(Path(output_path), result)
        else:
            result = postflight(api, candidate)
    except Exception as exc:
        print(f"pages deploy controller failed closed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(
        "pages_deploy_candidate="
        f"{candidate} current={result['current_sha']} "
        f"deploy={result.get('deploy', 'post-check')} recovery_dispatched={result['dispatched']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
