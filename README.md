# Autonomous Security Operations Center (ASOC)

An event-driven remediation platform that turns labeled GitHub issues into autonomous engineering sessions. When a security or bug issue is flagged, the ASOC orchestrator dispatches a Devin agent to investigate, fix, validate, and open a Pull Request — then tracks the outcome on a live dashboard.

## Overview

ASOC is a FastAPI service that sits between your GitHub repositories and the Devin API. It listens for issue events, launches autonomous remediation sessions, posts status back to the originating issue, and persists every job so metrics survive restarts.

- Event-driven: triggered by GitHub issue labels or direct API calls.
- Autonomous: each job spins up a Devin session with a structured remediation prompt.
- Closed-loop: the orchestrator tracks each session to completion, then reports the outcome back on the issue.
- Observable: a built-in dashboard surfaces active sessions, resolved issues, MTTR, categorized failure reasons, business-value KPIs, and trend charts, with filtering, drill-down links, and in-place retry/cancel controls.
- Actionable: failed remediations can be re-enqueued and in-flight ones cancelled directly from the dashboard or API, and new work can be triggered from a built-in form.
- Durable: job history is persisted to a local SQLite database and reloaded on startup, in-flight sessions are resumed automatically after a restart, and a pre-existing `tasks.json` is imported once so no history is lost.
- Resilient: transient Devin and GitHub failures are retried with exponential backoff, and a configurable cap bounds the number of concurrent in-flight sessions.
- Operable: every task state transition is emitted as structured JSON logging, and a health endpoint exposes database readiness.
- Secure by configuration: GitHub webhooks can be cryptographically verified, and the trigger and metrics endpoints can require bearer-token authentication.

## Why ASOC

Security and bug backlogs grow faster than teams can triage them, and the slow path between "issue filed" and "fix merged" is where risk accumulates. ASOC closes that gap:

- Shrinks mean time to resolution by dispatching a fix the moment an issue is labeled, with no human in the critical path.
- Keeps humans informed, not blocked: every issue receives a comment when remediation starts and a final comment with the Pull Request link or a categorized failure reason when it ends.
- Treats every issue as an isolated, parallel-safe unit of work, so many remediations can run concurrently without interfering with one another.
- Enforces a mandatory verification phase (tests, linters, build) before any Pull Request is opened, so autonomy does not come at the cost of reliability.
- Surfaces actionable failure categories (code bug, test failure, configuration) so a manager can triage at a glance instead of reading raw logs.

## Architecture

```
GitHub Webhook  ->  FastAPI Orchestrator  ->  Devin API
   (issue           (validates, enqueues,       (autonomous
    labeled)         persists, comments)         remediation)
```

1. A GitHub issue is labeled `trigger-devin`, firing a webhook to the orchestrator.
2. The FastAPI orchestrator validates the event, records a task, and enqueues a background job.
3. The background job creates a Devin session with a structured prompt and updates the task status to `running`.
4. On session start, the orchestrator comments back on the issue to confirm remediation has begun.
5. The orchestrator polls the Devin session until it stops. When the session opens a Pull Request and pauses for review, the task moves to the `PR` status and a background poller watches the Pull Request on GitHub.
6. When the Pull Request is merged, the task is marked `completed` and the linked GitHub issue is closed automatically; if the Pull Request is closed without merging, the task is marked `failed`.
7. On completion or failure, the orchestrator posts a final comment on the issue with the Pull Request link or the failure reason.

### Task lifecycle

Each task is persisted with `issue_id`, `status`, `session_id`, `pr_url`, `failure_category`, `failure_reason`, `created_at`, `updated_at`, and `error`. The status flows through `queued -> running -> PR -> completed`, where `PR` is an active state entered once the session opens a Pull Request and held until that Pull Request is merged. A task that never produces a Pull Request, or whose Pull Request is closed unmerged, transitions to `failed`. A `queued`, `running`, or `PR` task can also be moved to the terminal `cancelled` state via `POST /tasks/{task_id}/cancel`; cancellation is treated as resolved/terminal and rendered with a distinct badge. The agent's prompt instructs it to include a `Closes #<issue>` keyword so the originating issue is linked to the Pull Request and resolved on merge.

