"""Unit tests for the JavaScript inside the n8n Code nodes.

The code is read from the workflow JSON (what n8n actually runs) and executed with Node.js through
tests/js/run_code_node.js.
"""

from __future__ import annotations

import pytest

from conftest import (
    CodeNodeError,
    classify,
    normalise,
    normalise_and_classify,
    requires_node,
    run_code_node,
)
from simulator import events
from simulator.events import SCENARIO_ORDER, build_scenarios

pytestmark = requires_node

ROUTE_FOR_DECISION = {"PAGE": "PAGE", "NOTIFY": "NOTIFY", "DIGEST": "DIGEST", "RESOLVE": "RESOLVE", "LOG_ONLY": "RESOLVE"}


# ----------------------------------------------------------------------------- Normalise Payload

def test_databricks_failure_is_normalised():
    alert = normalise(events.databricks_job_event("jobs.on_failure", "nightly_sales_etl", run_id=777))
    assert alert["source"] == "databricks"
    assert alert["event_type"] == "failure"
    assert alert["status"] == "FIRING"
    assert alert["pipeline"] == "nightly_sales_etl"
    assert alert["environment"] == "prod"
    assert alert["run_id"] == "777"
    assert alert["run_url"].endswith("/jobs/101/runs/777")
    assert alert["fingerprint"] == "databricks:prod:nightly_sales_etl:failure"
    assert alert["event_id"] == f"databricks:{events.PROD_WORKSPACE_ID}:101:777:jobs.on_failure"
    assert alert["raw"]["job"]["name"] == "nightly_sales_etl"


def test_databricks_success_resolves_the_failure_fingerprint():
    failed = normalise(events.databricks_job_event("jobs.on_failure", "nightly_sales_etl", run_id=1))
    succeeded = normalise(events.databricks_job_event("jobs.on_success", "nightly_sales_etl", run_id=2))
    assert succeeded["status"] == "RESOLVED"
    assert succeeded["fingerprint"] == failed["fingerprint"]
    assert succeeded["event_id"] != failed["event_id"]


def test_databricks_dev_workspace_maps_to_dev():
    alert = normalise(events.databricks_job_event("jobs.on_failure", "nightly_sales_etl",
                                                  workspace_id=events.DEV_WORKSPACE_ID))
    assert alert["environment"] == "dev"


def test_databricks_duration_warning_is_an_sla_breach():
    alert = normalise(events.databricks_job_event("jobs.on_duration_warning_threshold_exceeded", "nightly_sales_etl"))
    assert alert["event_type"] == "sla_breach"


def test_azure_monitor_adf_alert_is_normalised():
    payload = events.azure_monitor_adf_alert("pl_ingest_crm_accounts", failure_type="UserError", severity="Sev2")
    alert = normalise(payload)
    assert alert["source"] == "azure_monitor"
    assert alert["source_label"] == "ADF pipeline"
    assert alert["source_event"] == "Metric/PipelineFailedRuns"
    assert alert["pipeline"] == "pl_ingest_crm_accounts"
    assert alert["environment"] == "prod"  # from the factory name adf-dataops-prod
    assert alert["upstream_severity"] == "Sev2"
    assert "FailureType=UserError" in alert["error_message"]
    assert alert["occurred_at"] == payload["data"]["essentials"]["firedDateTime"].replace("Z", ".000Z")


def test_azure_fired_and_resolved_have_same_fingerprint_different_event_ids():
    guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    fired = normalise(events.azure_monitor_adf_alert("pl_load_finance_gl", alert_guid=guid))
    resolved = normalise(events.azure_monitor_adf_alert("pl_load_finance_gl", alert_guid=guid, condition="Resolved"))
    assert fired["fingerprint"] == resolved["fingerprint"]
    assert fired["event_id"].endswith(":FIRING")
    assert resolved["event_id"].endswith(":RESOLVED")
    assert resolved["status"] == "RESOLVED"


def test_azure_elapsed_time_metric_is_sla_breach():
    alert = normalise(events.azure_monitor_adf_alert("pl_x", metric="PipelineElapsedTimeRuns"))
    assert alert["event_type"] == "sla_breach"
    assert alert["run_id"]


def test_generic_event_is_normalised_and_event_id_is_namespaced():
    payload = events.generic_event(source="sla_monitor", event_type="sla_breach", pipeline="gold.sales_daily",
                                   environment="PROD", message="late", event_id="chk-1")
    alert = normalise(payload)
    assert alert["source"] == "sla_monitor"
    assert alert["environment"] == "prod"
    assert alert["event_id"] == "sla_monitor:chk-1"
    assert alert["error_message"] == "late"


def test_unknown_object_is_kept_not_dropped():
    alert = normalise({"alert": "disk usage 91%", "host": "vm-01"})
    assert alert["source"] == "unknown"
    assert alert["pipeline"] == "unknown"
    assert "disk usage 91%" in alert["error_message"]
    # same body -> same event id (treated as a retry), different body -> different id
    assert normalise({"alert": "disk usage 91%", "host": "vm-01"})["event_id"] == alert["event_id"]
    assert normalise({"alert": "disk usage 92%", "host": "vm-01"})["event_id"] != alert["event_id"]


