import os
from dotenv import load_dotenv
load_dotenv()

import json
import time
import uuid
import hmac
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Tuple
from fastapi import FastAPI, BackgroundTasks, Header, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests

logger = logging.getLogger("asoc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log_startup_warnings()
    recover_in_flight_tasks()
    yield


app = FastAPI(title="Devin Automation Orchestrator", lifespan=lifespan)

DEVIN_API_URL = "https://api.devin.ai/v1"
GITHUB_API_URL = "https://api.github.com"
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

BASE_DIR = Path(__file__).resolve().parent
TASKS_FILE = BASE_DIR / "tasks.json"
TEMPLATES_DIR = BASE_DIR / "templates"

DEVIN_REQUEST_TIMEOUT = 30
SESSION_POLL_INTERVAL_SECONDS = 15
SESSION_POLL_MAX_ATTEMPTS = 240

TRIGGER_LABEL = "trigger-devin"
TERMINAL_SUCCESS_STATES = {"finished"}
TERMINAL_FAILURE_STATES = {"expired", "blocked"}
VALID_FAILURE_CATEGORIES = {"code_bug", "test_failure", "configuration"}
NON_TERMINAL_STATES = {"queued", "running"}

SESSION_START_COMMENT = "Autonomous remediation initiated by ASOC pipeline."
DASHBOARD_TOKEN_PLACEHOLDER = "__ASOC_DASHBOARD_TOKEN__"
RESTART_RECOVERY_FAILURE_REASON = (
    "Task was queued or running without an active Devin session when the orchestrator restarted."
)


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
    branch: str = "main"


def devin_headers() -> Dict[str, str]:
    clean_key = os.getenv("DEVIN_API_KEY", "").strip()
    return {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
    }


def get_api_token() -> str:
    return os.getenv("ASOC_API_TOKEN", "").strip()


def get_dashboard_token() -> str:
    return os.getenv("ASOC_DASHBOARD_TOKEN", "").strip()


def get_webhook_secret() -> str:
    return os.getenv("GITHUB_WEBHOOK_SECRET", "").strip()


def verify_bearer_token(expected: str, authorization: Optional[str]) -> None:
    if not expected:
        return
    provided = ""
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided = credentials.strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")


async def require_api_token(authorization: Optional[str] = Header(None)) -> None:
    verify_bearer_token(get_api_token(), authorization)


async def require_dashboard_token(authorization: Optional[str] = Header(None)) -> None:
    verify_bearer_token(get_dashboard_token(), authorization)


def verify_github_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"
    return hmac.compare_digest(expected, signature_header)


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
        requests.post(url, json={"body": body}, headers=headers, timeout=DEVIN_REQUEST_TIMEOUT)
    except requests.RequestException:
        return


def build_remediation_prompt(payload: RemediationRequest) -> str:
    return f"""
    You are an autonomous remediation agent in a closed-loop security operations pipeline.

    Repository: {payload.repository}
    Base Branch: {payload.branch}
    Issue Title: {payload.title}
    Issue Description: {payload.description}

    Isolation:
    Treat this issue as an independent unit of work. Operate only on a dedicated branch for this issue and do not depend on or modify work for any other issue, so multiple remediations can run in parallel safely.

    Remediation Phase:
    1. Clone the repository and create a new branch off {payload.branch} named for this issue.
    2. Reproduce the reported bug or vulnerability and confirm the root cause before changing code.
    3. Implement a complete, minimal fix without introducing any code comments.

    Verification Phase (mandatory before any Pull Request):
    4. Run the project's full automated test suite. If coverage for the affected behavior is missing, add tests.
    5. Run every configured linter, formatter, and type checker, plus the build.
    6. Only continue if all tests and checks pass. If they cannot be made to pass, stop and do not open a Pull Request.

    Delivery Phase:
    7. Rebase onto the latest {payload.branch}, resolving any merge conflicts so existing behavior is preserved, then re-run the full Verification Phase.
    8. Open a Pull Request back to {payload.branch} summarizing the root cause, the fix, and the verification you performed.

    Actionable Reporting:
    When you stop, set your session structured output to a JSON object with exactly these keys:
    - "result": "success" or "failure"
    - "failure_category": one of "code_bug", "test_failure", "configuration", or null when result is "success"
    - "failure_reason": a single concise sentence a human manager can read, or null when result is "success"
    - "pull_request_url": the opened Pull Request URL, or null when none was opened
    """


def update_task(task_id: str, **fields) -> None:
    if task_id not in task_store:
        return
    fields["updated_at"] = time.time()
    task_store[task_id].update(fields)
    save_tasks(task_store)


def normalize_failure_category(category: Optional[str]) -> str:
    if isinstance(category, str) and category in VALID_FAILURE_CATEGORIES:
        return category
    return "session_error"


def mark_task_failed(task_id: str, reason: str, category: str) -> None:
    update_task(
        task_id,
        status="failed",
        error=reason,
        failure_reason=reason,
        failure_category=category,
    )


def create_devin_session(payload: RemediationRequest) -> requests.Response:
    body = {"prompt": build_remediation_prompt(payload)}
    return requests.post(
        f"{DEVIN_API_URL}/sessions",
        json=body,
        headers=devin_headers(),
        timeout=DEVIN_REQUEST_TIMEOUT,
    )


def get_devin_session(session_id: str) -> requests.Response:
    return requests.get(
        f"{DEVIN_API_URL}/sessions/{session_id}",
        headers=devin_headers(),
        timeout=DEVIN_REQUEST_TIMEOUT,
    )


def extract_structured_output(data: dict) -> dict:
    structured = data.get("structured_output")
    return structured if isinstance(structured, dict) else {}


def extract_pull_request_url(data: dict, structured: dict) -> Optional[str]:
    pull_request = data.get("pull_request")
    if isinstance(pull_request, dict) and pull_request.get("url"):
        return pull_request["url"]
    candidate = structured.get("pull_request_url")
    return candidate if isinstance(candidate, str) and candidate else None


def finalize_success(task_id: str, payload: RemediationRequest, pr_url: Optional[str]) -> None:
    update_task(
        task_id,
        status="completed",
        pr_url=pr_url,
        failure_reason=None,
        failure_category=None,
        error=None,
    )
    if pr_url:
        comment = f"Autonomous remediation completed by ASOC pipeline. Pull Request: {pr_url}"
    else:
        comment = "Autonomous remediation completed by ASOC pipeline. No Pull Request was opened."
    post_issue_comment(payload.repository, payload.issue_id, comment)


def finalize_failure(task_id: str, payload: RemediationRequest, reason: str, category: str) -> None:
    mark_task_failed(task_id, reason, category)
    comment = (
        "Autonomous remediation failed in ASOC pipeline. "
        f"Failure category: {category}. Reason: {reason}"
    )
    post_issue_comment(payload.repository, payload.issue_id, comment)


def finalize_from_session(task_id: str, payload: RemediationRequest, data: dict) -> None:
    structured = extract_structured_output(data)
    pr_url = extract_pull_request_url(data, structured)
    status_enum = data.get("status_enum")

    if structured.get("result") == "failure" or status_enum in TERMINAL_FAILURE_STATES:
        reason = structured.get("failure_reason")
        if not isinstance(reason, str) or not reason:
            reason = f"Session ended in state '{status_enum}' without a successful remediation."
        category = normalize_failure_category(structured.get("failure_category"))
        finalize_failure(task_id, payload, reason, category)
        return

    finalize_success(task_id, payload, pr_url)


def poll_session(task_id: str, payload: RemediationRequest, session_id: str) -> None:
    for _ in range(SESSION_POLL_MAX_ATTEMPTS):
        time.sleep(SESSION_POLL_INTERVAL_SECONDS)
        try:
            response = get_devin_session(session_id)
        except requests.RequestException:
            continue

        if response.status_code != 200:
            continue

        data = response.json()
        status_enum = data.get("status_enum")
        if status_enum in TERMINAL_SUCCESS_STATES or status_enum in TERMINAL_FAILURE_STATES:
            finalize_from_session(task_id, payload, data)
            return

    finalize_failure(
        task_id,
        payload,
        "Session did not reach a terminal state within the polling window.",
        "configuration",
    )


def run_devin_remediation(task_id: str, payload: RemediationRequest) -> None:
    try:
        response = create_devin_session(payload)
    except requests.Timeout:
        finalize_failure(task_id, payload, "Devin API request timed out.", "configuration")
        return
    except requests.RequestException as e:
        finalize_failure(task_id, payload, f"Devin API connection error: {e}", "configuration")
        return

    if response.status_code != 201:
        finalize_failure(
            task_id,
            payload,
            f"Devin API returned status {response.status_code}.",
            "configuration",
        )
        return

    session_id = response.json().get("session_id")
    update_task(task_id, status="running", session_id=session_id)
    post_issue_comment(payload.repository, payload.issue_id, SESSION_START_COMMENT)

    if session_id:
        poll_session(task_id, payload, session_id)


def remediation_request_from_task(task: dict) -> RemediationRequest:
    return RemediationRequest(
        issue_id=str(task.get("issue_id", "")),
        title=task.get("title", "") or "",
        description=task.get("description", "") or "",
        repository=task.get("repository", "") or "",
        branch=task.get("branch", "main") or "main",
    )


def find_in_flight_task(repository: str, issue_id: str) -> Optional[str]:
    for task_id, task in task_store.items():
        if task.get("status") not in NON_TERMINAL_STATES:
            continue
        if task.get("repository") == repository and str(task.get("issue_id")) == str(issue_id):
            return task_id
    return None


def recover_in_flight_tasks() -> None:
    for task_id, task in list(task_store.items()):
        if task.get("status") not in NON_TERMINAL_STATES:
            continue
        session_id = task.get("session_id")
        if session_id:
            payload = remediation_request_from_task(task)
            update_task(task_id, status="running")
            thread = threading.Thread(
                target=poll_session,
                args=(task_id, payload, session_id),
                daemon=True,
            )
            thread.start()
        else:
            mark_task_failed(task_id, RESTART_RECOVERY_FAILURE_REASON, "configuration")


def log_startup_warnings() -> None:
    if not get_api_token():
        logger.warning("ASOC_API_TOKEN is not set; POST /remediate is unauthenticated.")
    if not get_webhook_secret():
        logger.warning("GITHUB_WEBHOOK_SECRET is not set; GitHub webhook signatures are not verified.")
    if not get_dashboard_token():
        logger.warning("ASOC_DASHBOARD_TOKEN is not set; GET /metrics is unauthenticated.")


def enqueue_task(payload: RemediationRequest, background_tasks: BackgroundTasks) -> Tuple[str, bool]:
    existing = find_in_flight_task(payload.repository, payload.issue_id)
    if existing:
        return existing, True

    task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    now = time.time()
    task_store[task_id] = {
        "issue_id": payload.issue_id,
        "title": payload.title,
        "repository": payload.repository,
        "branch": payload.branch,
        "status": "queued",
        "session_id": None,
        "pr_url": None,
        "failure_category": None,
        "failure_reason": None,
        "created_at": now,
        "updated_at": now,
        "error": None,
    }
    save_tasks(task_store)
    background_tasks.add_task(run_devin_remediation, task_id, payload)
    return task_id, False


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    template = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    rendered = template.replace(DASHBOARD_TOKEN_PLACEHOLDER, json.dumps(get_dashboard_token()))
    return HTMLResponse(content=rendered)


@app.post("/remediate")
async def trigger_remediation(
    payload: RemediationRequest,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_api_token),
):
    task_id, duplicate = enqueue_task(payload, background_tasks)
    if duplicate:
        return {"status": "duplicate", "task_id": task_id}
    return {"status": "accepted", "task_id": task_id}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: Optional[str] = Header(None),
    x_hub_signature_256: Optional[str] = Header(None),
):
    raw_body = await request.body()
    secret = get_webhook_secret()
    if secret:
        if not verify_github_signature(secret, raw_body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature.")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET is not set; skipping webhook signature verification.")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload.")

    if x_github_event != "issues":
        return {"status": "ignored"}

    if payload.get("action") != "labeled":
        return {"status": "ignored"}

    label_name = payload.get("label", {}).get("name", "")
    if label_name.lower() != TRIGGER_LABEL:
        return {"status": "ignored"}

    issue = payload.get("issue", {})
    repo = payload.get("repository", {})
    request_payload = RemediationRequest(
        issue_id=str(issue.get("number")),
        title=issue.get("title"),
        description=issue.get("body", ""),
        repository=repo.get("full_name"),
    )
    task_id, duplicate = enqueue_task(request_payload, background_tasks)
    if duplicate:
        return {"status": "duplicate", "task_id": task_id}
    return {"status": "webhook_processed", "task_id": task_id}


@app.get("/metrics")
async def get_metrics(_: None = Depends(require_dashboard_token)):
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
