import os
from dotenv import load_dotenv
load_dotenv()

import time
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header
from pydantic import BaseModel
import requests

app = FastAPI(title="Devin Automation Orchestrator")

DEVIN_API_URL = "https://api.devin.ai/v1"
DEVIN_API_KEY = os.getenv("DEVIN_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

task_store: Dict[str, dict] = {}

class RemediationRequest(BaseModel):
    issue_id: str
    title: str
    description: str
    repository: str
    branch: str = "master"

def run_devin_remediation(task_id: str, payload: RemediationRequest):
    clean_key = os.getenv('DEVIN_API_KEY', '').strip()
    
    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json"
    }
    
    prompt = f"""
    You are an automated engineering agent resolving an open tracking issue.
    
    Repository: {payload.repository}
    Branch: {payload.branch}
    Issue Title: {payload.title}
    Issue Description: {payload.description}
    
    Please clone the repository, check out a new branch targeting this issue, resolve the bug or vulnerability completely without introducing code comments, verify the build passes, and open a structured Pull Request back to the master branch.
    """
    
    body = {
        "prompt": prompt
    }
    
    try:
        response = requests.post(f"{DEVIN_API_URL}/sessions", json=body, headers=headers)
        if response.status_code == 201:
            data = response.json()
            task_store[task_id].update({
                "status": "running",
                "session_id": data.get("session_id"),
                "updated_at": time.time()
            })
        else:
            print(f"DEBUG: Devin API returned {response.status_code}")
            print(f"DEBUG: Response body: {response.text}")
            task_store[task_id].update({
                "status": "failed",
                "error": f"Devin API returned status {response.status_code}",
                "updated_at": time.time()
            })
    except Exception as e:
        task_store[task_id].update({
            "status": "failed",
            "error": str(e),
            "updated_at": time.time()
        })

@app.post("/remediate")
async def trigger_remediation(payload: RemediationRequest, background_tasks: BackgroundTasks):
    task_id = f"task_{int(time.time())}"
    
    task_store[task_id] = {
        "issue_id": payload.issue_id,
        "title": payload.title,
        "status": "queued",
        "session_id": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "error": None
    }
    
    background_tasks.add_task(run_devin_remediation, task_id, payload)
    
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
                    repository=repo.get("full_name")
                )
                
                task_id = f"task_{int(time.time())}"
                task_store[task_id] = {
                    "issue_id": request_payload.issue_id,
                    "title": request_payload.title,
                    "status": "queued",
                    "session_id": None,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "error": None
                }
                background_tasks.add_task(run_devin_remediation, task_id, request_payload)
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
            "mean_time_to_resolution_seconds": round(mean_time_to_resolution, 2)
        },
        "tasks": task_store
    }