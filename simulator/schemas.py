"""JSON Schemas (draft 2020-12) for the inbound payload shapes the simulator produces.

They pin down the fields the n8n "Normalise Payload" node relies on, so a change to a builder that
would silently break normalisation fails the tests instead.
"""

from __future__ import annotations

from typing import Any

DATABRICKS_JOB_WEBHOOK: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Databricks job notification webhook",
    "type": "object",
    "required": ["event_type", "workspace_id", "run", "job"],
    "properties": {
        "event_type": {
            "enum": [
                "jobs.on_start",
                "jobs.on_success",
                "jobs.on_failure",
                "jobs.on_duration_warning_threshold_exceeded",
                "jobs.on_streaming_backlog_exceeded",
            ]
        },
        "workspace_id": {"type": "string", "pattern": "^[0-9]+$"},
        "run": {
            "type": "object",
            "required": ["run_id"],
            "properties": {"run_id": {"type": "integer", "minimum": 1}},
        },
        "job": {
            "type": "object",
            "required": ["job_id", "name"],
            "properties": {"job_id": {"type": "string"}, "name": {"type": "string", "minLength": 1}},
        },
    },
}

AZURE_MONITOR_COMMON_ALERT: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Azure Monitor common alert schema (metric alert)",
    "type": "object",
    "required": ["schemaId", "data"],
    "properties": {
        "schemaId": {"const": "azureMonitorCommonAlertSchema"},
        "data": {
            "type": "object",
            "required": ["essentials", "alertContext"],
            "properties": {
                "essentials": {
                    "type": "object",
                    "required": [
                        "alertId", "alertRule", "severity", "signalType", "monitorCondition",
                        "alertTargetIDs", "firedDateTime",
                    ],
                    "properties": {
                        "alertId": {"type": "string", "pattern": "^/subscriptions/.+/alerts/.+$"},
                        "alertRule": {"type": "string", "minLength": 1},
                        "severity": {"enum": ["Sev0", "Sev1", "Sev2", "Sev3", "Sev4"]},
                        "signalType": {"enum": ["Metric", "Log", "Activity Log"]},
                        "monitorCondition": {"enum": ["Fired", "Resolved"]},
                        "alertTargetIDs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "firedDateTime": {"type": "string", "format": "date-time"},
                        "resolvedDateTime": {"type": "string", "format": "date-time"},
                    },
                    "if": {"properties": {"monitorCondition": {"const": "Resolved"}}},
                    "then": {"required": ["resolvedDateTime"]},
                },
                "alertContext": {
                    "type": "object",
                    "required": ["condition"],
                    "properties": {
                        "condition": {
                            "type": "object",
                            "required": ["allOf"],
                            "properties": {
                                "allOf": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "type": "object",
                                        "required": ["metricName", "metricNamespace", "operator", "threshold",
                                                     "dimensions"],
                                        "properties": {
                                            "metricName": {"type": "string"},
                                            "metricNamespace": {"const": "Microsoft.DataFactory/factories"},
                                            "dimensions": {
                                                "type": "array",
                                                "contains": {
                                                    "type": "object",
                                                    "properties": {"name": {"const": "Name"}},
                                                    "required": ["name", "value"],
                                                },
                                            },
                                        },
                                    },
                                }
                            },
                        }
                    },
                },
            },
        },
    },
}

GENERIC_EVENT: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Generic pipeline event (in-house checks)",
    "type": "object",
    "required": ["source", "event_id", "event_type", "status", "pipeline", "environment", "message"],
    "properties": {
        "source": {"type": "string", "pattern": "^[a-z0-9_.-]+$"},
        "event_id": {"type": "string", "minLength": 1},
        "event_type": {"enum": ["failure", "sla_breach", "info"]},
        "status": {"enum": ["FIRING", "RESOLVED"]},
        "pipeline": {"type": "string", "minLength": 1},
        "environment": {"type": "string", "minLength": 1},
        "message": {"type": "string"},
        "occurred_at": {"type": "string", "format": "date-time"},
    },
}


def schema_for(payload: Any) -> dict[str, Any] | None:
    """Return the schema a payload is supposed to match, or None for deliberately odd payloads."""
    if not isinstance(payload, dict):
        return None
    if str(payload.get("event_type", "")).startswith("jobs."):
        return DATABRICKS_JOB_WEBHOOK
    if payload.get("schemaId") == "azureMonitorCommonAlertSchema":
        return AZURE_MONITOR_COMMON_ALERT
    if "source" in payload and "pipeline" in payload:
        return GENERIC_EVENT
    return None
