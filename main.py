import os
from dotenv import load_dotenv
load_dotenv()

import io
import csv
import json
import time
import uuid
import hmac
import queue
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse
from fastapi import FastAPI, Header, Request, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import requests

from storage import TaskStore

logger = logging.getLogger("asoc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    imported = store.migrate_legacy_tasks(TASKS_FILE)
    if imported:
        logger.info(f"Imported {imported} task(s) from legacy {TASKS_FILE.name}.")
    log_startup_warnings()
    scheduler.start()
    recover_in_flight_tasks()
    backfill_acu_estimates()
    sweep_stop = start_sweep_thread()
    yield
    if sweep_stop:
        sweep_stop.set()


app = FastAPI(title="Devin Automation Orchestrator", lifespan=lifespan)

DEVIN_API_URL = "https://api.devin.ai/v1"
GITHUB_API_URL = "https://api.github.com"
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

BASE_DIR = Path(__file__).resolve().parent
TASKS_FILE = BASE_DIR / "tasks.json"
DB_FILE = BASE_DIR / "asoc.db"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

CSV_EXPORT_HEADERS = (
    "task_id",
    "issue_id",
    "repository",
    "status",
    "failure_category",
    "failure_reason",
    "pr_url",
    "created_at",
    "updated_at",
    "duration",
    "acu_used",
    "acu_estimated",
)

DEVIN_REQUEST_TIMEOUT = 30
SESSION_POLL_INTERVAL_SECONDS = 15
SESSION_POLL_MAX_ATTEMPTS = 240
PR_POLL_INTERVAL_SECONDS = 60
PR_POLL_MAX_ATTEMPTS = 2880

RETRY_MAX_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 30.0
RETRYABLE_STATUS_FLOOR = 500

DEFAULT_MAX_CONCURRENT_SESSIONS = 3
METRICS_DEFAULT_PAGE_SIZE = 50
METRICS_MAX_PAGE_SIZE = 200

TRIGGER_LABEL = "trigger-devin"
DEFAULT_SWEEP_INTERVAL_SECONDS = 300
PR_STATUS = "PR"
SESSION_BLOCKED_STATE = "blocked"
TERMINAL_SUCCESS_STATES = {"finished"}
TERMINAL_FAILURE_STATES = {"expired"}
SESSION_POLL_TERMINAL_STATES = TERMINAL_SUCCESS_STATES | TERMINAL_FAILURE_STATES | {SESSION_BLOCKED_STATE}
VALID_FAILURE_CATEGORIES = {"code_bug", "test_failure", "configuration"}
NON_TERMINAL_STATES = {"queued", "running", PR_STATUS}

STRUCTURED_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "enum": ["success", "failure"]},
        "failure_category": {
            "type": ["string", "null"],
            "enum": ["code_bug", "test_failure", "configuration", None],
        },
        "failure_reason": {"type": ["string", "null"]},
        "pull_request_url": {"type": ["string", "null"]},
    },
    "required": ["result", "failure_category", "failure_reason", "pull_request_url"],
    "additionalProperties": False,
}

LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}
TASK_LOG_FIELDS = ("task_id", "issue_id", "repository", "status", "failure_category")

