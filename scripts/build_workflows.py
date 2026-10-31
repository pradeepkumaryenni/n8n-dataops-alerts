"""Generate the n8n workflow JSON files in workflows/ from workflows/src/*.js.

Why generate instead of hand-editing exported JSON: JavaScript inside a JSON string is hard to
review and diff. The Code-node logic lives in plain .js files (which the tests execute with Node),
and this script assembles them into n8n's workflow export format, the same format the UI's
"Download" and `n8n export:workflow` produce, and that `n8n import:workflow` reads.

    python scripts/build_workflows.py          # (re)write workflows/*.json
    python scripts/build_workflows.py --check  # exit 1 if the committed JSON is out of date

IDs (workflow, node, webhook, version) are deterministic, so re-running produces no diff unless
something actually changed, and re-importing overwrites the same workflows instead of duplicating.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / "workflows"
SRC_DIR = WORKFLOW_DIR / "src"
NAMESPACE = uuid.UUID("6f1c2a52-4d1e-4c55-9a4b-2f7f3b0d8e11")

ROUTER_ID = "dataopsAlertRtr1"
DIGEST_ID = "dataopsDigest001"
ERROR_ID = "dataopsErrorWf01"

CRED_POSTGRES = {"postgres": {"id": "dataopsPostgres1", "name": "DataOps alert log (Postgres)"}}
CRED_SMTP = {"smtp": {"id": "dataopsSmtpMail1", "name": "SMTP (alert email)"}}
CRED_JIRA = {"httpBasicAuth": {"id": "dataopsJiraAuth1", "name": "Jira API (basic auth)"}}
CRED_WEBHOOK = {"httpBasicAuth": {"id": "dataopsHookAuth1", "name": "Alert webhook (basic auth)"}}

ALERT_WEBHOOK_PATH = "dataops-alert"
DIGEST_WEBHOOK_PATH = "dataops-digest"


def _uuid(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "/".join(parts)))


def _js(name: str) -> str:
    return (SRC_DIR / name).read_text(encoding="utf-8").replace("\r\n", "\n")


class Workflow:
    def __init__(self, workflow_id: str, name: str):
        self.id = workflow_id
        self.name = name
        self.nodes: list[dict[str, Any]] = []
        self.connections: dict[str, dict[str, list[list[dict[str, Any]]]]] = {}

    def node(self, name: str, node_type: str, version: float, position: tuple[int, int],
             parameters: dict[str, Any] | None = None, **extra: Any) -> str:
        node: dict[str, Any] = {
            "parameters": parameters or {},
            "id": _uuid(self.id, name),
            "name": name,
            "type": node_type,
            "typeVersion": version,
            "position": list(position),
        }
        node.update(extra)
        self.nodes.append(node)
        return name

    def sticky(self, key: str, content: str, position: tuple[int, int], width: int, height: int, color: int = 7) -> None:
        self.node(key, "n8n-nodes-base.stickyNote", 1, position,
                  {"content": content, "height": height, "width": width, "color": color})

    def connect(self, source: str, target: str, output: int = 0) -> None:
        outputs = self.connections.setdefault(source, {"main": []})["main"]
        while len(outputs) <= output:
            outputs.append([])
        outputs[output].append({"node": target, "type": "main", "index": 0})

    def to_json(self, settings: dict[str, Any], active: bool, description: str) -> dict[str, Any]:
        body = {
            "name": self.name,
            "nodes": self.nodes,
            "connections": self.connections,
            "settings": settings,
            "pinData": {},
            "meta": {"templateCredsSetupCompleted": True, "description": description},
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        return {
            "id": self.id,
            "versionId": _uuid(self.id, digest),
            "active": active,
            **body,
            "tags": [],
        }


def code_node(wf: Workflow, name: str, src: str, position: tuple[int, int], notes: str) -> str:
    return wf.node(name, "n8n-nodes-base.code", 2, position,
                   {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": _js(src)},
                   notes=notes, notesInFlow=False)


def equals_rule(wf: Workflow, label: str, value: str) -> dict[str, Any]:
    return {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": _uuid(wf.id, "switch", value),
                "leftValue": "={{ $json.action }}",
                "rightValue": value,
                "operator": {"type": "string", "operation": "equals"},
            }],
            "combinator": "and",
        },
        "renameOutput": True,
        "outputKey": label,
    }


def result_node(wf: Workflow, name: str, channel: str, ref_expr: str, ok_detail: str, position: tuple[int, int]) -> str:
    def assign(field: str, value: Any, kind: str) -> dict[str, Any]:
        return {"id": _uuid(wf.id, name, field), "name": field, "value": value, "type": kind}

    return wf.node(name, "n8n-nodes-base.set", 3.4, position, {
        "assignments": {"assignments": [
            assign("alert_id", "={{ $('Build Notifications').item.json.alert_id }}", "number"),
            assign("channel", channel, "string"),
            assign("status", "={{ $json.error ? 'FAILED' : 'SENT' }}", "string"),
            assign("external_ref", f"={{{{ $json.error ? '' : ({ref_expr}) }}}}", "string"),
            assign("detail", "={{ $json.error ? String($json.error.message || JSON.stringify($json.error)).slice(0, 500) : "
                             f"'{ok_detail}' }}}}", "string"),
        ]},
        "options": {},
    }, notes=f"Turns the {channel} success or error output into one delivery_log row.")


HTTP_RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000, "onError": "continueErrorOutput"}


def build_router() -> dict[str, Any]:
    wf = Workflow(ROUTER_ID, "DataOps Alert Router")
    y = 300

    wf.sticky("Note: Intake", "## 1. Intake\nDatabricks job webhooks, Azure Monitor (ADF) alerts and generic JSON "
              "arrive on `POST /webhook/dataops-alert` (basic auth). Normalise maps them to one schema; "
              "Classify applies ordered severity rules.", (-60, 40), 900, 420, 7)
    wf.sticky("Note: State", "## 2. Idempotency + de-dup + log\n`alerting.register_alert()` runs in one Postgres call "
              "under an advisory lock: replays are detected by event_id, repeats inside the window are "
              "SUPPRESSED, everything is written to `alert_log`. The webhook is acknowledged after this.",
              (880, 40), 680, 420, 5)
    wf.sticky("Note: Routing", "## 3. Route by decision\nPAGE: Slack + Jira + email. NOTIFY: Slack + Jira. "
              "RESOLVE: Slack. DIGEST: picked up by the daily digest workflow. Branches run top to bottom (Slack, email, "
              "then Jira); each channel retries 3x and a failure is recorded per channel instead of blocking the others.", (1600, -160), 1180, 1200, 4)

    webhook = wf.node("Alert Webhook", "n8n-nodes-base.webhook", 2.1, (0, y), {
        "httpMethod": "POST",
        "path": ALERT_WEBHOOK_PATH,
        "authentication": "basicAuth",
        "responseMode": "responseNode",
        "options": {},
    }, webhookId=_uuid(ROUTER_ID, "webhook", ALERT_WEBHOOK_PATH), credentials=CRED_WEBHOOK)

    normalise = code_node(wf, "Normalise Payload", "normalise.js", (220, y),
                          "Databricks / Azure Monitor / generic payload -> canonical alert + fingerprint + event_id.")
    classify = code_node(wf, "Classify Severity", "classify.js", (440, y),
                         "Ordered rules (first match wins) -> severity, rule id, reason, route, summary.")

    llm = wf.node("LLM Summary (optional)", "n8n-nodes-base.httpRequest", 4.2, (660, y), {
        "method": "POST",
        "url": "={{ ($env.LLM_API_BASE || 'https://api.openai.com/v1').replace(/\\/$/, '') + '/chat/completions' }}",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpHeaderAuth",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": (
            "={{ JSON.stringify({ model: $env.LLM_MODEL || 'gpt-4o-mini', temperature: 0.2, max_tokens: 160, messages: ["
            " { role: 'system', content: 'You summarise data pipeline alerts for an on-call data engineer in at most two sentences: "
            "what failed and the first thing to check. Use only the facts given.' },"
            " { role: 'user', content: JSON.stringify({ pipeline: $json.pipeline, environment: $json.environment, severity: $json.severity,"
            " event: $json.source_event, detail: $json.error_message, rule: $json.severity_reason }) } ] }) }}"
        ),
        "options": {"timeout": 15000},
    }, disabled=True, onError="continueRegularOutput",
        credentials={"httpHeaderAuth": {"id": "dataopsLlmKey001", "name": "LLM API key (optional)"}},
        notes="Disabled by default. Enable to replace the rule-based summary with an LLM one. "
              "Errors continue, so the LLM never blocks an alert.")

    attach = code_node(wf, "Attach Summary", "attach_summary.js", (880, y),
                       "Keeps the rule-based summary unless the LLM node produced text.")

    dedup = wf.node("Dedup + Log Alert", "n8n-nodes-base.postgres", 2.6, (1100, y), {
        "operation": "executeQuery",
        "query": "SELECT * FROM alerting.register_alert($1::jsonb, $2::int);",
        "options": {"queryReplacement": "={{ [ JSON.stringify($json), Number($env.DEDUP_WINDOW_MINUTES || 15) ] }}"},
    }, credentials=CRED_POSTGRES,
        notes="Atomic: idempotency on event_id, de-dup window per fingerprint, insert into alerting.alert_log.")

    respond = wf.node("Acknowledge Sender", "n8n-nodes-base.respondToWebhook", 1.1, (1320, y), {
        "respondWith": "json",
        "responseBody": (
            "={{ JSON.stringify({ alert_id: Number($json.alert_id), decision: $json.decision, replay: $json.is_replay,"
            " occurrence: $json.occurrence, severity: $json.alert.severity, rule: $json.alert.severity_rule,"
            " fingerprint: $json.alert.fingerprint }) }}"
        ),
        "options": {"responseCode": 200},
    }, notes="Responds once the alert is durably logged; fan-out continues after the response.")

    build = code_node(wf, "Build Notifications", "build_notifications.js", (1540, y),
                      "Slack blocks, Jira fields and email bodies; sets `action` (NONE for suppress/replay/log-only).")

    route = wf.node("Route by Decision", "n8n-nodes-base.switch", 3.2, (1760, y), {
        "rules": {"values": [
            equals_rule(wf, "PAGE", "PAGE"),
            equals_rule(wf, "NOTIFY", "NOTIFY"),
            equals_rule(wf, "RESOLVE", "RESOLVE"),
            equals_rule(wf, "DIGEST", "DIGEST"),
        ]},
        "options": {"fallbackOutput": "extra", "renameFallbackOutput": "NONE"},
    })

    slack = wf.node("Slack: Post Alert", "n8n-nodes-base.httpRequest", 4.2, (2040, 60), {
        "method": "POST",
        "url": "={{ $env.SLACK_WEBHOOK_URL }}",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json.slack) }}",
        "options": {"timeout": 10000},
    }, **HTTP_RETRY, notes="Slack incoming webhook (the mock receiver locally).")

    jira = wf.node("Jira: Create Issue", "n8n-nodes-base.httpRequest", 4.2, (2040, 540), {
        "method": "POST",
        "url": "={{ $env.JIRA_BASE_URL.replace(/\\/$/, '') }}/rest/api/2/issue",
        "authentication": "genericCredentialType",
        "genericAuthType": "httpBasicAuth",
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": (
            "={{ JSON.stringify({ fields: Object.assign({}, $json.jira.fields, {"
            " project: { key: $env.JIRA_PROJECT_KEY || 'DATA' }, issuetype: { name: $env.JIRA_ISSUE_TYPE || 'Bug' } }) }) }}"
        ),
        "options": {"timeout": 10000},
    }, credentials=CRED_JIRA, **HTTP_RETRY, notes="Jira REST v2 create issue (the mock receiver locally).")

    email = wf.node("Email: Page On-call", "n8n-nodes-base.emailSend", 2.1, (2040, 300), {
        "operation": "send",
        "fromEmail": "={{ $env.ALERT_EMAIL_FROM }}",
        "toEmail": "={{ $env.ONCALL_EMAIL_TO }}",
        "subject": "={{ $json.email.subject }}",
        "emailFormat": "both",
        "text": "={{ $json.email.text }}",
        "html": "={{ $json.email.html }}",
        "options": {"appendAttribution": False},
    }, credentials=CRED_SMTP, **HTTP_RETRY, notes="SMTP (Mailpit locally). CRITICAL only.")

    digest = wf.node("Queued for Daily Digest", "n8n-nodes-base.noOp", 1, (2040, 760),
                     notes="LOW alerts are already in alert_log; the digest workflow sends them once a day.")
    none = wf.node("No Notification", "n8n-nodes-base.noOp", 1, (2040, 920),
                   notes="SUPPRESS (duplicate in window), LOG_ONLY (recovery with nothing open) or a replayed event.")

    slack_res = result_node(wf, "Slack Result", "slack", "''", "Slack webhook returned 2xx", (2300, 60))
    jira_res = result_node(wf, "Jira Result", "jira", "$json.key || ''", "Jira issue created", (2300, 540))
    email_res = result_node(wf, "Email Result", "email", "$json.messageId || ''", "SMTP accepted the message", (2300, 300))

    record = wf.node("Record Delivery", "n8n-nodes-base.postgres", 2.6, (2560, 300), {
        "operation": "executeQuery",
        "query": (
            "INSERT INTO alerting.delivery_log (alert_id, channel, status, external_ref, detail)\n"
            "VALUES ($1, $2, $3, NULLIF($4, ''), $5)\n"
            "RETURNING delivery_id, alert_id, channel, status, external_ref;"
        ),
        "options": {"queryReplacement": "={{ [ $json.alert_id, $json.channel, $json.status, $json.external_ref, $json.detail ] }}"},
    }, credentials=CRED_POSTGRES)

    for a, b in [(webhook, normalise), (normalise, classify), (classify, llm), (llm, attach), (attach, dedup),
                 (dedup, respond), (respond, build), (build, route)]:
        wf.connect(a, b)
    for target in (slack, jira, email):
        wf.connect(route, target, 0)  # PAGE
    for target in (slack, jira):
        wf.connect(route, target, 1)  # NOTIFY
    wf.connect(route, slack, 2)       # RESOLVE
    wf.connect(route, digest, 3)      # DIGEST
    wf.connect(route, none, 4)        # fallback: NONE
    for channel, result in [(slack, slack_res), (jira, jira_res), (email, email_res)]:
        wf.connect(channel, result, 0)  # success
        wf.connect(channel, result, 1)  # error output (after retries)
        wf.connect(result, record)

    settings = {
        "executionOrder": "v1",
        "errorWorkflow": ERROR_ID,
        "saveDataSuccessExecution": "all",
        "saveDataErrorExecution": "all",
        "saveManualExecutions": True,
        "callerPolicy": "workflowsFromSameOwner",
    }
    return wf.to_json(settings, active=True, description=(
        "Receives pipeline failure / SLA events, classifies severity, de-duplicates, logs to Postgres and routes "
        "CRITICAL to Slack+Jira+email, HIGH to Slack+Jira, LOW to the daily digest."))


DIGEST_QUERY = """WITH pending AS (
    SELECT * FROM alerting.alert_log
     WHERE severity = 'LOW' AND digested_at IS NULL
), grouped AS (
    SELECT fingerprint, pipeline, environment, source, event_type,
           count(*)::int AS events,
           min(received_at) AS first_seen,
           max(received_at) AS last_seen,
           (array_agg(summary ORDER BY alert_id DESC))[1] AS latest_summary
      FROM pending
     GROUP BY fingerprint, pipeline, environment, source, event_type
)
SELECT (SELECT count(*)::int FROM pending) AS alert_count,
       (SELECT count(*)::int FROM grouped) AS group_count,
       (SELECT max(alert_id) FROM pending) AS cutoff_alert_id,
       COALESCE((SELECT jsonb_agg(to_jsonb(g) ORDER BY g.last_seen DESC) FROM grouped g), '[]'::jsonb) AS groups;"""

MARK_QUERY = """WITH marked AS (
    UPDATE alerting.alert_log
       SET digested_at = clock_timestamp()
     WHERE severity = 'LOW' AND digested_at IS NULL AND alert_id <= $1
    RETURNING alert_id
)
SELECT count(*)::int AS marked_digested FROM marked;"""


def build_digest() -> dict[str, Any]:
    wf = Workflow(DIGEST_ID, "DataOps Daily Digest")
    wf.sticky("Note: Digest", "## Daily digest of LOW alerts\nRuns at 08:00 (GENERIC_TIMEZONE). The webhook trigger "
              "exists so the demo can run it on demand. Alerts are marked digested only after the email "
              "was sent (at-least-once).", (-60, -120), 1300, 620, 6)
    schedule = wf.node("Every Day 08:00", "n8n-nodes-base.scheduleTrigger", 1.2, (0, 100), {
        "rule": {"interval": [{"field": "cronExpression", "expression": "0 8 * * *"}]},
    })
    hook = wf.node("Run Digest Now (webhook)", "n8n-nodes-base.webhook", 2.1, (0, 320), {
        "httpMethod": "POST",
        "path": DIGEST_WEBHOOK_PATH,
        "authentication": "basicAuth",
        "responseMode": "lastNode",
        "responseData": "firstEntryJson",
        "options": {},
    }, webhookId=_uuid(DIGEST_ID, "webhook", DIGEST_WEBHOOK_PATH), credentials=CRED_WEBHOOK)
    fetch = wf.node("Fetch Pending LOW Alerts", "n8n-nodes-base.postgres", 2.6, (240, 200), {
        "operation": "executeQuery", "query": DIGEST_QUERY, "options": {},
    }, credentials=CRED_POSTGRES)
    build = code_node(wf, "Build Digest", "build_digest.js", (460, 200), "Groups LOW alerts per fingerprint into one email.")
    check = wf.node("Anything to Send?", "n8n-nodes-base.if", 2.2, (680, 200), {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict", "version": 2},
            "conditions": [{
                "id": _uuid(DIGEST_ID, "if", "alert_count"),
                "leftValue": "={{ $json.alert_count }}",
                "rightValue": 0,
                "operator": {"type": "number", "operation": "gt"},
            }],
            "combinator": "and",
        },
        "options": {},
    })
    send = wf.node("Email: Send Digest", "n8n-nodes-base.emailSend", 2.1, (900, 100), {
        "operation": "send",
        "fromEmail": "={{ $env.ALERT_EMAIL_FROM }}",
        "toEmail": "={{ $env.DIGEST_EMAIL_TO }}",
        "subject": "={{ $json.subject }}",
        "emailFormat": "both",
        "text": "={{ $json.text }}",
        "html": "={{ $json.html }}",
        "options": {"appendAttribution": False},
    }, credentials=CRED_SMTP, retryOnFail=True, maxTries=3, waitBetweenTries=5000)
    mark = wf.node("Mark Digested", "n8n-nodes-base.postgres", 2.6, (1120, 100), {
        "operation": "executeQuery",
        "query": MARK_QUERY,
        "options": {"queryReplacement": "={{ [ $('Build Digest').item.json.cutoff_alert_id ] }}"},
    }, credentials=CRED_POSTGRES)
    nothing = wf.node("Nothing to Digest", "n8n-nodes-base.noOp", 1, (900, 320))

    wf.connect(schedule, fetch)
    wf.connect(hook, fetch)
    wf.connect(fetch, build)
    wf.connect(build, check)
    wf.connect(check, send, 0)
    wf.connect(check, nothing, 1)
    wf.connect(send, mark)
    settings = {"executionOrder": "v1", "errorWorkflow": ERROR_ID, "saveDataSuccessExecution": "all",
                "saveDataErrorExecution": "all", "callerPolicy": "workflowsFromSameOwner"}
    return wf.to_json(settings, active=True, description="Sends one email a day with all LOW alerts not yet digested.")


def build_error_handler() -> dict[str, Any]:
    wf = Workflow(ERROR_ID, "DataOps Alert Router - Error Handler")
    wf.sticky("Note: Errors", "## Error workflow\nSet as the error workflow of the router and the digest. Fires when an "
              "execution fails (bad payload, database down, a bug). Emails the platform team and logs to "
              "`alerting.workflow_error`; each branch continues on error so one failing does not hide the other.",
              (-60, -220), 760, 560, 3)
    trigger = wf.node("On Workflow Error", "n8n-nodes-base.errorTrigger", 1, (0, 60))
    mail = wf.node("Email: Platform Team", "n8n-nodes-base.emailSend", 2.1, (300, -60), {
        "operation": "send",
        "fromEmail": "={{ $env.ALERT_EMAIL_FROM }}",
        "toEmail": "={{ $env.PLATFORM_EMAIL_TO }}",
        "subject": "=[n8n] {{ $json.workflow.name }} execution {{ $json.execution.id }} failed at '{{ $json.execution.lastNodeExecuted }}'",
        "emailFormat": "text",
        "text": (
            "=The alerting pipeline itself failed, so an alert may not have been routed.\n\n"
            "Workflow: {{ $json.workflow.name }} ({{ $json.workflow.id }})\n"
            "Execution: {{ $json.execution.id }} {{ $json.execution.url }}\n"
            "Failed node: {{ $json.execution.lastNodeExecuted }}\n"
            "Error: {{ $json.execution.error.message }}\n"
        ),
        "options": {"appendAttribution": False},
    }, credentials=CRED_SMTP, onError="continueRegularOutput")
    log = wf.node("Log Workflow Error", "n8n-nodes-base.postgres", 2.6, (300, 180), {
        "operation": "executeQuery",
        "query": (
            "INSERT INTO alerting.workflow_error (workflow_id, workflow_name, execution_id, execution_url, failed_node, error_message)\n"
            "VALUES ($1, $2, $3, $4, $5, $6)\nRETURNING error_id;"
        ),
        "options": {"queryReplacement": (
            "={{ [ $json.workflow.id, $json.workflow.name, $json.execution.id, $json.execution.url,"
            " $json.execution.lastNodeExecuted, ($json.execution.error && $json.execution.error.message) || 'unknown error' ] }}"
        )},
    }, credentials=CRED_POSTGRES, onError="continueRegularOutput")
    wf.connect(trigger, mail)
    wf.connect(trigger, log)
    settings = {"executionOrder": "v1", "saveDataSuccessExecution": "all", "saveDataErrorExecution": "all",
                "callerPolicy": "workflowsFromSameOwner"}
    return wf.to_json(settings, active=True, description="Error workflow for the router and the digest.")


OUTPUTS = {
    "dataops-alert-router.json": build_router,
    "dataops-daily-digest.json": build_digest,
    "dataops-error-handler.json": build_error_handler,
}


def render_all() -> dict[str, str]:
    return {name: json.dumps(fn(), indent=2, ensure_ascii=False) + "\n" for name, fn in OUTPUTS.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if workflows/*.json differ from the sources")
    args = parser.parse_args()
    stale = []
    for name, text in render_all().items():
        path = WORKFLOW_DIR / name
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != text:
            stale.append(name)
            if not args.check:
                path.write_text(text, encoding="utf-8", newline="\n")
    if args.check:
        if stale:
            print("Out of date, run `python scripts/build_workflows.py`: " + ", ".join(stale))
            return 1
        print("workflows/*.json are up to date")
        return 0
    print("wrote: " + (", ".join(stale) if stale else "nothing (already up to date)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
