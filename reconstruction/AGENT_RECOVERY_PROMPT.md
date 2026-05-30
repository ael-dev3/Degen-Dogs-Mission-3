# Agent recovery prompt

Paste this into a future agent if the dashboard must be recovered from a fork or fresh
clone.

```text
You are recovering the Degen Dogs Mission 3 dashboard from a fork or fresh clone.

Do not redesign the dashboard.
Do not fabricate data.
Do not commit secrets.
Do not hand-edit generated files as the durable fix.
Do not push unless explicitly told.

First inspect README.md, README.template.md, package.json, scripts/build_dashboard.py,
scripts/build_unified_dog_index.py, sql/mission3_dashboard.sql, generated/,
public/generated/, archive/, reconstruction/, docs/, and .github/workflows/.

Your goals:
1. Confirm how the dashboard is generated.
2. Configure a safe local environment from .env.example if needed.
3. Run npm ci, npm run data, npm run build.
4. Verify generated outputs, latest block, row counts, and local dashboard rendering.
5. If the runner fails, diagnose RPC/chunking/build issues and rerun safely.
6. Preserve current dashboard behavior and source/generated separation.
7. Produce a summary of changed files and commands run.
8. Keep secrets, private keys, API keys, and local machine paths out of commits.

Use reconstruction/QUICKSTART.md, reconstruction/RUNBOOK.md, reconstruction/VALIDATION.md,
reconstruction/LOCAL_RUNNERS.md, and docs/configuration.md.
```
