#!/usr/bin/env python3
"""Select a fail-closed Pages validation mode for the checked-out commit.

The fast lane is a transitive trust edge: one exact current-data push may use
it only when its sole parent already completed this Pages workflow. Source,
multi-commit, non-current, and uncertain pushes run the full suite, establish a
new deployed baseline on success, and can therefore safely parent the next
eligible current-data commit without racing parallel CI.
"""
from __future__ import annotations

import csv
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

FAST_SUBJECT = "[cron] refresh Mission 3 data"
MAX_API_RESPONSE_BYTES = 1_000_000
MAX_PARENT_RUN_CANDIDATES = 10
PAGES_WORKFLOW = "deploy-pages.yml"
PAGES_DEPLOY_JOB = "deploy"
PAGES_DEPLOY_STEP = "Deploy to GitHub Pages"
ALLOWED_EXACT = {
    "README.md",
    "index.html",
    "archive/data/identity/wallet_profiles.json",
    "archive/dogs/manifest.json",
}
ALLOWED_PATTERNS = (
    re.compile(r"^(generated|public/generated)/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^archive/mission3/data/generated/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^public/generated/mission3/[A-Za-z0-9_]+\.json$"),
    re.compile(r"^archive/data/generated/unified_dog_search_[A-Za-z0-9_]+\.json$"),
    re.compile(r"^archive/dogs/by-id/[0-9]+\.json$"),
    re.compile(r"^archive/prices/data/generated/[A-Za-z0-9_]+\.(csv|json)$"),
    re.compile(r"^archive/prices/data/raw/[A-Za-z0-9_-]+\.json$"),
)
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
RUNNER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
ALLOWED_TRANSITIONS = {
    "A": ("000000", "100644"),
    "M": ("100644", "100644"),
    "D": ("100644", "000000"),
}


