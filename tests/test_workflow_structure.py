"""Structural checks on the exported n8n workflows (no n8n needed)."""

from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from typing import Any, Iterator

import pytest

from conftest import ROOT, WORKFLOW_DIR, load_workflow, workflow_files
from scripts import build_workflows

TRIGGER_TYPES = {
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.errorTrigger",
}
STICKY = "n8n-nodes-base.stickyNote"

WORKFLOWS = [p.name for p in workflow_files()]


def _nodes(wf: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["name"]: n for n in wf["nodes"]}


def _edges(wf: dict[str, Any]) -> Iterator[tuple[str, int, str]]:
    for source, kinds in wf["connections"].items():
        for output_index, targets in enumerate(kinds.get("main", [])):
            for target in targets or []:
                yield source, output_index, target["node"]


def _targets(wf: dict[str, Any], source: str, output: int) -> set[str]:
    return {t for s, o, t in _edges(wf) if s == source and o == output}


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}[{i}]")


def test_three_workflows_exported():
    assert WORKFLOWS == ["dataops-alert-router.json", "dataops-daily-digest.json", "dataops-error-handler.json"]


def test_generated_json_is_up_to_date_with_sources():
    rendered = build_workflows.render_all()
    for name, text in rendered.items():
        assert (WORKFLOW_DIR / name).read_text(encoding="utf-8") == text, (
            f"{name} is stale: run `python scripts/build_workflows.py`")