### Autonomous workflow contract

Every Devin session is dispatched with a prompt that enforces a closed-loop pattern:

- Isolation: the agent works on a dedicated branch as an independent unit, so remediations are parallel-safe.
- Verification Phase: the agent must run the full test suite plus all linters, formatters, type checkers, and the build, and may only open a Pull Request when they pass.
- Actionable reporting: the agent emits structured output (`result`, `failure_category`, `failure_reason`, `pull_request_url`) that the orchestrator records and renders on the dashboard.

## Endpoints

| Method | Path | Triggers |
| ------ | ---- | -------- |
| `POST` | `/remediate` | Manually enqueues a remediation job from a JSON payload and starts a Devin session in the background. |
| `POST` | `/tasks/{task_id}/retry` | Re-enqueues a `failed` task as a brand-new remediation, reusing the original issue details and respecting the concurrency cap and in-flight de-duplication. |
| `POST` | `/tasks/{task_id}/cancel` | Marks a `queued`, `running`, or `PR` task as `cancelled` (a terminal state) and stops its polling loop. |
| `DELETE` | `/tasks/{task_id}` | Permanently removes a task's record so it disappears from every table, chart, KPI, leaderboard, and the CSV export. |
| `POST` | `/webhooks/github` | Receives GitHub issue events; when an issue is labeled `trigger-devin`, enqueues a remediation job automatically. |
| `GET` | `/metrics` | Returns server-side summary aggregates (queued, running, pr, completed, failed, cancelled, MTTR/MTTF) and a filterable, paginated page of tasks. |
| `GET` | `/export/tasks.csv` | Streams the matching tasks (same filters as `/metrics`) as a downloadable CSV file. |
| `GET` | `/healthz` | Liveness/readiness probe; returns `{"status": "ok"}` after a lightweight database connectivity check. |
| `GET` | `/` | Serves the observability dashboard. |

### `POST /remediate`

Accepts a JSON body describing the issue to remediate:

```json
{
  "issue_id": "57",
  "title": "Patch outdated dependency CVE-2024-1234",
  "description": "Bump the vulnerable package to a patched release.",
  "repository": "your-org/your-repo",
  "branch": "main"
}
```

Returns an accepted status with the generated `task_id`. The remediation runs in the background.

Returns `{"status": "duplicate", "task_id": <existing>}` instead of starting a second session when an in-flight job (`queued` or `running`) already exists for the same `repository` and `issue_id`.

When `ASOC_API_TOKEN` is set, the request must include an `Authorization: Bearer <ASOC_API_TOKEN>` header or it is rejected with `401`.

### `POST /tasks/{task_id}/retry`

Re-runs a remediation for a task whose `status` is `failed`. The orchestrator reconstructs the original remediation request (issue, title, description, repository, branch) and pushes it back through the standard enqueue path, so the concurrency cap and in-flight de-duplication both apply. It returns `{"status": "accepted", "task_id": <new>, "source_task_id": <original>}`, or `{"status": "duplicate", ...}` when an in-flight job already exists for that repository and issue. Retrying a task that is not `failed` returns `409`, and an unknown `task_id` returns `404`. Protected by `ASOC_API_TOKEN` when set.

### `POST /tasks/{task_id}/cancel`

Marks a `queued` or `running` task as `cancelled`, a terminal state, and signals its polling loop to stop so it neither completes nor fails afterwards. It returns `{"status": "cancelled", "task_id": <id>}`. Cancelling a task that is already terminal returns `409`, and an unknown `task_id` returns `404`. Protected by `ASOC_API_TOKEN` when set.

### `DELETE /tasks/{task_id}`

Permanently deletes a task's record from the store. Because every dashboard table, summary card, chart, ROI KPI, repository leaderboard, and the CSV export are all derived from `/metrics` (which reads the store), a removed task is no longer displayed or counted anywhere. If the task is still `queued` or `running`, its polling loop is signaled to stop first so it cannot be re-persisted. It returns `{"status": "removed", "task_id": <id>}`, and an unknown `task_id` returns `404`. Protected by `ASOC_API_TOKEN` when set.

