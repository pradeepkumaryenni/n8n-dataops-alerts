"""The simulator's sample events match the documented payload shapes."""

from __future__ import annotations

import jsonschema
import pytest

from simulator import events
from simulator.events import SCENARIO_ORDER, build_scenarios
from simulator.schemas import AZURE_MONITOR_COMMON_ALERT, DATABRICKS_JOB_WEBHOOK, GENERIC_EVENT, schema_for

FORMAT_CHECKER = jsonschema.Draft202012Validator.FORMAT_CHECKER


def _validate(payload, schema):
    jsonschema.Draft202012Validator(schema, format_checker=FORMAT_CHECKER).validate(payload)


def _all_steps():
    scenarios = build_scenarios()
    for name in SCENARIO_ORDER:
        for step in scenarios[name]:
            payloads = [step.payload_factory() for _ in range(2)] if step.payload_factory else [step.payload]
            for payload in payloads:
                yield pytest.param(name, step, payload, id=f"{name}:{step.label[:40]}")


def test_schemas_are_valid_json_schema():
    for schema in (DATABRICKS_JOB_WEBHOOK, AZURE_MONITOR_COMMON_ALERT, GENERIC_EVENT):
        jsonschema.Draft202012Validator.check_schema(schema)


def test_every_scenario_is_listed_in_order():
    assert set(build_scenarios()) == set(SCENARIO_ORDER)


@pytest.mark.parametrize("scenario,step,payload", list(_all_steps()))
def test_scenario_payload_matches_its_schema(scenario, step, payload):
    schema = schema_for(payload)
    if scenario in ("unknown_payload", "malformed_payload"):
        assert schema is None, "edge-case payloads are deliberately outside the known shapes"
        return
    assert schema is not None, f"{scenario}: payload does not look like any supported shape"
    _validate(payload, schema)


def test_databricks_builder_shape():
    payload = events.databricks_job_event("jobs.on_failure", "nightly_sales_etl", run_id=42)
    _validate(payload, DATABRICKS_JOB_WEBHOOK)
    assert payload == {
        "event_type": "jobs.on_failure",
        "workspace_id": events.PROD_WORKSPACE_ID,
        "run": {"run_id": 42},
        "job": {"job_id": "101", "name": "nightly_sales_etl"},
    }


def test_azure_fired_and_resolved_share_alert_id():
    fired = events.azure_monitor_adf_alert("pl_x", alert_guid="11111111-2222-3333-4444-555555555555")
    resolved = events.azure_monitor_adf_alert("pl_x", alert_guid="11111111-2222-3333-4444-555555555555",
                                              condition="Resolved")
    _validate(fired, AZURE_MONITOR_COMMON_ALERT)
    _validate(resolved, AZURE_MONITOR_COMMON_ALERT)
    assert fired["data"]["essentials"]["alertId"] == resolved["data"]["essentials"]["alertId"]
    assert "resolvedDateTime" in resolved["data"]["essentials"]
    assert "resolvedDateTime" not in fired["data"]["essentials"]


def test_azure_resolved_without_resolved_time_is_rejected():
    payload = events.azure_monitor_adf_alert("pl_x", condition="Resolved")
    del payload["data"]["essentials"]["resolvedDateTime"]
    with pytest.raises(jsonschema.ValidationError):
        _validate(payload, AZURE_MONITOR_COMMON_ALERT)


def test_elapsed_time_metric_carries_run_id_dimension():
    payload = events.azure_monitor_adf_alert("pl_x", metric="PipelineElapsedTimeRuns")
    dims = {d["name"] for d in payload["data"]["alertContext"]["condition"]["allOf"][0]["dimensions"]}
    assert dims == {"Name", "RunId"}


def test_generic_event_rejects_unknown_event_type():
    payload = events.generic_event(source="sla_monitor", event_type="sla_breach", pipeline="p", environment="prod",
                                   message="m")
    _validate(payload, GENERIC_EVENT)
    payload["event_type"] = "explosion"
    with pytest.raises(jsonschema.ValidationError):
        _validate(payload, GENERIC_EVENT)


def test_run_ids_are_unique_so_new_events_are_not_replays():
    ids = {events.next_run_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_burst_payloads_share_a_fingerprint_but_not_an_event():
    step = build_scenarios()["duplicate_burst"][0]
    payloads = [step.payload_factory() for _ in range(step.burst)]
    assert len({p["job"]["name"] for p in payloads}) == 1
    assert len({p["run"]["run_id"] for p in payloads}) == step.burst
    assert sum(step.burst_expect.values()) == step.burst