@pytest.mark.parametrize("name", WORKFLOWS)
def test_import_format(name):
    wf = load_workflow(name)
    for key in ("id", "name", "nodes", "connections", "settings", "versionId"):
        assert key in wf, f"{name}: missing {key}"
    assert re.fullmatch(r"[A-Za-z0-9]{16}", wf["id"]), "n8n workflow ids are 16 alphanumeric characters"
    assert wf["settings"]["executionOrder"] == "v1"
    for node in wf["nodes"]:
        for key in ("id", "name", "type", "typeVersion", "position", "parameters"):
            assert key in node, f"{name}/{node.get('name')}: missing {key}"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_node_names_and_ids_are_unique(name):
    wf = load_workflow(name)
    names = [n["name"] for n in wf["nodes"]]
    ids = [n["id"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", WORKFLOWS)
def test_connections_reference_existing_nodes(name):
    wf = load_workflow(name)
    nodes = _nodes(wf)
    for source, _, target in _edges(wf):
        assert source in nodes, f"{name}: connection from unknown node {source!r}"
        assert target in nodes, f"{name}: connection to unknown node {target!r}"
        assert nodes[target]["type"] not in TRIGGER_TYPES, f"{name}: {target!r} is a trigger but has an input"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_every_node_is_connected_and_reachable_from_a_trigger(name):
    wf = load_workflow(name)
    nodes = {n: d for n, d in _nodes(wf).items() if d["type"] != STICKY}
    adjacency = defaultdict(set)
    for source, _, target in _edges(wf):
        adjacency[source].add(target)
    triggers = [n for n, d in nodes.items() if d["type"] in TRIGGER_TYPES]
    assert triggers, f"{name}: no trigger node"
    seen, queue = set(triggers), deque(triggers)
    while queue:
        for nxt in adjacency[queue.popleft()]:
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    assert set(nodes) - seen == set(), f"{name}: unreachable nodes {sorted(set(nodes) - seen)}"
    for n, d in nodes.items():
        if d["type"] in TRIGGER_TYPES:
            assert adjacency[n], f"{name}: trigger {n!r} is not connected to anything"


def test_router_webhook_is_present_and_protected():
    wf = load_workflow("dataops-alert-router.json")
    hooks = [n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook"]
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["parameters"]["path"] == "dataops-alert"
    assert hook["parameters"]["httpMethod"] == "POST"
    assert hook["parameters"]["authentication"] == "basicAuth"
    assert hook["parameters"]["responseMode"] == "responseNode"
    assert hook.get("webhookId"), "webhookId is needed for a stable production URL"
    assert any(n["type"] == "n8n-nodes-base.respondToWebhook" for n in wf["nodes"])


def test_digest_webhook_is_present_and_protected():
    wf = load_workflow("dataops-daily-digest.json")
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert hook["parameters"]["path"] == "dataops-digest"
    assert hook["parameters"]["authentication"] == "basicAuth"
    cron = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.scheduleTrigger")
    assert cron["parameters"]["rule"]["interval"][0]["expression"] == "0 8 * * *"


def test_router_pipeline_order():
    wf = load_workflow("dataops-alert-router.json")
    chain = ["Alert Webhook", "Normalise Payload", "Classify Severity", "LLM Summary (optional)", "Attach Summary",
             "Dedup + Log Alert", "Acknowledge Sender", "Build Notifications", "Route by Decision"]
    for a, b in zip(chain, chain[1:]):
        assert _targets(wf, a, 0) == {b}, f"{a} should feed {b}"


def test_routing_matrix():
    wf = load_workflow("dataops-alert-router.json")
    switch = _nodes(wf)["Route by Decision"]
    keys = [r["outputKey"] for r in switch["parameters"]["rules"]["values"]]
    assert keys == ["PAGE", "NOTIFY", "RESOLVE", "DIGEST"]
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"
    expected = {
        0: {"Slack: Post Alert", "Jira: Create Issue", "Email: Page On-call"},  # PAGE (CRITICAL)
        1: {"Slack: Post Alert", "Jira: Create Issue"},                         # NOTIFY (HIGH)
        2: {"Slack: Post Alert"},                                               # RESOLVE
        3: {"Queued for Daily Digest"},                                         # DIGEST (LOW)
        4: {"No Notification"},                                                 # fallback
    }
    for output, targets in expected.items():
        assert _targets(wf, "Route by Decision", output) == targets


def test_each_channel_records_success_and_failure():
    wf = load_workflow("dataops-alert-router.json")
    nodes = _nodes(wf)
    for channel, result in [("Slack: Post Alert", "Slack Result"), ("Jira: Create Issue", "Jira Result"),
                            ("Email: Page On-call", "Email Result")]:
        node = nodes[channel]
        assert node["onError"] == "continueErrorOutput", f"{channel}: a failure must not stop other channels"
        assert node["retryOnFail"] is True and node["maxTries"] >= 2
        assert _targets(wf, channel, 0) == {result}
        assert _targets(wf, channel, 1) == {result}
        assert _targets(wf, result, 0) == {"Record Delivery"}


def test_llm_node_is_optional_and_disabled_by_default():
    wf = load_workflow("dataops-alert-router.json")
    llm = _nodes(wf)["LLM Summary (optional)"]
    assert llm.get("disabled") is True
    assert llm["onError"] == "continueRegularOutput"


def test_error_workflow_is_wired():
    router = load_workflow("dataops-alert-router.json")
    digest = load_workflow("dataops-daily-digest.json")
    handler = load_workflow("dataops-error-handler.json")
    assert router["settings"]["errorWorkflow"] == handler["id"]
    assert digest["settings"]["errorWorkflow"] == handler["id"]
    assert any(n["type"] == "n8n-nodes-base.errorTrigger" for n in handler["nodes"])


def test_dedup_query_uses_bind_parameters():
    node = _nodes(load_workflow("dataops-alert-router.json"))["Dedup + Log Alert"]
    assert "$1::jsonb" in node["parameters"]["query"]
    assert "{{" not in node["parameters"]["query"], "no expressions inside SQL text (injection risk)"


@pytest.mark.parametrize("name", WORKFLOWS)
def test_sql_text_never_interpolates_expressions(name):
    for node in load_workflow(name)["nodes"]:
        if node["type"] == "n8n-nodes-base.postgres":
            assert "{{" not in node["parameters"]["query"], f"{node['name']}: use query parameters instead"


def test_code_nodes_match_their_source_files():
    sources = {
        ("dataops-alert-router.json", "Normalise Payload"): "normalise.js",
        ("dataops-alert-router.json", "Classify Severity"): "classify.js",
        ("dataops-alert-router.json", "Attach Summary"): "attach_summary.js",
        ("dataops-alert-router.json", "Build Notifications"): "build_notifications.js",
        ("dataops-daily-digest.json", "Build Digest"): "build_digest.js",
    }
    for (wf_name, node_name), src in sources.items():
        code = _nodes(load_workflow(wf_name))[node_name]["parameters"]["jsCode"]
        assert code == (WORKFLOW_DIR / "src" / src).read_text(encoding="utf-8").replace("\r\n", "\n")


# ----------------------------------------------------------------------------- secrets

SECRET_PATTERNS = {
    "Slack token": re.compile(r"xox[abposr]-[A-Za-z0-9-]{10,}"),
    "Slack webhook URL": re.compile(r"hooks\.slack\.com/services/"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "Atlassian API token": re.compile(r"\bATATT[A-Za-z0-9_=-]{20,}"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    "Credentials in URL": re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    "Private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Literal bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}"),
}


@pytest.mark.parametrize("name", WORKFLOWS)
def test_no_hardcoded_secrets(name):
    wf = load_workflow(name)
    for path, text in _strings(wf):
        for label, pattern in SECRET_PATTERNS.items():
            assert not pattern.search(text), f"{name}{path}: looks like a {label}"


def _keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


@pytest.mark.parametrize("name", WORKFLOWS)
def test_credentials_are_references_only(name):
    wf = load_workflow(name)
    forbidden = {"password", "token", "apikey", "api_key", "secret", "accesstoken", "authorization"}
    for node in wf["nodes"]:
        for ref in node.get("credentials", {}).values():
            assert set(ref) == {"id", "name"}, f"{node['name']}: credential must be an id/name reference"
        bad = {k for k in _keys(node["parameters"]) if k.lower() in forbidden}
        assert not bad, f"{node['name']}: secret-like parameter(s) {bad}; use an n8n credential"


def test_outbound_urls_come_from_env():
    wf = load_workflow("dataops-alert-router.json")
    for node in wf["nodes"]:
        if node["type"] == "n8n-nodes-base.httpRequest":
            url = node["parameters"]["url"]
            assert url.startswith("={{") and "$env." in url, f"{node['name']}: URL must come from an env var"
        if node["type"] == "n8n-nodes-base.emailSend":
            assert "$env." in node["parameters"]["toEmail"]


def test_referenced_credentials_are_provisioned_by_the_import_script():
    script = (ROOT / "n8n" / "render-credentials.js").read_text(encoding="utf-8")
    provisioned = set(re.findall(r"id: '([A-Za-z0-9]+)'", script))
    for path in workflow_files():
        for node in load_workflow(path.name)["nodes"]:
            for ref in node.get("credentials", {}).values():
                assert ref["id"] in provisioned, f"{path.name}/{node['name']}: credential {ref['id']} not provisioned"


def test_publish_list_matches_workflows():
    script = (ROOT / "n8n" / "import.sh").read_text(encoding="utf-8")
    ids = {load_workflow(p.name)["id"] for p in workflow_files()}
    match = re.search(r"N8N_PUBLISH_WORKFLOW_IDS:-([A-Za-z0-9 ]+)\}", script)
    assert match and set(match.group(1).split()) == ids


def test_no_secrets_committed_in_compose_or_env_example():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for text in (compose, env_example):
        for label, pattern in SECRET_PATTERNS.items():
            assert not pattern.search(text), f"looks like a {label}"
    # every secret-looking variable in .env.example is an obvious placeholder or empty
    for line in env_example.splitlines():
        if re.match(r"^[A-Z_]*(PASSWORD|TOKEN|API_KEY|ENCRYPTION_KEY|PASSWORD_HASH)=", line):
            value = line.split("=", 1)[1]
            assert value == "" or value.startswith("change-me"), line
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
