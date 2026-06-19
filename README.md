# Autonomous Security Operations Center (ASOC)

An event-driven remediation platform that turns labeled GitHub issues into autonomous engineering sessions. When a security or bug issue is flagged, the ASOC orchestrator dispatches a Devin agent to investigate, fix, validate, and open a Pull Request — then tracks the outcome on a live dashboard.

## Overview

ASOC is a FastAPI service that sits between your GitHub repositories and the Devin API. It listens for issue events, launches autonomous remediation sessions, posts status back to the originating issue, and persists every job so metrics survive restarts.

- Event-driven: triggered by GitHub issue labels or direct API calls.
- Autonomous: each job spins up a Devin session with a structured remediation prompt.
- Observable: a built-in dashboard surfaces active sessions, resolved issues, and MTTR.
- Durable: job history is persisted to local storage and reloaded on startup.

## Architecture

```
GitHub Webhook  ->  FastAPI Orchestrator  ->  Devin API
   (issue           (validates, enqueues,       (autonomous
    labeled)         persists, comments)         remediation)
```

1. A GitHub issue is labeled `trigger-devin`, firing a webhook to the orchestrator.
2. The FastAPI orchestrator validates the event, records a task, and enqueues a background job.
3. The background job creates a Devin session with a structured prompt and updates the task status.
4. On session start, the orchestrator comments back on the issue to confirm remediation has begun.

## Endpoints

| Method | Path | Triggers |
| ------ | ---- | -------- |
| `POST` | `/remediate` | Manually enqueues a remediation job from a JSON payload and starts a Devin session in the background. |
| `POST` | `/webhooks/github` | Receives GitHub issue events; when an issue is labeled `trigger-devin`, enqueues a remediation job automatically. |
| `GET` | `/metrics` | Returns summary counts (queued, running, completed, failed), MTTR, and the full task store. |
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

### `POST /webhooks/github`

Consumes GitHub `issues` webhook events. When the action is `labeled` and the label is `trigger-devin`, the orchestrator builds a remediation request from the issue payload and enqueues it automatically.

## Observability

A live dashboard is served at http://localhost:8000 and refreshes every 10 seconds. It queries the `/metrics` endpoint and renders:

- Summary cards: total jobs, queued, running, completed, failed, and mean time to resolution.
- Active Sessions: tasks that are queued or running, with their issue, repository, session id, and start time.
- Resolved Issues: completed or failed tasks with their final status and resolution time.

### Local persistence

The orchestrator stores its task history in a `tasks.json` file in the project root. The store is written after every state change and reloaded automatically on startup, so dashboard metrics persist across server restarts. This file is local runtime state and is excluded from version control.

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

### Start the server

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Then open http://localhost:8000 to view the dashboard.

## Deployment with Docker

Build and start the containerized application:

```bash
docker compose up --build
```

This builds the image from `python:3.11-slim`, installs dependencies from `requirements.txt`, and starts the orchestrator on port 8000.

Environment variables are loaded from a `.env` file in the project root (see [Configure environment variables](#configure-environment-variables) above). The `tasks.json` file is mounted as a volume so task history persists across container restarts.

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
