"""The mock Slack/Jira receiver behaves like the endpoints it stands in for."""

from __future__ import annotations

import importlib.util
import sys

import pytest

from conftest import ROOT

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCK_LOG_FILE", str(tmp_path / "requests.jsonl"))
    spec = importlib.util.spec_from_file_location("mock_receiver_app", ROOT / "mock_receiver" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["mock_receiver_app"] = module
    spec.loader.exec_module(module)
    with TestClient(module.app) as c:
        yield c, tmp_path / "requests.jsonl"


AUTH = ("mock-user@example.com", "not-a-real-token")
ISSUE = {"fields": {"project": {"key": "DATA"}, "summary": "[HIGH] x failed", "issuetype": {"name": "Bug"}}}


def test_slack_accepts_text_and_records_it(client):
    c, log_file = client
    r = c.post("/slack/webhook", json={"text": "hello"})
    assert (r.status_code, r.text) == (200, "ok")
    assert c.get("/requests", params={"target": "slack"}).json()[0]["body"] == {"text": "hello"}
    assert log_file.read_text(encoding="utf-8").count("\n") == 1


def test_slack_rejects_empty_message(client):
    c, _ = client
    r = c.post("/slack/webhook", json={"foo": "bar"})
    assert (r.status_code, r.text) == (400, "no_text")


def test_jira_requires_basic_auth(client):
    c, _ = client
    assert c.post("/rest/api/2/issue", json=ISSUE).status_code == 401


def test_jira_creates_sequential_issue_keys(client):
    c, _ = client
    keys = [c.post("/rest/api/2/issue", json=ISSUE, auth=AUTH).json()["key"] for _ in range(2)]
    assert keys == ["DATA-1", "DATA-2"]
    recorded = c.get("/requests", params={"target": "jira"}).json()
    assert [r["issue_key"] for r in recorded] == keys
    assert recorded[0]["auth_user"] == AUTH[0]


def test_jira_validates_required_fields(client):
    c, _ = client
    r = c.post("/rest/api/2/issue", json={"fields": {"project": {"key": "DATA"}}}, auth=AUTH)
    assert r.status_code == 400
    assert set(r.json()["errors"]) == {"summary", "issuetype"}


def test_forced_failures_then_recovery(client):
    c, _ = client
    c.post("/admin/fail", json={"target": "jira", "count": 2, "status": 503})
    statuses = [c.post("/rest/api/2/issue", json=ISSUE, auth=AUTH).status_code for _ in range(3)]
    assert statuses == [503, 503, 201]


def test_reset_clears_everything(client):
    c, log_file = client
    c.post("/slack/webhook", json={"text": "x"})
    c.post("/rest/api/2/issue", json=ISSUE, auth=AUTH)
    assert c.delete("/requests").json() == {"cleared": 2}
    assert c.get("/requests").json() == []
    assert not log_file.exists()
    assert c.post("/rest/api/2/issue", json=ISSUE, auth=AUTH).json()["key"] == "DATA-1"
