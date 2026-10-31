"""Mock Slack + Jira receiver for the local demo.

It impersonates two third-party endpoints so the n8n workflow can run end to end
without real Slack or Jira accounts:

  POST /slack/webhook          Slack "incoming webhook"   -> 200, body "ok"
  POST /rest/api/2/issue       Jira REST v2 "create issue" -> 201 {"id", "key", "self"}

Everything it receives is kept in memory, appended to a JSONL file and printed to
stdout, so you can show exactly what n8n sent:

  GET    /requests?target=slack|jira   list what was received (oldest first)
  DELETE /requests                     reset (memory and JSONL file)
  POST   /admin/fail                   make the next N calls to a target fail,
                                       e.g. {"target": "jira", "count": 5, "status": 503}
  GET    /health
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

LOG_FILE = Path(os.environ.get("MOCK_LOG_FILE", "/data/requests.jsonl"))
JIRA_PROJECT_KEY = os.environ.get("MOCK_JIRA_PROJECT_KEY", "DATA")

app = FastAPI(title="mock-receiver", version="1.0.0")

_lock = threading.Lock()
_records: list[dict[str, Any]] = []
_issue_counter = itertools.count(1)
_fail_budget: dict[str, dict[str, int]] = {}  # target -> {"remaining": n, "status": code}

Target = Literal["slack", "jira"]


class FailRequest(BaseModel):
    target: Target
    count: int = Field(default=1, ge=0, le=1000)
    status: int = Field(default=503, ge=400, le=599)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _record(target: str, outcome: str, http_status: int, body: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    rec = {
        "received_at": _now(),
        "target": target,
        "outcome": outcome,
        "http_status": http_status,
        "body": body,
        **(extra or {}),
    }
    with _lock:
        _records.append(rec)
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # the file is a convenience; the in-memory list is the source of truth
    print(f"{rec['received_at']} {target.upper():5} {http_status} {outcome}: {_headline(target, body, extra)}", flush=True)
    return rec


def _headline(target: str, body: Any, extra: dict[str, Any] | None) -> str:
    if not isinstance(body, dict):
        return str(body)[:120]
    if target == "slack":
        return str(body.get("text", ""))[:160]
    fields = body.get("fields", {}) if isinstance(body.get("fields"), dict) else {}
    key = (extra or {}).get("issue_key", "-")
    return f"{key} {str(fields.get('summary', ''))[:150]}"


def _consume_failure(target: str) -> int | None:
    with _lock:
        budget = _fail_budget.get(target)
        if budget and budget["remaining"] > 0:
            budget["remaining"] -= 1
            return budget["status"]
    return None


async def _json_body(request: Request) -> Any:
    raw = await request.body()
    try:
        return json.loads(raw or b"null")
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/slack/webhook")
async def slack_webhook(request: Request):
    body = await _json_body(request)
    forced = _consume_failure("slack")
    if forced:
        _record("slack", "forced_failure", forced, body)
        return PlainTextResponse("service_unavailable", status_code=forced)
    if not isinstance(body, dict) or not (body.get("text") or body.get("blocks")):
        _record("slack", "rejected", 400, body)
        return PlainTextResponse("no_text", status_code=400)  # what Slack returns for an empty message
    _record("slack", "accepted", 200, body)
    return PlainTextResponse("ok")


@app.post("/rest/api/2/issue")
async def jira_create_issue(request: Request):
    body = await _json_body(request)
    auth = request.headers.get("authorization", "")
    auth_user = None
    if auth.lower().startswith("basic "):
        try:
            auth_user = base64.b64decode(auth.split(" ", 1)[1]).decode().split(":", 1)[0]
        except (ValueError, UnicodeDecodeError):
            auth_user = None
    if not auth_user:
        _record("jira", "rejected", 401, body)
        return JSONResponse({"errorMessages": ["You are not authenticated."], "errors": {}}, status_code=401)

    forced = _consume_failure("jira")
    if forced:
        _record("jira", "forced_failure", forced, body, {"auth_user": auth_user})
        return JSONResponse({"errorMessages": ["Service unavailable (mock)"], "errors": {}}, status_code=forced)

    fields = body.get("fields") if isinstance(body, dict) else None
    errors: dict[str, str] = {}
    if not isinstance(fields, dict):
        errors["fields"] = "Request body must contain 'fields'."
    else:
        if not (fields.get("project") or {}).get("key"):
            errors["project"] = "Specify a valid project ID or key"
        if not fields.get("summary"):
            errors["summary"] = "You must specify a summary of the issue."
        if not (fields.get("issuetype") or {}).get("name"):
            errors["issuetype"] = "Specify an issue type"
    if errors:
        _record("jira", "rejected", 400, body, {"auth_user": auth_user})
        return JSONResponse({"errorMessages": [], "errors": errors}, status_code=400)

    project = fields["project"]["key"] or JIRA_PROJECT_KEY
    n = next(_issue_counter)
    issue = {"id": str(10000 + n), "key": f"{project}-{n}", "self": f"http://mock-receiver:8000/rest/api/2/issue/{10000 + n}"}
    _record("jira", "created", 201, body, {"auth_user": auth_user, "issue_key": issue["key"]})
    return JSONResponse(issue, status_code=201)


@app.get("/requests")
def list_requests(target: Target | None = None) -> list[dict[str, Any]]:
    with _lock:
        return [r for r in _records if target is None or r["target"] == target]


@app.delete("/requests")
def reset_requests() -> dict[str, Any]:
    global _issue_counter
    with _lock:
        cleared = len(_records)
        _records.clear()
        _fail_budget.clear()
        _issue_counter = itertools.count(1)
        try:
            LOG_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    return {"cleared": cleared}


@app.post("/admin/fail")
def set_failures(req: FailRequest) -> dict[str, Any]:
    with _lock:
        _fail_budget[req.target] = {"remaining": req.count, "status": req.status}
    return {"target": req.target, "fail_next": req.count, "status": req.status}
