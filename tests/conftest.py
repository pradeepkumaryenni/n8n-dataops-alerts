from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / "workflows"
HARNESS = ROOT / "tests" / "js" / "run_code_node.js"

sys.path.insert(0, str(ROOT))

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="Node.js is needed to execute the n8n Code-node JavaScript")


def load_workflow(name: str) -> dict[str, Any]:
    return json.loads((WORKFLOW_DIR / name).read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.json"))


def code_of(node_name: str, workflow: str = "dataops-alert-router.json") -> str:
    for node in load_workflow(workflow)["nodes"]:
        if node["name"] == node_name:
            return node["parameters"]["jsCode"]
    raise KeyError(node_name)


class CodeNodeError(RuntimeError):
    pass


def run_code_node(node_name: str, items: list[dict[str, Any]], refs: dict[str, list[dict[str, Any]]] | None = None,
                  workflow: str = "dataops-alert-router.json") -> list[dict[str, Any]]:
    """Execute the jsCode of `node_name` (as stored in the workflow JSON) with Node.js."""
    payload = json.dumps({"code": code_of(node_name, workflow), "items": items, "refs": refs or {}})
    proc = subprocess.run([NODE, str(HARNESS)], input=payload.encode(), capture_output=True, timeout=30, check=True)
    result = json.loads(proc.stdout)
    if not result["ok"]:
        raise CodeNodeError(result["error"])
    return result["items"]


def normalise(payload: Any) -> dict[str, Any]:
    return run_code_node("Normalise Payload", [{"body": payload, "headers": {}, "query": {}}])[0]


def classify(alert: dict[str, Any]) -> dict[str, Any]:
    return run_code_node("Classify Severity", [alert])[0]


def normalise_and_classify(payload: Any) -> dict[str, Any]:
    return classify(normalise(payload))
