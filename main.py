import os
from dotenv import load_dotenv
load_dotenv()

import json
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, BackgroundTasks, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests

app = FastAPI(title="Devin Automation Orchestrator")

DEVIN_API_URL = "https://api.devin.ai/v1"
GITHUB_API_URL = "https://api.github.com"
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

BASE_DIR = Path(__file__).resolve().parent
TASKS_FILE = BASE_DIR / "tasks.json"
TEMPLATES_DIR = BASE_DIR / "templates"

SESSION_START_COMMENT = "Autonomous remediation initiated by ASOC pipeline."
DEVIN_REQUEST_TIMEOUT = 30


def load_tasks() -> Dict[str, dict]:
    if not TASKS_FILE.exists():
        return {}
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
        return {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_tasks(tasks: Dict[str, dict]) -> None:
    with TASKS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(tasks, handle, indent=2)


task_store: Dict[str, dict] = load_tasks()


class RemediationRequest(BaseModel):
    issue_id: str
    title: str
    description: str
    repository: str
    branch: str = "master"


class BatchRemediationRequest(BaseModel):
    requests: List[RemediationRequest]


def post_issue_comment(repository: str, issue_id: str, body: str) -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token or not repository or not issue_id:
        return

    url = f"{GITHUB_API_URL}/repos/{repository}/issues/{issue_id}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        requests.post(url, json={"body": body}, headers=headers, timeout=10)
    except requests.RequestException:
        return


def build_remediation_prompt(payload: RemediationRequest) -> str:
    return f"""
    You are an automated engineering agent resolving an open tracking issue end to end.

    Repository: {payload.repository}
    Branch: {payload.branch}
    Issue Title: {payload.title}
    Issue Description: {payload.description}

    Follow these steps precisely:
    1. Clone the repository and create a new branch off {payload.branch} named for this issue.
    2. Reproduce the reported bug or vulnerability before changing anything, and confirm your understanding of the root cause.
    3. Implement a complete, minimal fix without introducing any code comments.
    4. Run the full test suite. If tests are missing for the affected behavior, add them. Do not proceed until all tests pass locally.
    5. Run any available linters, type checkers, and the build, and resolve every failure they surface.
    6. Rebase onto the latest {payload.branch}. If you hit merge conflicts, resolve them carefully so existing behavior is preserved, then re-run the full test suite.
    7. Open a structured Pull Request back to {payload.branch} summarizing the root cause, the fix, and the validation you performed.

    If at any point you cannot validate the fix or the tests cannot be made to pass, stop and report the blocker instead of opening a Pull Request.
    """


def mark_task_failed(task_id: str, error: str) -> None:
    task_store[task_id].update({
        "status": "failed",
        "error": error,
        "updated_at": time.time(),
    })
    save_tasks(task_store)


def create_devin_session(payload: RemediationRequest) -> requests.Response:
    clean_key = os.getenv("DEVIN_API_KEY", "").strip()
    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
    }
    body = {"prompt": build_remediation_prompt(payload)}
    return requests.post(
        f"{DEVIN_API_URL}/sessions",
        json=body,
        headers=headers,
        timeout=DEVIN_REQUEST_TIMEOUT,
    )


def run_devin_remediation(task_id: str, payload: RemediationRequest):
    try:
        response = create_devin_session(payload)
    except requests.Timeout:
        mark_task_failed(task_id, "Devin API request timed out")
        return
    except requests.RequestException as e:
        mark_task_failed(task_id, str(e))
        return

    if response.status_code != 201:
        mark_task_failed(task_id, f"Devin API returned status {response.status_code}")
        return

    data = response.json()
    task_store[task_id].update({
        "status": "running",
        "session_id": data.get("session_id"),
        "updated_at": time.time(),
    })
    save_tasks(task_store)
    post_issue_comment(payload.repository, payload.issue_id, SESSION_START_COMMENT)


def enqueue_task(payload: RemediationRequest, background_tasks: BackgroundTasks) -> str:
    task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    task_store[task_id] = {
        "issue_id": payload.issue_id,
        "title": payload.title,
        "repository": payload.repository,
        "status": "queued",
        "session_id": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "error": None,
    }
    save_tasks(task_store)
    background_tasks.add_task(run_devin_remediation, task_id, payload)
    return task_id


@app.get("/")
async def dashboard():
    return FileResponse(TEMPLATES_DIR / "index.html")


@app.post("/remediate")
async def trigger_remediation(payload: RemediationRequest, background_tasks: BackgroundTasks):
    task_id = enqueue_task(payload, background_tasks)
    return {"status": "accepted", "task_id": task_id}


@app.post("/webhook/batch-remediate")
async def batch_remediate(payload: BatchRemediationRequest, background_tasks: BackgroundTasks):
    accepted = []
    errors = []
    for item in payload.requests:
        try:
            task_id = enqueue_task(item, background_tasks)
            accepted.append({"issue_id": item.issue_id, "task_id": task_id})
        except OSError as e:
            errors.append({"issue_id": item.issue_id, "error": str(e)})

    return {
        "status": "accepted" if accepted else "failed",
        "accepted_count": len(accepted),
        "error_count": len(errors),
        "tasks": accepted,
        "errors": errors,
    }


@app.post("/webhooks/github")
async def github_webhook(payload: dict, background_tasks: BackgroundTasks, x_github_event: Optional[str] = Header(None)):
    if x_github_event == "issues":
        action = payload.get("action")
        issue = payload.get("issue", {})
        repo = payload.get("repository", {})

        if action == "labeled":
            label_name = payload.get("label", {}).get("name", "")
            if label_name.lower() == "trigger-devin":
                request_payload = RemediationRequest(
                    issue_id=str(issue.get("number")),
                    title=issue.get("title"),
                    description=issue.get("body", ""),
                    repository=repo.get("full_name"),
                )
                task_id = enqueue_task(request_payload, background_tasks)
                return {"status": "webhook_processed", "task_id": task_id}

    return {"status": "ignored"}


@app.get("/metrics")
async def get_metrics():
    total_tasks = len(task_store)
    completed = sum(1 for t in task_store.values() if t["status"] == "completed")
    failed = sum(1 for t in task_store.values() if t["status"] == "failed")
    running = sum(1 for t in task_store.values() if t["status"] == "running")
    queued = sum(1 for t in task_store.values() if t["status"] == "queued")

    durations = [
        t["updated_at"] - t["created_at"]
        for t in task_store.values()
        if t["status"] in ["completed", "failed"]
    ]
    mean_time_to_resolution = sum(durations) / len(durations) if durations else 0.0

    return {
        "summary": {
            "total_triggered_jobs": total_tasks,
            "queued": queued,
            "running": running,
            "completed": completed,
            "failed": failed,
            "mean_time_to_resolution_seconds": round(mean_time_to_resolution, 2),
        },
        "tasks": task_store,
    }