class RejectRedirectHandler(HTTPRedirectHandler):
    """Keep the scoped Actions token on the configured API origin."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def git_output(repo: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    return result.stdout


def exact_trailer(
    message: str,
    prefix: str,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    values = [line[len(prefix) :] for line in message.splitlines() if line.startswith(prefix)]
    if len(values) != 1:
        return None
    value = values[0]
    if pattern is not None and pattern.fullmatch(value) is None:
        return None
    return value


def terminal_trailers(repo: Path, message: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "interpret-trailers", "--parse"],
        check=True,
        input=message,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def allowed_artifact_path(path: str) -> bool:
    return path in ALLOWED_EXACT or any(pattern.fullmatch(path) for pattern in ALLOWED_PATTERNS)


def git_blob(repo: Path, head: str, relative: str) -> bytes:
    payload = git_output(repo, "cat-file", "blob", f"{head}:{relative}", text=False)
    if not isinstance(payload, bytes):
        raise TypeError("git cat-file returned text instead of bytes")
    return payload


def load_dashboard_module(repo: Path) -> object:
    module_path = repo / "scripts" / "build_dashboard.py"
    module_name = "_degen_dogs_build_dashboard_for_pages_attestation"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError("dashboard renderer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def reconstruct_dashboard_tables(
    repo: Path,
    head: str,
    dashboard_module: object,
) -> dict[str, tuple[list[str], list[tuple[object, ...]]]]:
    raw_names = getattr(dashboard_module, "OUTPUT_TABLES", None)
    if not isinstance(raw_names, list) or not raw_names:
        raise ValueError("dashboard output table inventory is invalid")
    names = [str(name) for name in raw_names]
    if len(set(names)) != len(names) or any(re.fullmatch(r"[A-Za-z0-9_]+", name) is None for name in names):
        raise ValueError("dashboard output table inventory is ambiguous")

    tables: dict[str, tuple[list[str], list[tuple[object, ...]]]] = {}
    for name in names:
        csv_payload = git_blob(repo, head, f"generated/{name}.csv")
        json_payload = git_blob(repo, head, f"generated/{name}.json")
        csv_text = csv_payload.decode("utf-8")
        reader = csv.reader(io.StringIO(csv_text, newline=""))
        columns = next(reader)
        if not columns or len(set(columns)) != len(columns) or any(not column for column in columns):
            raise ValueError(f"dashboard table {name} has an invalid CSV header")
        def reject_json_constant(value: str) -> None:
            raise ValueError(f"dashboard table {name} contains invalid JSON number {value}")

        typed_rows = json.loads(json_payload.decode("utf-8"), parse_constant=reject_json_constant)
        if not isinstance(typed_rows, list):
            raise ValueError(f"dashboard table {name} JSON is not a row list")
        rows: list[tuple[object, ...]] = []
        for row in typed_rows:
            if not isinstance(row, dict) or list(row) != columns:
                raise ValueError(f"dashboard table {name} JSON schema differs from its CSV header")
            rows.append(tuple(row[column] for column in columns))
        tables[name] = (columns, rows)
    return tables


def render_dashboard_once(
    tables: dict[str, tuple[list[str], list[tuple[object, ...]]]],
    avatar: bytes,
    dashboard_module: object,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="degen-dogs-pages-render-") as raw:
        root = Path(raw)
        public = root / "public"
        public.mkdir(mode=0o700)
        (public / "mark-profile.png").write_bytes(avatar)
        original_root = getattr(dashboard_module, "ROOT")
        original_atomic_write = getattr(dashboard_module, "atomic_write_text")

        def write_utf8_lf(path: Path, payload: str) -> None:
            target = Path(path)
            if target != root / "index.html" or not isinstance(payload, str):
                raise ValueError("dashboard renderer attempted an unexpected write")
            target.write_bytes(payload.encode("utf-8"))

        try:
            setattr(dashboard_module, "ROOT", root)
            setattr(dashboard_module, "atomic_write_text", write_utf8_lf)
            getattr(dashboard_module, "write_html")(tables)
        finally:
            setattr(dashboard_module, "atomic_write_text", original_atomic_write)
            setattr(dashboard_module, "ROOT", original_root)
        return (root / "index.html").read_bytes()


def dashboard_is_reproducible(
    repo: Path,
    head: str,
    *,
    dashboard_module: object | None = None,
) -> bool:
    try:
        module = dashboard_module or load_dashboard_module(repo)
        tables = reconstruct_dashboard_tables(repo, head, module)
        avatar = git_blob(repo, head, "public/mark-profile.png")
        expected = git_blob(repo, head, "index.html")
        if not avatar or not expected.endswith(b"\n") or b"\r" in expected:
            return False
        first = render_dashboard_once(tables, avatar, module)
        second = render_dashboard_once(tables, avatar, module)
        return (
            first.endswith(b"\n")
            and second.endswith(b"\n")
            and b"\r" not in first
            and b"\r" not in second
            and first == second == expected
        )
    except Exception:
        return False


def artifact_diff(repo: Path, head: str) -> list[tuple[str, str, str, tuple[str, ...]]]:
    raw = git_output(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--raw",
        "-r",
        "-z",
        "-M100%",
        "-C100%",
        "--find-copies-harder",
        head,
        text=False,
    )
    if not isinstance(raw, bytes):
        raise TypeError("git diff-tree returned text instead of bytes")
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    records: list[tuple[str, str, str, tuple[str, ...]]] = []
    offset = 0
    while offset < len(fields):
        header = fields[offset]
        offset += 1
        parts = header.split()
        if len(parts) != 5 or not parts[0].startswith(b":"):
            raise ValueError("malformed git raw diff header")
        old_mode = parts[0][1:].decode("ascii")
        new_mode = parts[1].decode("ascii")
        status = parts[4].decode("ascii")
        path_count = 2 if status.startswith(("R", "C")) else 1
        if offset + path_count > len(fields):
            raise ValueError("malformed git raw diff paths")
        paths = tuple(os.fsdecode(path) for path in fields[offset : offset + path_count])
        offset += path_count
        records.append((status, old_mode, new_mode, paths))
    return records


def parent_has_successful_pages_run(
    *,
    repository: str,
    parent: str,
    token: str,
    api_url: str,
) -> bool:
    owner, separator, name = repository.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError("invalid GitHub repository identity")
    if not token or not api_url:
        raise ValueError("missing GitHub Actions API context")
    workflow = quote(PAGES_WORKFLOW, safe="")
    query = urlencode({"head_sha": parent, "status": "completed", "per_page": 100})
    runs_endpoint = (
        f"{api_url.rstrip('/')}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        f"/actions/workflows/{workflow}/runs?{query}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "degen-dogs-pages-validation",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def api_document(endpoint: str) -> object:
        request = Request(endpoint, headers=headers)
        with build_opener(RejectRedirectHandler()).open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError("GitHub Actions API returned an unexpected status")
            payload = response.read(MAX_API_RESPONSE_BYTES + 1)
        if len(payload) > MAX_API_RESPONSE_BYTES:
            raise ValueError("GitHub Actions API response is too large")
        return json.loads(payload)

    document = api_document(runs_endpoint)
    runs = document.get("workflow_runs") if isinstance(document, dict) else None
    total_count = document.get("total_count") if isinstance(document, dict) else None
    if (
        not isinstance(runs, list)
        or type(total_count) is not int
        or total_count < len(runs)
        or len(runs) > 100
    ):
        raise ValueError("GitHub Actions API response has no workflow_runs list")
    run_ids: list[int] = []
    for run in runs:
        if (
            not isinstance(run, dict)
            or run.get("head_sha") != parent
            or run.get("status") != "completed"
        ):
            raise ValueError("GitHub Actions run response is inconsistent with its query")
        if run.get("conclusion") != "success":
            continue
        run_id = run.get("id")
        if type(run_id) is not int or run_id <= 0:
            raise ValueError("GitHub Actions run has no canonical positive ID")
        run_ids.append(run_id)
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("GitHub Actions run response contains duplicate IDs")

    repository_endpoint = (
        f"{api_url.rstrip('/')}/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    )
    for run_id in run_ids[:MAX_PARENT_RUN_CANDIDATES]:
        jobs_query = urlencode({"filter": "latest", "per_page": 100})
        jobs_document = api_document(
            f"{repository_endpoint}/actions/runs/{run_id}/jobs?{jobs_query}"
        )
        jobs = jobs_document.get("jobs") if isinstance(jobs_document, dict) else None
        jobs_total = jobs_document.get("total_count") if isinstance(jobs_document, dict) else None
        if (
            not isinstance(jobs, list)
            or type(jobs_total) is not int
            or jobs_total != len(jobs)
            or len(jobs) > 100
            or any(not isinstance(job, dict) for job in jobs)
        ):
            raise ValueError("GitHub Actions jobs response is invalid")
        deploy_jobs = [job for job in jobs if job.get("name") == PAGES_DEPLOY_JOB]
        if len(deploy_jobs) != 1:
            continue
        deploy_job = deploy_jobs[0]
        if (
            deploy_job.get("run_id") != run_id
            or deploy_job.get("head_sha") != parent
            or deploy_job.get("status") != "completed"
            or deploy_job.get("conclusion") != "success"
        ):
            continue
        steps = deploy_job.get("steps")
        if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
            raise ValueError("GitHub Actions deploy steps response is invalid")
        deploy_steps = [
            step
            for step in steps
            if step.get("name") == PAGES_DEPLOY_STEP
        ]
        if len(deploy_steps) != 1:
            continue
        deploy_step = deploy_steps[0]
        if deploy_step.get("status") == "completed" and deploy_step.get("conclusion") == "success":
            return True
    return False


def classify(
    repo: Path,
    *,
    repository: str,
    token: str,
    api_url: str,
    event_name: str,
    ref: str,
    forced: str,
    before: str,
    after: str,
) -> tuple[str, str]:
    if event_name != "push" or ref != "refs/heads/main":
        return "full", "workflow event is not an exact main-branch push"
    if forced != "false":
        return "full", "push event forced identity is not exactly false"
    if (
        SHA_PATTERN.fullmatch(before) is None
        or SHA_PATTERN.fullmatch(after) is None
        or before == "0" * 40
        or after == "0" * 40
    ):
        return "full", "push event commit identity is invalid"
    try:
        history = str(git_output(repo, "rev-list", "--parents", "-n", "1", "HEAD")).split()
        if len(history) != 2:
            return "full", "HEAD is not a single-parent commit with available history"
        head, parent = history
        git_output(repo, "cat-file", "-e", f"{parent}^{{commit}}")
    except Exception:
        return "full", "HEAD is not a single-parent commit with available history"
    if after != head or before != parent:
        return "full", "push event does not identify HEAD and its sole parent"
    try:
        introduced = str(git_output(repo, "rev-list", "--count", f"{before}..{after}")).strip()
    except Exception:
        return "full", "push event commit range could not be verified"
    if introduced != "1":
        return "full", "push event introduces more than one commit"

    try:
        subject = str(git_output(repo, "show", "-s", "--format=%s", head)).rstrip("\n")
        message = str(git_output(repo, "show", "-s", "--format=%B", head))
        trailers = terminal_trailers(repo, message)
    except Exception:
        return "full", "runner commit attribution is missing or invalid"
    runner_id = exact_trailer(trailers, "Refresh-Runner-ID: ", RUNNER_ID_PATTERN)
    run_scope = exact_trailer(trailers, "Refresh-Run-Scope: ")
    run_id = exact_trailer(trailers, "Refresh-Run-ID: ", RUN_ID_PATTERN)
    if subject != FAST_SUBJECT or runner_id is None or run_id is None or run_scope != "current":
        return "full", "runner commit attribution is missing or invalid"

    try:
        records = artifact_diff(repo, head)
    except Exception:
        return "full", "commit artifact inventory could not be verified"
    if not records:
        return "full", "commit changes no runner artifacts"
    if any(ALLOWED_TRANSITIONS.get(status) != (old_mode, new_mode) for status, old_mode, new_mode, _ in records):
        return "full", "commit includes a non-content-only artifact change"
    changed = [path for _status, _old_mode, _new_mode, paths in records for path in paths]
    if any(not allowed_artifact_path(path) for path in changed):
        return "full", "commit changes paths outside the runner artifact allowlist"
    if not dashboard_is_reproducible(repo, head):
        return "full", "committed dashboard is not a deterministic renderer output"

    try:
        parent_verified = parent_has_successful_pages_run(
            repository=repository,
            parent=parent,
            token=token,
            api_url=api_url,
        )
    except Exception:
        return "full", "parent Pages deployment lookup failed"
    if not parent_verified:
        return "full", "parent Pages deployment is not verified successful"
    return "fast", "verified runner artifact commit"


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    environment = os.environ if environ is None else environ
    if len(args) != 1:
        print("full\tclassifier invocation is invalid")
        return 0
    try:
        mode, reason = classify(
            Path(args[0]).resolve(),
            repository=environment.get("GITHUB_REPOSITORY", ""),
            token=environment.get("GITHUB_TOKEN", ""),
            api_url=environment.get("GITHUB_API_URL", ""),
            event_name=environment.get("DEGEN_DOGS_PAGES_EVENT_NAME", ""),
            ref=environment.get("DEGEN_DOGS_PAGES_EVENT_REF", ""),
            forced=environment.get("DEGEN_DOGS_PAGES_EVENT_FORCED", ""),
            before=environment.get("DEGEN_DOGS_PAGES_EVENT_BEFORE", ""),
            after=environment.get("DEGEN_DOGS_PAGES_EVENT_AFTER", ""),
        )
    except Exception:
        mode, reason = "full", "classifier failed closed"
    print(f"{mode}\t{reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
