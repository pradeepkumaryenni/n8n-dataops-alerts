"""Builders for realistic sample alert payloads, plus the demo scenarios.

Payload shapes:
  * Databricks job notification webhook (notification destination of type "webhook"):
    {"event_type": "jobs.on_failure", "workspace_id": ..., "run": {"run_id": ...}, "job": {"job_id": ..., "name": ...}}
  * Azure Monitor common alert schema, as sent by an action group webhook for an Azure Data Factory
    metric alert (PipelineFailedRuns / PipelineElapsedTimeRuns).
  * A generic JSON event, as an in-house freshness/SLA checker or an ADF Web activity would post.

All identifiers (workspace ids, subscription id, run ids) are made up for the demo.
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

PROD_WORKSPACE_ID = "1234567890123456"
DEV_WORKSPACE_ID = "2345678901234567"
SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP = "rg-dataops-prod"
DATA_FACTORY = "adf-dataops-prod"

# Job ids for the sample Databricks jobs.
DATABRICKS_JOBS = {"nightly_sales_etl": 101, "customer_360_refresh": 102}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


_run_counter = itertools.count(int(time.time() * 1000) % 1_000_000_000)


def next_run_id() -> int:
    """Run ids unique across simulator runs, so new events are never mistaken for webhook retries."""
    return next(_run_counter)


def databricks_job_event(event_type: str, job_name: str, *, run_id: int | None = None,
                         workspace_id: str = PROD_WORKSPACE_ID) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "workspace_id": workspace_id,
        "run": {"run_id": run_id if run_id is not None else next_run_id()},
        "job": {"job_id": str(DATABRICKS_JOBS.get(job_name, 199)), "name": job_name},
    }


def azure_monitor_adf_alert(pipeline: str, *, metric: str = "PipelineFailedRuns", condition: str = "Fired",
                            severity: str = "Sev2", alert_guid: str | None = None, rule: str | None = None,
                            failure_type: str = "UserError", fired_at: datetime | None = None,
                            factory: str = DATA_FACTORY) -> dict[str, Any]:
    fired = fired_at or datetime.now(timezone.utc)
    alert_guid = alert_guid or str(uuid.uuid4())
    resource_id = (f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
                   f"/providers/Microsoft.DataFactory/factories/{factory}")
    if metric == "PipelineElapsedTimeRuns":
        dimensions = [{"name": "Name", "value": pipeline}, {"name": "RunId", "value": str(uuid.uuid4())}]
        operator, threshold, value = "GreaterThan", "1", 1.0
    else:
        dimensions = [{"name": "Name", "value": pipeline}, {"name": "FailureType", "value": failure_type}]
        operator, threshold, value = "GreaterThan", "0", 1.0
    rule = rule or f"{factory}-{pipeline}-{'sla' if metric == 'PipelineElapsedTimeRuns' else 'failed'}"
    essentials = {
        "alertId": f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.AlertsManagement/alerts/{alert_guid}",
        "alertRule": rule,
        "severity": severity,
        "signalType": "Metric",
        "monitorCondition": condition,
        "monitoringService": "Platform",
        "alertTargetIDs": [resource_id.lower()],
        "configurationItems": [factory],
        "originAlertId": f"{SUBSCRIPTION_ID}_{RESOURCE_GROUP}_microsoft.insights_metricAlerts_{rule}_{int(alert_guid.replace('-', ''), 16) % 10**9}",
        "firedDateTime": _iso(fired),
        "description": "",
        "essentialsVersion": "1.0",
        "alertContextVersion": "1.0",
    }
    if condition == "Resolved":
        essentials["resolvedDateTime"] = _iso(fired + timedelta(minutes=12))
    return {
        "schemaId": "azureMonitorCommonAlertSchema",
        "data": {
            "essentials": essentials,
            "alertContext": {
                "properties": None,
                "conditionType": "SingleResourceMultipleMetricCriteria",
                "condition": {
                    "windowSize": "PT5M",
                    "allOf": [{
                        "metricName": metric,
                        "metricNamespace": "Microsoft.DataFactory/factories",
                        "operator": operator,
                        "threshold": threshold,
                        "timeAggregation": "Total",
                        "dimensions": dimensions,
                        "metricValue": value,
                        "webTestName": None,
                    }],
                    "windowStartTime": _iso(fired - timedelta(minutes=5)),
                    "windowEndTime": _iso(fired),
                },
            },
            "customProperties": None,
        },
    }


def generic_event(*, source: str, event_type: str, pipeline: str, environment: str, message: str,
                  status: str = "FIRING", event_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    return {
        "source": source,
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "status": status,
        "pipeline": pipeline,
        "environment": environment,
        "run_id": run_id,
        "message": message,
        "occurred_at": _iso(datetime.now(timezone.utc)),
    }


# ---------------------------------------------------------------------------------------------
# Demo scenarios. Each step states what the router should answer, so the simulator doubles as an
# end-to-end check. Expectations assume a fresh alert log (see `--reset`).
# ---------------------------------------------------------------------------------------------

@dataclass
class Step:
    label: str
    payload: Any
    expect_decision: str | None = None      # decision in the webhook response
    expect_replay: bool = False
    expect_http: int = 200
    burst: int = 1                          # >1: send `burst` copies concurrently (distinct run ids)
    burst_expect: dict[str, int] = field(default_factory=dict)  # decision -> count, for bursts
    before: list[tuple[str, dict[str, Any]]] = field(default_factory=list)  # mock-receiver admin calls
    payload_factory: Any = None             # callable producing one payload per burst copy


def build_scenarios() -> dict[str, list[Step]]:
    adf_crm_guid = str(uuid.uuid4())
    adf_crm_fired = azure_monitor_adf_alert("pl_ingest_crm_accounts", alert_guid=adf_crm_guid, severity="Sev2")
    adf_gl_guid = str(uuid.uuid4())
    adf_gl_fired_at = datetime.now(timezone.utc)

    return {
        "databricks_failure": [
            Step("Databricks job nightly_sales_etl failed (prod, tier 1)",
                 databricks_job_event("jobs.on_failure", "nightly_sales_etl"), expect_decision="PAGE"),
        ],
        "adf_failure": [
            Step("ADF pl_ingest_crm_accounts PipelineFailedRuns fired (prod, tier 2)",
                 adf_crm_fired, expect_decision="NOTIFY"),
            Step("Azure Monitor retries the same webhook delivery",
                 adf_crm_fired, expect_decision="NOTIFY", expect_replay=True),
        ],
        "sla_breach": [
            Step("Databricks nightly_sales_etl exceeded its duration threshold (tier 1)",
                 databricks_job_event("jobs.on_duration_warning_threshold_exceeded", "nightly_sales_etl"),
                 expect_decision="NOTIFY"),
            Step("Freshness check: gold.sales_daily not refreshed by 06:00 (tier 1)",
                 generic_event(source="sla_monitor", event_type="sla_breach", pipeline="gold.sales_daily",
                               environment="prod",
                               message="gold.sales_daily last refreshed 26h ago; SLA is 06:00 IST daily"),
                 expect_decision="NOTIFY"),
            Step("ADF pl_refresh_marketing_lookup elapsed-time breach (tier 3)",
                 azure_monitor_adf_alert("pl_refresh_marketing_lookup", metric="PipelineElapsedTimeRuns",
                                         severity="Sev3"),
                 expect_decision="DIGEST"),
        ],
        "non_prod_failure": [
            Step("Databricks nightly_sales_etl failed in the dev workspace",
                 databricks_job_event("jobs.on_failure", "nightly_sales_etl", workspace_id=DEV_WORKSPACE_ID),
                 expect_decision="DIGEST"),
        ],
        "duplicate_burst": [
            Step("5 failures of customer_360_refresh arrive at once (tier 2)", None,
                 burst=5, burst_expect={"NOTIFY": 1, "SUPPRESS": 4},
                 payload_factory=lambda: databricks_job_event("jobs.on_failure", "customer_360_refresh")),
        ],
        "recovery": [
            Step("ADF pl_load_finance_gl failed (prod, tier 1)",
                 azure_monitor_adf_alert("pl_load_finance_gl", alert_guid=adf_gl_guid, fired_at=adf_gl_fired_at,
                                         failure_type="SystemError"),
                 expect_decision="PAGE"),
            Step("ADF pl_load_finance_gl alert resolved",
                 azure_monitor_adf_alert("pl_load_finance_gl", alert_guid=adf_gl_guid, fired_at=adf_gl_fired_at,
                                         condition="Resolved", failure_type="SystemError"),
                 expect_decision="RESOLVE"),
            Step("Dev nightly_sales_etl succeeded (nothing paged, so log only)",
                 databricks_job_event("jobs.on_success", "nightly_sales_etl", workspace_id=DEV_WORKSPACE_ID),
                 expect_decision="LOG_ONLY"),
        ],
        "jira_outage": [
            Step("Jira is down (mock returns 503 x3) while a Sev1 ADF alert fires",
                 azure_monitor_adf_alert("pl_refresh_marketing_lookup", severity="Sev1",
                                         failure_type="SystemError"),
                 expect_decision="PAGE",
                 before=[("/admin/fail", {"target": "jira", "count": 3, "status": 503})]),
        ],
        "unknown_payload": [
            Step("Unrecognised JSON from some other tool",
                 {"alert": "disk usage 91%", "host": "vm-shir-01"}, expect_decision="DIGEST"),
        ],
        "malformed_payload": [
            Step("Body is a JSON array, not an object (fails the execution)",
                 [{"event_type": "jobs.on_failure"}], expect_http=500),
        ],
    }


SCENARIO_ORDER = [
    "databricks_failure",
    "adf_failure",
    "sla_breach",
    "non_prod_failure",
    "duplicate_burst",
    "recovery",
    "jira_outage",
    "unknown_payload",
    "malformed_payload",
]
