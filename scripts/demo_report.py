"""Collect the evidence of a demo run: what the mock receiver got, what Mailpit caught, and what
the alert log says. Prints Markdown, so the output can be pasted into docs/demo.md.

    python scripts/demo_report.py > docs/demo-raw/report.md
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from simulator.simulate import setting  # noqa: E402

MOCK = f"http://127.0.0.1:{setting('MOCK_RECEIVER_PORT', '8000')}"
MAILPIT = f"http://127.0.0.1:{setting('MAILPIT_UI_PORT', '8025')}"

QUERIES = {
    "Alert log (`alerting.alert_overview`)": (
        "SELECT alert_id AS id, source, pipeline, environment AS env, event_type, status, severity, decision, "
        "occurrence AS occ, deliveries FROM alerting.alert_overview ORDER BY alert_id;"),
    "Decisions": "SELECT decision, severity, count(*) FROM alerting.alert_log GROUP BY 1, 2 ORDER BY 1, 2;",
    "Delivery log (`alerting.delivery_log`)": (
        "SELECT delivery_id AS id, alert_id, channel, status, external_ref, left(detail, 70) AS detail "
        "FROM alerting.delivery_log ORDER BY delivery_id;"),
    "Workflow errors (`alerting.workflow_error`)": (
        "SELECT error_id, workflow_name, execution_id, failed_node, error_message FROM alerting.workflow_error;"),
    "Digest bookkeeping": (
        "SELECT alert_id, pipeline, severity, decision, digested_at IS NOT NULL AS digested "
        "FROM alerting.alert_log WHERE severity = 'LOW' ORDER BY alert_id;"),
}


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def psql(sql: str) -> str:
    cmd = ["docker", "compose", "exec", "-T", "postgres", "sh", "-c",
           f'psql -U "$POSTGRES_USER" -d dataops_alerts -P pager=off -c "{sql}"']
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout.rstrip()


def mock_receiver_log() -> str:
    out = subprocess.run(["docker", "compose", "logs", "--no-log-prefix", "mock-receiver"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return "\n".join(line for line in out.splitlines() if " SLACK " in line or " JIRA " in line)


def main() -> None:
    requests_ = get_json(f"{MOCK}/requests")
    counts = Counter((r["target"], r["outcome"]) for r in requests_)
    print("## Mock receiver: what n8n sent to \"Slack\" and \"Jira\"\n")
    print("| target | outcome | count |\n|---|---|---|")
    for (target, outcome), n in sorted(counts.items()):
        print(f"| {target} | {outcome} | {n} |")
    print("\nContainer log (`docker compose logs mock-receiver`):\n\n```text")
    print(mock_receiver_log())
    print("```\n")

    slack = next((r for r in requests_ if r["target"] == "slack"), None)
    jira = next((r for r in requests_ if r["target"] == "jira" and r["outcome"] == "created"), None)
    if slack:
        print("First Slack message as received (Block Kit JSON):\n\n```json")
        print(json.dumps(slack["body"], indent=2, ensure_ascii=False))
        print("```\n")
    if jira:
        print(f"First Jira create-issue request (answered with {jira['issue_key']}):\n\n```json")
        print(json.dumps(jira["body"], indent=2, ensure_ascii=False))
        print("```\n")

    messages = list(reversed(get_json(f"{MAILPIT}/api/v1/messages").get("messages", [])))
    print("## Mailpit: emails caught\n")
    print("| to | subject |\n|---|---|")
    for m in messages:
        to = ", ".join(t["Address"] for t in m["To"])
        print(f"| {to} | {m['Subject']} |")
    print()
    shown = set()
    for m in messages:
        to = m["To"][0]["Address"] if m["To"] else ""
        if to in shown:
            continue
        shown.add(to)
        body = get_json(f"{MAILPIT}/api/v1/message/{m['ID']}").get("Text", "").strip()
        print(f"Plain-text body of the first email to {to}:\n\n```text\nSubject: {m['Subject']}\n\n{body}\n```\n")

    for title, sql in QUERIES.items():
        print(f"## {title}\n\n```text\n{psql(sql)}\n```\n")


if __name__ == "__main__":
    main()