SESSION_START_COMMENT = "Autonomous remediation initiated by ASOC pipeline."
DASHBOARD_TOKEN_PLACEHOLDER = "__ASOC_DASHBOARD_TOKEN__"
API_TOKEN_PLACEHOLDER = "__ASOC_API_TOKEN__"
ACU_BUDGET_PLACEHOLDER = "__ASOC_ACU_PERIOD_BUDGET__"
ACU_PERIOD_DAYS_PLACEHOLDER = "__ASOC_ACU_PERIOD_DAYS__"
RESTART_RECOVERY_FAILURE_REASON = (
    "Task was queued or running without an active Devin session when the orchestrator restarted."
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in TASK_LOG_FIELDS:
            if key in record.__dict__:
                payload[key] = record.__dict__[key]
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = LOG_LEVELS.get(level_name, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False


def log_task_transition(task_id: str, task: dict) -> None:
    logger.info(
        "task_state_transition",
        extra={
            "task_id": task_id,
            "issue_id": task.get("issue_id"),
            "repository": task.get("repository"),
            "status": task.get("status"),
            "failure_category": task.get("failure_category"),
        },
    )


store = TaskStore(DB_FILE)

cancel_lock = threading.Lock()
cancelled_task_ids: set = set()


def request_cancel(task_id: str) -> None:
    with cancel_lock:
        cancelled_task_ids.add(task_id)


def is_cancel_requested(task_id: str) -> bool:
    with cancel_lock:
        return task_id in cancelled_task_ids


def clear_cancel(task_id: str) -> None:
    with cancel_lock:
        cancelled_task_ids.discard(task_id)


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


def retry_delay(attempt: int) -> float:
    return min(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)), RETRY_MAX_DELAY_SECONDS)


def call_with_retries(operation: Callable[[], requests.Response], context: str) -> requests.Response:
    attempt = 1
    while True:
        try:
            response = operation()
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= RETRY_MAX_ATTEMPTS:
                raise
            logger.warning(f"Transient error calling {context}; retrying (attempt {attempt}).")
            time.sleep(retry_delay(attempt))
            attempt += 1
            continue
        if response.status_code >= RETRYABLE_STATUS_FLOOR and attempt < RETRY_MAX_ATTEMPTS:
            logger.warning(
                f"{context} returned status {response.status_code}; retrying (attempt {attempt})."
            )
            time.sleep(retry_delay(attempt))
            attempt += 1
            continue
        return response


def github_headers() -> Dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def post_issue_comment(repository: str, issue_id: str, body: str) -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token or not repository or not issue_id:
        return

    url = f"{GITHUB_API_URL}/repos/{repository}/issues/{issue_id}/comments"

    try:
        call_with_retries(
            lambda: requests.post(url, json={"body": body}, headers=github_headers(), timeout=DEVIN_REQUEST_TIMEOUT),
            "GitHub issue comment",
        )
    except requests.RequestException:
        return


def close_github_issue(repository: str, issue_id: str) -> None:
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token or not repository or not str(issue_id).isdigit():
        return

    url = f"{GITHUB_API_URL}/repos/{repository}/issues/{issue_id}"

    try:
        call_with_retries(
            lambda: requests.patch(
                url,
                json={"state": "closed", "state_reason": "completed"},
                headers=github_headers(),
                timeout=DEVIN_REQUEST_TIMEOUT,
            ),
            "GitHub issue close",
        )
    except requests.RequestException:
        return


def parse_github_pr_url(pr_url: str) -> Optional[Tuple[str, str, str]]:
    if not isinstance(pr_url, str):
        return None
    segments = [segment for segment in urlparse(pr_url).path.split("/") if segment]
    if len(segments) >= 4 and segments[2] == "pull" and segments[3].isdigit():
        return segments[0], segments[1], segments[3]
    return None