@pytest.mark.parametrize("body", [[{"event_type": "jobs.on_failure"}], "plain text", None, 42])
def test_non_object_body_fails_the_execution(body):
    with pytest.raises(CodeNodeError, match="expected a JSON object"):
        normalise(body)


# ----------------------------------------------------------------------------- Classify Severity (rules)

def _alert(**overrides):
    base = {
        "source": "generic", "source_label": "Pipeline", "event_type": "failure", "status": "FIRING",
        "pipeline": "some_unlisted_pipeline", "environment": "prod", "run_id": None, "upstream_severity": None,
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("overrides,severity,rule,route", [
    ({"status": "RESOLVED", "pipeline": "nightly_sales_etl"}, "INFO", "R1_recovery", "RESOLVE"),
    ({"environment": "dev", "pipeline": "nightly_sales_etl"}, "LOW", "R2_non_prod", "DIGEST"),
    ({"environment": "staging", "upstream_severity": "Sev0"}, "LOW", "R2_non_prod", "DIGEST"),
    ({"pipeline": "nightly_sales_etl"}, "CRITICAL", "R3_tier1_failure", "PAGE"),
    ({"pipeline": "pl_refresh_marketing_lookup", "upstream_severity": "Sev1"}, "CRITICAL", "R4_upstream_sev0_sev1", "PAGE"),
    ({"event_type": "sla_breach", "upstream_severity": "Sev0"}, "CRITICAL", "R4_upstream_sev0_sev1", "PAGE"),
    ({"pipeline": "pl_ingest_crm_accounts"}, "HIGH", "R5_prod_failure", "NOTIFY"),
    ({"pipeline": "some_unlisted_pipeline", "upstream_severity": "Sev3"}, "HIGH", "R5_prod_failure", "NOTIFY"),
    ({"event_type": "sla_breach", "pipeline": "nightly_sales_etl"}, "HIGH", "R6_sla_breach_tier1_2", "NOTIFY"),
    ({"event_type": "sla_breach", "pipeline": "customer_360_refresh"}, "HIGH", "R6_sla_breach_tier1_2", "NOTIFY"),
    ({"event_type": "sla_breach", "pipeline": "pl_refresh_marketing_lookup"}, "LOW", "R7_default_low", "DIGEST"),
    ({"event_type": "info"}, "LOW", "R7_default_low", "DIGEST"),
    ({"event_type": "info", "upstream_severity": "Sev0"}, "LOW", "R7_default_low", "DIGEST"),
])
def test_severity_rules(overrides, severity, rule, route):
    out = classify(_alert(**overrides))
    assert (out["severity"], out["severity_rule"], out["route"]) == (severity, rule, route)
    assert out["severity_reason"]
    assert out["summary"].startswith(f"[{severity}]")


def test_unknown_environment_is_treated_like_production():
    # Conservative on purpose: if we cannot tell the environment, do not downgrade the alert.
    out = classify(_alert(environment="unknown", pipeline="nightly_sales_etl"))
    assert out["severity"] == "CRITICAL"


def test_unlisted_pipeline_defaults_to_tier_3():
    assert classify(_alert(pipeline="brand_new_pipeline"))["tier"] == 3


def _first_occurrence_steps():
    scenarios = build_scenarios()
    for name in SCENARIO_ORDER:
        for step in scenarios[name]:
            if step.expect_http != 200 or step.expect_replay:
                continue
            payload = step.payload_factory() if step.payload_factory else step.payload
            decision = step.expect_decision or next(d for d in step.burst_expect if d != "SUPPRESS")
            yield pytest.param(payload, decision, id=f"{name}:{step.label[:40]}")


@pytest.mark.parametrize("payload,decision", list(_first_occurrence_steps()))
def test_simulator_expectations_match_the_rules(payload, decision):
    """The decisions the simulator expects come from these rules (de-dup aside), not from guesswork."""
    assert normalise_and_classify(payload)["route"] == ROUTE_FOR_DECISION[decision]


# ----------------------------------------------------------------------------- Attach Summary

def test_attach_summary_keeps_rule_summary_when_llm_node_is_disabled():
    alert = classify(_alert(pipeline="nightly_sales_etl"))
    out = run_code_node("Attach Summary", [alert], refs={"Classify Severity": [alert]})[0]
    assert out["summary"] == alert["summary"]
    assert out["summary_source"] == "rules"


def test_attach_summary_uses_llm_text_when_present():
    alert = classify(_alert(pipeline="nightly_sales_etl"))
    llm_response = {"choices": [{"message": {"role": "assistant", "content": "  Sales ETL failed; check the source extract.  "}}]}
    out = run_code_node("Attach Summary", [llm_response], refs={"Classify Severity": [alert]})[0]
    assert out["summary"] == "Sales ETL failed; check the source extract."
    assert out["summary_source"] == "llm"
    assert out["severity"] == "CRITICAL"  # alert fields come back from the Classify node


def test_attach_summary_ignores_llm_error_output():
    alert = classify(_alert(pipeline="nightly_sales_etl"))
    out = run_code_node("Attach Summary", [{"error": {"message": "401 Unauthorized"}}],
                        refs={"Classify Severity": [alert]})[0]
    assert out["summary"] == alert["summary"]


# ----------------------------------------------------------------------------- Build Notifications

def _registered(alert, decision, replay=False, alert_id="42", occurrence=1):
    return {"alert_id": alert_id, "decision": decision, "occurrence": occurrence, "is_replay": replay,
            "window_started_at": "2026-11-01T00:00:00Z", "alert": alert}


@pytest.mark.parametrize("decision,replay,action", [
    ("PAGE", False, "PAGE"), ("NOTIFY", False, "NOTIFY"), ("RESOLVE", False, "RESOLVE"), ("DIGEST", False, "DIGEST"),
    ("SUPPRESS", False, "NONE"), ("LOG_ONLY", False, "NONE"), ("PAGE", True, "NONE"),
])
def test_action_never_notifies_for_suppressed_or_replayed(decision, replay, action):
    alert = normalise_and_classify(events.databricks_job_event("jobs.on_failure", "nightly_sales_etl"))
    out = run_code_node("Build Notifications", [_registered(alert, decision, replay)])[0]
    assert out["action"] == action
    assert out["alert_id"] == 42


def test_notification_payloads_are_well_formed():
    alert = normalise_and_classify(events.databricks_job_event("jobs.on_failure", "nightly_sales_etl", run_id=5))
    out = run_code_node("Build Notifications", [_registered(alert, "PAGE")])[0]
    assert out["slack"]["text"].startswith(":rotating_light: CRITICAL: nightly_sales_etl failed (prod)")
    assert out["slack"]["blocks"][0]["type"] == "header"
    fields = out["jira"]["fields"]
    assert fields["summary"] == "[CRITICAL] nightly_sales_etl failed (prod)"
    assert fields["priority"] == {"name": "Highest"}
    assert all(" " not in label for label in fields["labels"]), "Jira labels cannot contain spaces"
    assert "project" not in fields and "issuetype" not in fields, "set from env vars in the HTTP node"
    assert out["email"]["subject"] == "[CRITICAL] nightly_sales_etl failed in prod"
    assert "databricks:prod:nightly_sales_etl:failure" in out["email"]["text"]


def test_resolved_slack_message():
    guid = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
    alert = normalise_and_classify(events.azure_monitor_adf_alert("pl_load_finance_gl", alert_guid=guid,
                                                                  condition="Resolved"))
    out = run_code_node("Build Notifications", [_registered(alert, "RESOLVE")])[0]
    assert out["slack"]["text"] == ":white_check_mark: RESOLVED: pl_load_finance_gl (prod)"


def test_email_html_is_escaped():
    alert = normalise_and_classify(events.generic_event(
        source="sla_monitor", event_type="failure", pipeline="p<script>", environment="prod",
        message="<img src=x onerror=alert(1)>"))
    html = run_code_node("Build Notifications", [_registered(alert, "NOTIFY")])[0]["email"]["html"]
    assert "<script>" not in html and "<img" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html


# ----------------------------------------------------------------------------- Build Digest

def test_digest_groups_and_counts():
    row = {
        "alert_count": 5, "group_count": 2, "cutoff_alert_id": "17",
        "groups": [
            {"fingerprint": "a", "pipeline": "pl_a", "environment": "dev", "source": "databricks",
             "event_type": "failure", "events": 4, "first_seen": "2026-11-01T01:00:00+00:00",
             "last_seen": "2026-11-01T02:00:00+00:00", "latest_summary": "[LOW] a <b>"},
            {"fingerprint": "b", "pipeline": "pl_b", "environment": "prod", "source": "azure_monitor",
             "event_type": "sla_breach", "events": 1, "first_seen": "2026-11-01T03:00:00+00:00",
             "last_seen": "2026-11-01T03:00:00+00:00", "latest_summary": "[LOW] b"},
        ],
    }
    out = run_code_node("Build Digest", [row], workflow="dataops-daily-digest.json")[0]
    assert out["subject"] == "DataOps daily digest: 5 low-severity alerts across 2 pipelines"
    assert "&lt;b&gt;" in out["html"]
    assert out["cutoff_alert_id"] == "17"
    assert "- pl_a [dev] failure x4: [LOW] a <b>" in out["text"]


def test_empty_digest():
    out = run_code_node("Build Digest", [{"alert_count": 0, "group_count": 0, "cutoff_alert_id": None, "groups": []}],
                        workflow="dataops-daily-digest.json")[0]
    assert out["alert_count"] == 0
    assert out["subject"] == "DataOps daily digest: 0 low-severity alerts across 0 pipelines"
