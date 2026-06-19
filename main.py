import os
from dotenv import load_dotenv
load_dotenv()

import json
import time
from pathlib import Path
from typing import Dict, Optional
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


def run_devin_remediation(task_id: str, payload: RemediationRequest):
    clean_key = os.getenv("DEVIN_API_KEY", "").strip()

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
    }

    prompt = f"""
    You are an automated engineering agent resolving an open tracking issue.

    Repository: {payload.repository}
    Branch: {payload.branch}
    Issue Title: {payload.title}
    Issue Description: {payload.description}

    Please clone the repository, check out a new branch targeting this issue, resolve the bug or vulnerability completely without introducing code comments, verify the build passes, and open a structured Pull Request back to the master branch.
    """

    body = {"prompt": prompt}

    try:
        response = requests.post(f"{DEVIN_API_URL}/sessions", json=body, headers=headers)
        if response.status_code == 201:
            data = response.json()
            task_store[task_id].update({
                "status": "running",
                "session_id": data.get("session_id"),
                "updated_at": time.time(),
            })
            save_tasks(task_store)
            post_issue_comment(payload.repository, payload.issue_id, SESSION_START_COMMENT)
        else:
            task_store[task_id].update({
                "status": "failed",
                "error": f"Devin API returned status {response.status_code}",
                "updated_at": time.time(),
            })
            save_tasks(task_store)
    except Exception as e:
        task_store[task_id].update({
            "status": "failed",
            "error": str(e),
            "updated_at": time.time(),
        })
        save_tasks(task_store)


def enqueue_task(payload: RemediationRequest, background_tasks: BackgroundTasks) -> str:
    task_id = f"task_{int(time.time())}"
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