### `POST /webhooks/github`

Consumes GitHub `issues` webhook events. When the action is `labeled` and the label is `trigger-devin`, the orchestrator builds a remediation request from the issue payload and enqueues it automatically. Like `/remediate`, it de-duplicates in-flight jobs for the same `repository` and `issue_id`.

When `GITHUB_WEBHOOK_SECRET` is set, every request is verified against its `X-Hub-Signature-256` header before processing (see [Security](#security)).

## Observability

A live dashboard is served at http://localhost:8000 and refreshes every 10 seconds. It queries the `/metrics` endpoint and renders an Operations tab and an Analytics tab.

The Operations tab shows:

- Summary cards: total jobs, queued, running, completed, failed, cancelled, and mean time to resolution.
- Active Sessions: a sortable table of queued or running tasks, with their issue, repository, session id, start time, and a per-row Cancel action.
- Resolved Issues: a sortable table of completed, failed, or cancelled tasks, showing the Pull Request link for successes, a UI-friendly failure category and reason for failures, and per-row Retry (failures) and Remove actions.

Both tables are sortable by clicking any column header.

### Filtering and search

A filter bar above both tabs drives the tables and every chart simultaneously. It exposes a repository dropdown (derived from the loaded data), a status dropdown (including `cancelled`), a failure-category dropdown, a free-text search box (matching issue, title, repository, session id, and failure reason), and a `From`/`To` date range. The repository and date-range filters are pushed to `/metrics` as server-side query parameters; the remaining filters are applied client-side. A `Clear filters` button resets everything. An `Export CSV` button downloads the current server-filtered view (repository, status, and date range) through `GET /export/tasks.csv`.

### Drill-down links and actions

In both tables the `session_id` links to its Devin session (`https://app.devin.ai/sessions/<id>`) and the issue links to its GitHub issue (`https://github.com/<repository>/issues/<issue_id>`). Per-row `Retry` (failed tasks), `Cancel` (queued/running tasks), and `Remove` (any task) buttons call `POST /tasks/{task_id}/retry`, `POST /tasks/{task_id}/cancel`, and `DELETE /tasks/{task_id}` respectively, then refresh; when `ASOC_API_TOKEN` is configured the dashboard sends it as a bearer token automatically. `Remove` asks for confirmation, then permanently deletes the record so it is excluded from every table, chart, and KPI.

### Manual trigger form

A `Trigger Remediation` button opens a theme-matched modal with repository, issue id, title, description, and branch fields that `POST`s to `/remediate` (sending the bearer token when configured) and reports success, duplicate, or error inline.

### Analytics

The Analytics tab adds:

- KPI cards: issues resolved, failed to resolve, success rate, and average resolution time.
- Repository Leaderboard: a sortable per-repository table of total handled, resolved, failed, success rate, and average resolution time, computed client-side from the loaded metrics and matching the existing table styling.
- Business-value KPIs: estimated engineer-hours saved (resolved count times a configurable average hours-per-fix input, default `4`), auto-resolved issue count, and auto-handled rate (resolved divided by total triggered). The math is shown inline.
- Remediation Throughput: a grouped-bar chart of resolved, failed, and cancelled issues over time, with a Day/Week/Month/Year granularity toggle.
- Cumulative Issues Resolved: a running-total line chart.
- Failure Breakdown: a donut chart of failure categories (`code_bug`, `test_failure`, `configuration`, `session_error`).
- Quality Trends: a dual-axis line chart of success-rate and MTTR over time, using the same granularity toggle.

### Querying metrics

`GET /metrics` is filterable and paginated. Every parameter is optional; with none supplied it returns the most recent page of tasks and aggregates computed over the entire store, matching the original behavior.

| Parameter | Default | Description |
| --------- | ------- | ----------- |
| `status` | – | Only include tasks with this status (`queued`, `running`, `completed`, `failed`, `cancelled`). |
| `repository` | – | Only include tasks for this `owner/name` repository. |
| `from` | – | Unix timestamp; only include tasks created at or after this time. |
| `to` | – | Unix timestamp; only include tasks created at or before this time. |
| `page` | `1` | 1-based page number. |
| `page_size` | `50` | Tasks per page, capped at `200`. |

The response stays backward compatible, adding pagination metadata alongside the existing `summary` and `tasks` keys:

```json
{
  "summary": {
    "total_triggered_jobs": 12,
    "queued": 1,
    "running": 2,
    "completed": 7,
    "failed": 2,
    "cancelled": 0,
    "mean_time_to_resolution_seconds": 134.5,
    "mean_time_to_failure_seconds": 88.0
  },
  "tasks": { "task_...": { "...": "..." } },
  "page": 1,
  "page_size": 50,
  "total": 12
}
```

The `summary` aggregates are always computed server-side over the full filtered set, not just the returned page. `mean_time_to_resolution_seconds` now covers completed tasks only, and `mean_time_to_failure_seconds` covers failed tasks only.

### Exporting tasks as CSV

`GET /export/tasks.csv` streams the matching tasks as a downloadable CSV with the columns `task_id`, `issue_id`, `repository`, `status`, `failure_category`, `failure_reason`, `pr_url`, `created_at`, `updated_at`, and `duration`. Timestamps are emitted as UTC ISO-8601 strings and `duration` is the resolution time in seconds. It accepts the same `status`, `repository`, `from`, and `to` filters as `/metrics`, so the dashboard `Export CSV` button downloads exactly the current server-filtered view. The response is rendered incrementally with a `text/csv` media type and a `Content-Disposition: attachment; filename="tasks.csv"` header. When `ASOC_DASHBOARD_TOKEN` is set, the endpoint requires the same bearer token as `/metrics`.

### Failure handling

When remediation does not succeed, the task is recorded with `status` `failed`, a `failure_category`, and a human-readable `failure_reason`. The orchestrator then posts a final comment on the issue in the form `Autonomous remediation failed in ASOC pipeline. Failure category: <category>. Reason: <reason>`, and the dashboard renders the category as a badge alongside the reason.

Failures originate in two places:

- Orchestrator-side, when the session never starts or never reaches a terminal state. These are always categorized as `configuration` (for example a timed-out request, a connection error, a non-`201` response from the Devin API, or a session that does not finish within the polling window).
- Devin-reported, when the session runs and ends in failure. The category is taken from the session's structured output and normalized against the supported set, falling back to `session_error`.

| Category | Meaning |
| -------- | ------- |
| `code_bug` | The agent could not produce a working fix for the issue. |
| `test_failure` | The fix did not pass the project's test suite. |
| `configuration` | An environment, credentials, or setup problem, including all orchestrator-side API errors. |
| `session_error` | Fallback when a session ends in a non-success state (such as `expired` or `blocked`) without reporting a supported category. |

A task that an operator stops via `POST /tasks/{task_id}/cancel` is recorded with `status` `cancelled` rather than `failed`; it is a deliberate terminal state, carries no failure category, and is rendered with its own badge.

### Local persistence

The orchestrator stores its task history in a local SQLite database (`asoc.db`) in the project root, using Python's standard-library `sqlite3` with no extra infrastructure. Writes are serialized so concurrent background polling cannot corrupt the store. The database is reloaded automatically on startup, so dashboard metrics persist across server restarts. On first startup, a pre-existing `tasks.json` is imported once and then renamed to `tasks.json.imported`. The database file is local runtime state and is excluded from version control.

## Front-end dependencies

The dashboard is a single static template with no Node or build pipeline. Its two third-party libraries are pinned to reduce supply-chain and offline risk:

- Chart.js is vendored locally. The pinned UMD build (`static/chart.umd.min.js`) is committed to the repository and served by FastAPI through `StaticFiles` at `/static/chart.umd.min.js`, so the dashboard does not depend on a CDN at runtime.
- Tailwind is pinned to an exact version (`@tailwindcss/browser@4.3.0`) loaded over a CORS-enabled CDN with a Subresource Integrity (`integrity` + `crossorigin`) attribute, so the browser refuses to execute a script whose contents do not match the pinned hash.

## Testing and continuous integration

The test suite uses `pytest` with FastAPI's `TestClient` and mocks every external Devin and GitHub call, so it never touches the network. It covers webhook signature verification (valid, invalid, and absent), endpoint authentication (open when unset, required when set), idempotency/de-duplication, the SQLite store load/save/update round-trip, `/metrics` filtering with summary and MTTR/MTTF math, the retry and cancel endpoints, the remediation lifecycle, and the CSV export.

Install the development dependencies and run the suite from the project root:

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

A GitHub Actions workflow at `.github/workflows/ci.yml` runs the linter (`ruff check .`) and the test suite (`pytest -q`) on every push and pull request.

## How to Run

### Prerequisites

- Python 3.10+

### Install dependencies

```bash
pip install fastapi uvicorn requests python-dotenv
```

### Configure environment variables

Create a `.env` file in the project root:

```bash
DEVIN_API_KEY=your_devin_api_key
GITHUB_TOKEN=your_github_token
```

| Variable | Purpose |
| -------- | ------- |
| `DEVIN_API_KEY` | Authenticates requests to the Devin API when creating remediation sessions. |
| `GITHUB_TOKEN` | Authorizes the orchestrator to post status comments back on GitHub issues. |
| `GITHUB_WEBHOOK_SECRET` | Optional. Shared secret used to verify the `X-Hub-Signature-256` header on `POST /webhooks/github`. When set, unsigned or invalid requests are rejected with `401`. When unset, verification is skipped and a warning is logged (preserving current behavior). |
| `ASOC_API_TOKEN` | Optional. When set, `POST /remediate` requires `Authorization: Bearer <ASOC_API_TOKEN>`. When unset, the endpoint is open and a startup warning is logged. |
| `ASOC_DASHBOARD_TOKEN` | Optional. When set, `GET /metrics` requires `Authorization: Bearer <ASOC_DASHBOARD_TOKEN>`, and the dashboard page is rendered with the token injected so its polling requests stay authenticated. When unset, `/metrics` is open and the dashboard behaves exactly as before. |
| `ASOC_MAX_CONCURRENT_SESSIONS` | Optional. Maximum number of Devin sessions allowed to run at once. Additional triggers are accepted, held as `queued`, and started as capacity frees up. Defaults to `3`. |
| `ASOC_MAX_ACU_PER_SESSION` | Optional. Maximum ACU (AI Compute Units) budget for each Devin session. When set to a positive integer, the value is passed as `max_acu_limit` in the session creation request. When unset, the key is omitted and Devin applies its default limit. |
| `LOG_LEVEL` | Optional. Logging verbosity, one of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Defaults to `INFO`. |

The authentication and webhook variables default to unset, which preserves the original unauthenticated behavior.

## Security

### GitHub webhook signature verification

Set `GITHUB_WEBHOOK_SECRET` to the same value configured as the secret on the GitHub webhook. The orchestrator reads the raw request body, computes `HMAC-SHA256` over it with the secret, and compares the result against the `X-Hub-Signature-256` header using a constant-time comparison. A missing or invalid signature is rejected with `401` and no job is enqueued. When the variable is unset, verification is skipped and a warning is logged so existing deployments keep working.

### Endpoint authentication

`POST /remediate` and `GET /metrics` each support an optional bearer token, configured independently via `ASOC_API_TOKEN` and `ASOC_DASHBOARD_TOKEN`. When a token is set, requests must send `Authorization: Bearer <token>`; otherwise they are rejected with `401`. When the dashboard token is set, the dashboard served at `/` is rendered with the token injected so its automatic `GET /metrics` polling remains authenticated without any manual step. Both endpoints stay open when their token is unset, and the absence of a token is logged as a warning at startup.

### Restart recovery

Task history is reloaded on startup. Any task left in a non-terminal state (`queued` or `running`) is reconciled:

- If it has a `session_id`, the orchestrator resumes polling that Devin session in the background until it reaches a terminal state, so a restart never strands an in-flight remediation.
- If it has no `session_id` (it was recorded but the Devin session never started), it is marked `failed` with the `configuration` category and a clear reason, since there is no session to resume.

Recovery runs in background threads and does not block startup.

## Reliability and operations

### Retries and concurrency

Transient Devin API and GitHub failures — timeouts, connection errors, and HTTP `5xx` responses — are retried with bounded exponential backoff before a task is marked failed. Clear client errors (`401`, `403`, and other `4xx` responses) are never retried and remain immediate `configuration` failures.

The number of simultaneously in-flight Devin sessions is capped by `ASOC_MAX_CONCURRENT_SESSIONS` (default 3). Triggers beyond the cap are accepted immediately, held as `queued`, and started automatically as capacity frees up.

### Structured logging

Each task state transition is emitted as a single JSON log line — including task id, issue, repository, status, and failure category — via Python `logging`. The verbosity is configurable with `LOG_LEVEL` (default `INFO`).

### Health checks

`GET /healthz` returns `{"status": "ok"}` after a lightweight database connectivity check, suitable for container or orchestrator readiness probes. It responds with `503` when the database is unreachable.

### Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Then open http://localhost:8000 to view the dashboard.

### Simulating an issue

With the server running, trigger a remediation directly through the manual endpoint:

```bash
curl -X POST http://localhost:8000/remediate \
  -H "Content-Type: application/json" \
  -d '{
    "issue_id": "101",
    "title": "Fix SQL injection in login handler",
    "description": "User input is concatenated directly into the query.",
    "repository": "your-org/your-repo",
    "branch": "main"
  }'
```

To simulate the GitHub webhook path, send an `issues` event with the `trigger-devin` label:

```bash
curl -X POST http://localhost:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -d '{
    "action": "labeled",
    "label": { "name": "trigger-devin" },
    "issue": { "number": 101, "title": "Fix SQL injection in login handler", "body": "User input is concatenated directly into the query." },
    "repository": { "full_name": "your-org/your-repo" }
  }'
```

Either call returns an accepted response with a `task_id`, and the new job appears on the dashboard. Re-triggering the same `repository` and `issue_id` while a job is still in flight returns `{"status": "duplicate", "task_id": <existing>}` instead of starting a second session. Without a valid `DEVIN_API_KEY`, the task is recorded and then marked `failed` with a `configuration` reason, which is the expected behavior when credentials are absent.

When `ASOC_API_TOKEN` is set, pass it as a bearer token on `/remediate`:

```bash
curl -X POST http://localhost:8000/remediate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ASOC_API_TOKEN" \
  -d '{ "issue_id": "101", "title": "Fix SQL injection in login handler", "description": "User input is concatenated directly into the query.", "repository": "your-org/your-repo", "branch": "main" }'
```

When `GITHUB_WEBHOOK_SECRET` is set, sign the raw body and send the digest in `X-Hub-Signature-256`:

```bash
BODY='{"action":"labeled","label":{"name":"trigger-devin"},"issue":{"number":101,"title":"Fix SQL injection in login handler","body":"User input is concatenated directly into the query."},"repository":{"full_name":"your-org/your-repo"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | sed 's/^.* //')"
curl -X POST http://localhost:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
```

## Deployment with Docker

Build and start the containerized application:

```bash
docker compose up --build
```

This builds the image from `python:3.11-slim`, installs dependencies from `requirements.txt`, and starts the orchestrator on port 8000.

Environment variables are loaded from a `.env` file in the project root (see [Configure environment variables](#configure-environment-variables) above). The `asoc.db` SQLite database is mounted as a volume so task history persists across container restarts.

To run in detached mode:

```bash
docker compose up --build -d
```

To stop and remove the container:

```bash
docker compose down
```

## License

Proprietary. All rights reserved.