def get_github_pull_request(owner: str, repo: str, pr_number: str) -> requests.Response:
    url = f"{GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    return requests.get(url, headers=github_headers(), timeout=DEVIN_REQUEST_TIMEOUT)


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
    8. Open a Pull Request back to {payload.branch} summarizing the root cause, the fix, and the verification you performed. When this work corresponds to a tracked GitHub issue, include a closing keyword such as "Closes #{payload.issue_id}" in the Pull Request description so merging the Pull Request resolves that issue.

    Actionable Reporting:
    When you stop, set your session structured output to a JSON object matching the provided schema.
    The object must contain exactly these keys:
    - "result": "success" or "failure" (required)
    - "failure_category": one of "code_bug", "test_failure", "configuration", or null when result is "success" (required)
    - "failure_reason": a single concise sentence a human manager can read, or null when result is "success" (required)
    - "pull_request_url": the opened Pull Request URL, or null when none was opened (required)
    No additional keys are allowed.
    """


def update_task(task_id: str, **fields) -> None:
    task = store.update_task(task_id, **fields)
    if task is not None and "status" in fields:
        log_task_transition(task_id, task)


def normalize_failure_category(category: Optional[str]) -> str:
    if isinstance(category, str) and category in VALID_FAILURE_CATEGORIES:
        return category
    return "session_error"


def mark_task_failed(task_id: str, reason: str, category: str, acu_used: Optional[float] = None) -> None:
    fields: Dict[str, object] = {
        "status": "failed",
        "error": reason,
        "failure_reason": reason,
        "failure_category": category,
    }
    if acu_used is not None:
        fields["acu_used"] = acu_used
    update_task(task_id, **fields)


def resolve_acu_period_budget() -> Optional[int]:
    raw = os.getenv("ASOC_ACU_PERIOD_BUDGET", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def resolve_acu_period_days() -> int:
    raw = os.getenv("ASOC_ACU_PERIOD_DAYS", "").strip()
    if not raw:
        return 30
    try:
        value = int(raw)
    except ValueError:
        return 30
    return value if value >= 1 else 30


def resolve_acu_per_minute() -> Optional[float]:
    raw = os.getenv("ASOC_ACU_PER_MINUTE", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def estimate_acu_from_duration(task: dict) -> Optional[float]:
    rate = resolve_acu_per_minute()
    if rate is None:
        return None
    created = task.get("created_at")
    updated = task.get("updated_at")
    if not isinstance(created, (int, float)) or not isinstance(updated, (int, float)):
        return None
    duration_minutes = max((updated - created) / 60.0, 0)
    return round(duration_minutes * rate, 2)


def resolve_max_acu_limit() -> Optional[int]:
    raw = os.getenv("ASOC_MAX_ACU_PER_SESSION", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


def build_session_body(payload: RemediationRequest) -> dict:
    body: Dict[str, object] = {
        "prompt": build_remediation_prompt(payload),
        "structured_output_schema": STRUCTURED_OUTPUT_SCHEMA,
        "idempotent": True,
        "title": f"ASOC remediation: {payload.title}",
        "tags": ["asoc", f"issue-{payload.issue_id}", payload.repository],
    }
    max_acu = resolve_max_acu_limit()
    if max_acu is not None:
        body["max_acu_limit"] = max_acu
    return body


def create_devin_session(payload: RemediationRequest) -> requests.Response:
    body = build_session_body(payload)
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


def extract_acu_used(data: dict) -> Optional[float]:
    value = data.get("total_acu_used")
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def extract_pull_request_url(data: dict, structured: dict) -> Optional[str]:
    pull_request = data.get("pull_request")
    if isinstance(pull_request, dict) and pull_request.get("url"):
        return pull_request["url"]
    candidate = structured.get("pull_request_url")
    return candidate if isinstance(candidate, str) and candidate else None


def finalize_success(
    task_id: str, payload: RemediationRequest, pr_url: Optional[str], acu_used: Optional[float] = None
) -> None:
    fields: Dict[str, object] = {
        "status": "completed",
        "pr_url": pr_url,
        "failure_reason": None,
        "failure_category": None,
        "error": None,
    }
    if acu_used is not None:
        fields["acu_used"] = acu_used
    update_task(task_id, **fields)
    if pr_url:
        comment = f"Autonomous remediation completed by ASOC pipeline. Pull Request: {pr_url}"
    else:
        comment = "Autonomous remediation completed by ASOC pipeline. No Pull Request was opened."
    post_issue_comment(payload.repository, payload.issue_id, comment)


def finalize_failure(
    task_id: str, payload: RemediationRequest, reason: str, category: str, acu_used: Optional[float] = None
) -> None:
    mark_task_failed(task_id, reason, category, acu_used=acu_used)
    comment = (
        "Autonomous remediation failed in ASOC pipeline. "
        f"Failure category: {category}. Reason: {reason}"
    )
    post_issue_comment(payload.repository, payload.issue_id, comment)


def finalize_pr_merged(task_id: str, payload: RemediationRequest, pr_url: Optional[str]) -> None:
    update_task(
        task_id,
        status="completed",
        pr_url=pr_url,
        failure_reason=None,
        failure_category=None,
        error=None,
    )
    close_github_issue(payload.repository, payload.issue_id)
    comment = f"Autonomous remediation completed by ASOC pipeline. Merged Pull Request: {pr_url}"
    post_issue_comment(payload.repository, payload.issue_id, comment)


def poll_pr_for_merge(task_id: str, payload: RemediationRequest, pr_url: str) -> None:
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        finalize_failure(
            task_id,
            payload,
            f"Could not parse the Pull Request URL '{pr_url}' to track its merge status.",
            "configuration",
        )
        return
    owner, repo, pr_number = parsed

    for _ in range(PR_POLL_MAX_ATTEMPTS):
        if is_cancel_requested(task_id):
            clear_cancel(task_id)
            return
        try:
            response = get_github_pull_request(owner, repo, pr_number)
        except requests.RequestException:
            time.sleep(PR_POLL_INTERVAL_SECONDS)
            continue

        if response.status_code == 200:
            data = response.json()
            if data.get("merged"):
                if is_cancel_requested(task_id):
                    clear_cancel(task_id)
                    return
                finalize_pr_merged(task_id, payload, pr_url)
                return
            if data.get("state") == "closed":
                if is_cancel_requested(task_id):
                    clear_cancel(task_id)
                    return
                finalize_failure(
                    task_id,
                    payload,
                    "The Pull Request was closed without being merged.",
                    "configuration",
                )
                return

        time.sleep(PR_POLL_INTERVAL_SECONDS)

    if is_cancel_requested(task_id):
        clear_cancel(task_id)
        return

    finalize_failure(
        task_id,
        payload,
        "The Pull Request was not merged within the tracking window.",
        "configuration",
    )


def start_pr_tracking_thread(task_id: str, payload: RemediationRequest, pr_url: str) -> None:
    threading.Thread(
        target=poll_pr_for_merge,
        args=(task_id, payload, pr_url),
        daemon=True,
    ).start()


def begin_pr_tracking(task_id: str, payload: RemediationRequest, pr_url: str) -> None:
    update_task(
        task_id,
        status=PR_STATUS,
        pr_url=pr_url,
        failure_reason=None,
        failure_category=None,
        error=None,
    )
    comment = f"Autonomous remediation opened a Pull Request and is awaiting review and merge: {pr_url}"
    post_issue_comment(payload.repository, payload.issue_id, comment)
    start_pr_tracking_thread(task_id, payload, pr_url)


def finalize_from_session(task_id: str, payload: RemediationRequest, data: dict) -> None:
    structured = extract_structured_output(data)
    pr_url = extract_pull_request_url(data, structured)
    acu_used = extract_acu_used(data)
    acu_estimated = 0
    status_enum = data.get("status_enum")
    reported_failure = structured.get("result") == "failure"

    if acu_used is None:
        task = store.get_task(task_id)
        if task:
            estimated = estimate_acu_from_duration(task)
            if estimated is not None:
                acu_used = estimated
                acu_estimated = 1

    if acu_used is not None:
        update_task(task_id, acu_used=acu_used, acu_estimated=acu_estimated)

    if not reported_failure and status_enum == SESSION_BLOCKED_STATE and pr_url:
        begin_pr_tracking(task_id, payload, pr_url)
        return

    if reported_failure or status_enum in TERMINAL_FAILURE_STATES or status_enum == SESSION_BLOCKED_STATE:
        reason = structured.get("failure_reason")
        if not isinstance(reason, str) or not reason:
            reason = f"Session ended in state '{status_enum}' without a successful remediation."
        category = normalize_failure_category(structured.get("failure_category"))
        finalize_failure(task_id, payload, reason, category, acu_used=acu_used)
        return

    finalize_success(task_id, payload, pr_url, acu_used=acu_used)


def poll_session(task_id: str, payload: RemediationRequest, session_id: str) -> None:
    for _ in range(SESSION_POLL_MAX_ATTEMPTS):
        if is_cancel_requested(task_id):
            clear_cancel(task_id)
            return
        time.sleep(SESSION_POLL_INTERVAL_SECONDS)
        try:
            response = get_devin_session(session_id)
        except requests.RequestException:
            continue

        if response.status_code != 200:
            continue

        data = response.json()
        status_enum = data.get("status_enum")
        if status_enum in SESSION_POLL_TERMINAL_STATES:
            logger.debug(
                "session_terminal_response",
                extra={
                    "task_id": task_id,
                    "status_enum": status_enum,
                    "response_keys": sorted(data.keys()),
                    "has_total_acu_used": "total_acu_used" in data,
                    "total_acu_used_value": data.get("total_acu_used"),
                },
            )
            if is_cancel_requested(task_id):
                clear_cancel(task_id)
                return
            finalize_from_session(task_id, payload, data)
            return

    if is_cancel_requested(task_id):
        clear_cancel(task_id)
        return

    finalize_failure(
        task_id,
        payload,
        "Session did not reach a terminal state within the polling window.",
        "configuration",
    )


def run_devin_remediation(task_id: str, payload: RemediationRequest) -> None:
    if is_cancel_requested(task_id):
        clear_cancel(task_id)
        return
    try:
        response = call_with_retries(lambda: create_devin_session(payload), "Devin session creation")
    except requests.Timeout:
        finalize_failure(task_id, payload, "Devin API request timed out.", "configuration")
        return
    except requests.RequestException as e:
        finalize_failure(task_id, payload, f"Devin API connection error: {e}", "configuration")
        return

    if not response.ok:
        finalize_failure(
            task_id,
            payload,
            f"Devin API returned status {response.status_code}.",
            "configuration",
        )
        return

    response_data = response.json()
    session_id = response_data.get("session_id")
    session_url = response_data.get("url")
    update_task(task_id, status="running", session_id=session_id, session_url=session_url)
    post_issue_comment(payload.repository, payload.issue_id, SESSION_START_COMMENT)

    if session_id:
        poll_session(task_id, payload, session_id)


def resolve_max_concurrent_sessions() -> int:
    raw = os.getenv("ASOC_MAX_CONCURRENT_SESSIONS", "").strip()
    if not raw:
        return DEFAULT_MAX_CONCURRENT_SESSIONS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_SESSIONS
    return value if value >= 1 else DEFAULT_MAX_CONCURRENT_SESSIONS


class SessionScheduler:
    def __init__(self, max_concurrent: int, worker: Callable[[str, RemediationRequest], None]) -> None:
        self._max_concurrent = max_concurrent
        self._worker = worker
        self._queue: "queue.Queue[Tuple[str, RemediationRequest]]" = queue.Queue()
        self._semaphore = threading.Semaphore(max_concurrent)
        self._started = False
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            threading.Thread(target=self._drain, daemon=True).start()

    def submit(self, task_id: str, payload: RemediationRequest) -> None:
        self._queue.put((task_id, payload))

    def _drain(self) -> None:
        while True:
            task_id, payload = self._queue.get()
            self._semaphore.acquire()
            threading.Thread(
                target=self._run, args=(task_id, payload), daemon=True
            ).start()

    def _run(self, task_id: str, payload: RemediationRequest) -> None:
        try:
            self._worker(task_id, payload)
        finally:
            self._semaphore.release()


scheduler = SessionScheduler(resolve_max_concurrent_sessions(), run_devin_remediation)


def remediation_request_from_task(task: dict) -> RemediationRequest:
    return RemediationRequest(
        issue_id=str(task.get("issue_id", "")),
        title=task.get("title", "") or "",
        description=task.get("description", "") or "",
        repository=task.get("repository", "") or "",
        branch=task.get("branch", "main") or "main",
    )


def find_in_flight_task(repository: str, issue_id: str) -> Optional[str]:
    return store.find_in_flight(repository, issue_id, tuple(NON_TERMINAL_STATES))


def recover_in_flight_tasks() -> None:
    for task_id, task in store.load_tasks().items():
        if task.get("status") not in NON_TERMINAL_STATES:
            continue
        payload = remediation_request_from_task(task)
        if task.get("status") == PR_STATUS and task.get("pr_url"):
            start_pr_tracking_thread(task_id, payload, task["pr_url"])
            continue
        session_id = task.get("session_id")
        if session_id:
            update_task(task_id, status="running")
            thread = threading.Thread(
                target=poll_session,
                args=(task_id, payload, session_id),
                daemon=True,
            )
            thread.start()
        else:
            mark_task_failed(task_id, RESTART_RECOVERY_FAILURE_REASON, "configuration")


def backfill_acu_estimates() -> int:
    rate = resolve_acu_per_minute()
    if rate is None:
        return 0
    count = 0
    for task_id, task in store.load_tasks().items():
        if task.get("acu_used") is not None:
            continue
        estimated = estimate_acu_from_duration(task)
        if estimated is not None:
            update_task(task_id, acu_used=estimated, acu_estimated=1)
            count += 1
    if count:
        logger.info(f"Backfilled ACU estimates for {count} task(s).")
    return count


def resolve_sweep_enabled() -> bool:
    return os.getenv("ASOC_SWEEP_ENABLED", "").strip().lower() in ("1", "true", "yes")


def resolve_sweep_interval() -> int:
    raw = os.getenv("ASOC_SWEEP_INTERVAL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SWEEP_INTERVAL_SECONDS
    return value if value >= 10 else DEFAULT_SWEEP_INTERVAL_SECONDS


def resolve_sweep_repos() -> List[str]:
    raw = os.getenv("ASOC_SWEEP_REPOS", "").strip()
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


def resolve_sweep_label() -> str:
    return os.getenv("ASOC_SWEEP_LABEL", TRIGGER_LABEL).strip() or TRIGGER_LABEL


def list_labeled_issues(repository: str, label: str) -> List[dict]:
    url = f"{GITHUB_API_URL}/repos/{repository}/issues"
    params = {"labels": label, "state": "open", "per_page": "100"}
    try:
        response = requests.get(url, headers=github_headers(), params=params, timeout=DEVIN_REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        logger.warning("sweep_list_issues_failed", extra={"repository": repository})
        return []


def run_sweep() -> Dict[str, int]:
    repos = resolve_sweep_repos()
    label = resolve_sweep_label()
    enqueued = 0
    skipped = 0
    errors = 0

    for repo in repos:
        issues = list_labeled_issues(repo, label)
        for issue in issues:
            issue_id = str(issue.get("number", ""))
            title = issue.get("title", "")
            body = issue.get("body") or ""
            if not issue_id:
                errors += 1
                continue
            try:
                payload = RemediationRequest(
                    issue_id=issue_id,
                    title=title,
                    description=body,
                    repository=repo,
                )
                _, duplicate = enqueue_task(payload)
                if duplicate:
                    skipped += 1
                else:
                    enqueued += 1
            except Exception:
                errors += 1
                logger.exception("sweep_enqueue_error", extra={"repository": repo, "issue_id": issue_id})

    logger.info(
        "sweep_completed",
        extra={"enqueued": enqueued, "skipped": skipped, "errors": errors, "repos": len(repos)},
    )
    return {"enqueued": enqueued, "skipped": skipped, "errors": errors}


def sweep_loop(stop_event: threading.Event) -> None:
    interval = resolve_sweep_interval()
    logger.info(f"Scheduled sweep started (interval={interval}s, repos={resolve_sweep_repos()}).")
    while not stop_event.is_set():
        try:
            run_sweep()
        except Exception:
            logger.exception("sweep_loop_error")
        stop_event.wait(interval)


def start_sweep_thread() -> Optional[threading.Event]:
    if not resolve_sweep_enabled():
        return None
    repos = resolve_sweep_repos()
    if not repos:
        logger.warning("ASOC_SWEEP_ENABLED is set but ASOC_SWEEP_REPOS is empty; sweep disabled.")
        return None
    stop_event = threading.Event()
    thread = threading.Thread(target=sweep_loop, args=(stop_event,), daemon=True)
    thread.start()
    return stop_event


def log_startup_warnings() -> None:
    if not get_api_token():
        logger.warning("ASOC_API_TOKEN is not set; POST /remediate is unauthenticated.")
    if not get_webhook_secret():
        logger.warning("GITHUB_WEBHOOK_SECRET is not set; GitHub webhook signatures are not verified.")
    if not get_dashboard_token():
        logger.warning("ASOC_DASHBOARD_TOKEN is not set; GET /metrics is unauthenticated.")
    logger.info(
        f"Concurrency cap set to {resolve_max_concurrent_sessions()} simultaneous Devin session(s)."
    )


def enqueue_task(payload: RemediationRequest) -> Tuple[str, bool]:
    existing = find_in_flight_task(payload.repository, payload.issue_id)
    if existing:
        return existing, True

    task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    now = time.time()
    task = {
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
        "session_url": None,
        "acu_used": None,
        "acu_estimated": 0,
    }
    store.insert_task(task_id, task)
    log_task_transition(task_id, task)
    scheduler.submit(task_id, payload)
    return task_id, False


@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    template = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    rendered = template.replace(DASHBOARD_TOKEN_PLACEHOLDER, json.dumps(get_dashboard_token()))
    rendered = rendered.replace(API_TOKEN_PLACEHOLDER, json.dumps(get_api_token()))
    rendered = rendered.replace(ACU_BUDGET_PLACEHOLDER, json.dumps(resolve_acu_period_budget()))
    rendered = rendered.replace(ACU_PERIOD_DAYS_PLACEHOLDER, json.dumps(resolve_acu_period_days()))
    return HTMLResponse(content=rendered)


@app.post("/remediate")
async def trigger_remediation(
    payload: RemediationRequest,
    _: None = Depends(require_api_token),
):
    task_id, duplicate = enqueue_task(payload)
    if duplicate:
        return {"status": "duplicate", "task_id": task_id}
    return {"status": "accepted", "task_id": task_id}


def require_task(task_id: str) -> dict:
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


@app.post("/tasks/{task_id}/retry")
async def retry_task(task_id: str, _: None = Depends(require_api_token)):
    task = require_task(task_id)
    if task.get("status") != "failed":
        raise HTTPException(status_code=409, detail="Only failed tasks can be retried.")
    payload = remediation_request_from_task(task)
    new_task_id, duplicate = enqueue_task(payload)
    status = "duplicate" if duplicate else "accepted"
    return {"status": status, "task_id": new_task_id, "source_task_id": task_id}


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, _: None = Depends(require_api_token)):
    task = require_task(task_id)
    if task.get("status") not in NON_TERMINAL_STATES:
        raise HTTPException(status_code=409, detail="Only queued, running, or PR tasks can be cancelled.")
    request_cancel(task_id)
    update_task(
        task_id,
        status="cancelled",
        error=None,
        failure_reason=None,
        failure_category=None,
    )
    return {"status": "cancelled", "task_id": task_id}


@app.delete("/tasks/{task_id}")
async def delete_task(task_id: str, _: None = Depends(require_api_token)):
    task = require_task(task_id)
    if task.get("status") in NON_TERMINAL_STATES:
        request_cancel(task_id)
    store.delete_task(task_id)
    logger.info(
        "task_removed",
        extra={
            "task_id": task_id,
            "issue_id": task.get("issue_id"),
            "repository": task.get("repository"),
            "status": task.get("status"),
            "failure_category": task.get("failure_category"),
        },
    )
    return {"status": "removed", "task_id": task_id}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
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
    task_id, duplicate = enqueue_task(request_payload)
    if duplicate:
        return {"status": "duplicate", "task_id": task_id}
    return {"status": "webhook_processed", "task_id": task_id}


def mean_duration(tasks: List[Tuple[str, dict]], status: str) -> float:
    durations = [
        task["updated_at"] - task["created_at"]
        for _, task in tasks
        if task["status"] == status and task["created_at"] and task["updated_at"]
    ]
    return round(sum(durations) / len(durations), 2) if durations else 0.0


def build_summary(tasks: List[Tuple[str, dict]]) -> Dict[str, object]:
    statuses = [task["status"] for _, task in tasks]
    return {
        "total_triggered_jobs": len(tasks),
        "queued": statuses.count("queued"),
        "running": statuses.count("running"),
        "pr": statuses.count(PR_STATUS),
        "completed": statuses.count("completed"),
        "failed": statuses.count("failed"),
        "cancelled": statuses.count("cancelled"),
        "mean_time_to_resolution_seconds": mean_duration(tasks, "completed"),
        "mean_time_to_failure_seconds": mean_duration(tasks, "failed"),
    }


@app.get("/metrics")
async def get_metrics(
    status: Optional[str] = None,
    repository: Optional[str] = None,
    from_: Optional[float] = Query(None, alias="from"),
    to: Optional[float] = None,
    page: int = 1,
    page_size: int = METRICS_DEFAULT_PAGE_SIZE,
    _: None = Depends(require_dashboard_token),
):
    page = max(page, 1)
    page_size = max(1, min(page_size, METRICS_MAX_PAGE_SIZE))

    tasks = store.query_tasks(
        status=status, repository=repository, time_from=from_, time_to=to
    )
    summary = build_summary(tasks)
    total = len(tasks)

    start = (page - 1) * page_size
    page_items = tasks[start : start + page_size]

    return {
        "summary": summary,
        "tasks": {task_id: task for task_id, task in page_items},
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def task_duration_seconds(task: dict) -> Optional[float]:
    created = task.get("created_at")
    updated = task.get("updated_at")
    if created and updated:
        return round(updated - created, 2)
    return None


def isoformat_timestamp(value: Optional[float]) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def csv_row_for_task(task_id: str, task: dict) -> List[object]:
    duration = task_duration_seconds(task)
    acu = task.get("acu_used")
    return [
        task_id,
        task.get("issue_id") or "",
        task.get("repository") or "",
        task.get("status") or "",
        task.get("failure_category") or "",
        task.get("failure_reason") or "",
        task.get("pr_url") or "",
        isoformat_timestamp(task.get("created_at")),
        isoformat_timestamp(task.get("updated_at")),
        "" if duration is None else duration,
        "" if acu is None else acu,
        "yes" if task.get("acu_estimated") else "no",
    ]


@app.get("/export/tasks.csv")
async def export_tasks_csv(
    status: Optional[str] = None,
    repository: Optional[str] = None,
    from_: Optional[float] = Query(None, alias="from"),
    to: Optional[float] = None,
    _: None = Depends(require_dashboard_token),
):
    tasks = store.query_tasks(
        status=status, repository=repository, time_from=from_, time_to=to
    )

    def stream() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(CSV_EXPORT_HEADERS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for task_id, task in tasks:
            writer.writerow(csv_row_for_task(task_id, task))
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    headers = {"Content-Disposition": 'attachment; filename="tasks.csv"'}
    return StreamingResponse(stream(), media_type="text/csv", headers=headers)


@app.get("/healthz")
async def healthz():
    if store.ping():
        return {"status": "ok"}
    raise HTTPException(status_code=503, detail="Database connectivity check failed.")
