"""Fire sample DataOps alert events at the n8n webhook and check how they were routed.

    python -m simulator.simulate --reset            # clean demo state, then run every scenario
    python -m simulator.simulate -s duplicate_burst # one scenario
    python -m simulator.simulate --run-digest       # trigger the daily digest now
    python -m simulator.simulate --list

Only the Python standard library is used. The webhook basic-auth user/password are read from the
environment (ALERT_WEBHOOK_USER / ALERT_WEBHOOK_PASSWORD) or from the repository's .env file.
Exit code 1 if any response differs from the scenario's expectation.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # allow `python simulator/simulate.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from simulator.events import SCENARIO_ORDER, Step, build_scenarios  # type: ignore
else:
    from .events import SCENARIO_ORDER, Step, build_scenarios

ROOT = Path(__file__).resolve().parents[1]
ALERT_PATH = "/webhook/dataops-alert"
DIGEST_PATH = "/webhook/dataops-digest"


def load_dotenv(path: Path = ROOT / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def setting(name: str, default: str = "") -> str:
    return os.environ.get(name) or load_dotenv().get(name) or default


class Client:
    def __init__(self, base_url: str, user: str, password: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth_header = f"Basic {token}"
        self.timeout = timeout

    def post(self, path: str, payload: Any, auth: bool = True) -> tuple[int, Any]:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", self.auth_header)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, _parse(err.read())


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw or b"null")
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")[:300]


def http(method: str, url: str, body: Any | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, _parse(err.read())


def wait_for_webhooks(client: Client, paths: list[str], seconds: int) -> None:
    """n8n registers production webhooks shortly after the editor is up; 401 = registered (auth required)."""
    deadline = time.time() + seconds
    pending, last = list(paths), None
    while pending and time.time() < deadline:
        try:
            status, _ = client.post(pending[0], {}, auth=False)
            last = status
            if status in (401, 403):
                pending.pop(0)
                continue
        except OSError as err:  # connection refused while n8n starts
            last = err
        time.sleep(2)
    if pending:
        raise SystemExit(f"Webhook {client.base_url}{pending[0]} not ready after {seconds}s (last: {last}). "
                         "Is `docker compose up` finished? Check `docker compose logs n8n`.")


def reset_demo_state(mock_url: str, mailpit_url: str) -> None:
    http("DELETE", f"{mock_url}/requests")
    http("DELETE", f"{mailpit_url}/api/v1/messages")
    sql = ("TRUNCATE alerting.delivery_log, alerting.alert_log, alerting.dedup_state, "
           "alerting.workflow_error RESTART IDENTITY CASCADE;")
    cmd = ["docker", "compose", "exec", "-T", "postgres", "sh", "-c",
           f'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d dataops_alerts -c "{sql}"']
    subprocess.run(cmd, cwd=ROOT, check=True)
    print("reset: mock receiver, Mailpit and alert tables cleared")


def run_step(client: Client, mock_url: str, step: Step) -> list[dict[str, Any]]:
    for path, body in step.before:
        http("POST", mock_url + path, body)
    if step.burst > 1:
        payloads = [step.payload_factory() for _ in range(step.burst)]
        gate = threading.Barrier(step.burst)

        def fire(p: Any) -> tuple[int, Any]:
            gate.wait()
            return client.post(ALERT_PATH, p)

        with ThreadPoolExecutor(max_workers=step.burst) as pool:
            results = list(pool.map(fire, payloads))
    else:
        results = [client.post(ALERT_PATH, step.payload)]
    return [{"status": s, "body": b} for s, b in results]


def check(step: Step, results: list[dict[str, Any]]) -> tuple[bool, str]:
    if step.burst > 1:
        got = Counter(r["body"].get("decision") if isinstance(r["body"], dict) else None for r in results)
        ok = all(r["status"] == 200 for r in results) and dict(got) == step.burst_expect
        return ok, f"expected {step.burst_expect}, got {dict(got)}"
    r = results[0]
    if r["status"] != step.expect_http:
        return False, f"expected HTTP {step.expect_http}, got {r['status']}"
    if step.expect_http != 200:
        return True, f"HTTP {r['status']} as expected"
    body = r["body"] if isinstance(r["body"], dict) else {}
    ok = body.get("decision") == step.expect_decision and bool(body.get("replay")) == step.expect_replay
    exp = step.expect_decision + (" (replay)" if step.expect_replay else "")
    return ok, f"expected {exp}"


def describe(results: list[dict[str, Any]]) -> str:
    parts = []
    for r in results:
        b = r["body"]
        if isinstance(b, dict) and "decision" in b:
            parts.append(f"{b['decision']}{' replay' if b.get('replay') else ''} sev={b.get('severity')} "
                         f"rule={b.get('rule')} id={b.get('alert_id')}")
        else:
            parts.append(f"HTTP {r['status']} {str(b)[:80]}")
    if len(parts) > 1:
        return "; ".join(sorted(parts))
    return parts[0]


def main(argv: list[str] | None = None) -> int:
    scenarios = build_scenarios()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--scenario", action="append", choices=SCENARIO_ORDER,
                        help="scenario to run (repeatable); default: all, in order")
    parser.add_argument("--base-url", default=setting("N8N_BASE_URL", "http://127.0.0.1:5678"))
    parser.add_argument("--mock-url", default=f"http://127.0.0.1:{setting('MOCK_RECEIVER_PORT', '8000')}")
    parser.add_argument("--mailpit-url", default=f"http://127.0.0.1:{setting('MAILPIT_UI_PORT', '8025')}")
    parser.add_argument("--reset", action="store_true", help="clear mock receiver, Mailpit and alert tables first")
    parser.add_argument("--run-digest", action="store_true", help="trigger the daily digest workflow now")
    parser.add_argument("--no-scenarios", action="store_true", help="skip scenarios (e.g. with --run-digest)")
    parser.add_argument("--wait", type=int, default=180, help="seconds to wait for the webhook to be registered")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name in SCENARIO_ORDER:
            print(name)
            for step in scenarios[name]:
                print(f"   - {step.label}")
        return 0

    user, password = setting("ALERT_WEBHOOK_USER"), setting("ALERT_WEBHOOK_PASSWORD")
    if not user or not password:
        raise SystemExit("Set ALERT_WEBHOOK_USER and ALERT_WEBHOOK_PASSWORD (env or .env)")
    client = Client(args.base_url, user, password)
    wait_for_webhooks(client, [ALERT_PATH] + ([DIGEST_PATH] if args.run_digest else []), args.wait)
    if args.reset:
        reset_demo_state(args.mock_url, args.mailpit_url)

    failures = 0
    if not args.no_scenarios:
        names = args.scenario or SCENARIO_ORDER
        print(f"{'scenario':<18} {'result':<5} {'what was sent':<72} response")
        print("-" * 160)
        for name in names:
            for step in scenarios[name]:
                results = run_step(client, args.mock_url, step)
                ok, why = check(step, results)
                failures += 0 if ok else 1
                print(f"{name:<18} {'PASS' if ok else 'FAIL':<5} {step.label[:72]:<72} {describe(results)}"
                      + ("" if ok else f"   <-- {why}"))
                time.sleep(0.3)  # keep the log readable; the burst step is the only concurrent one

    if args.run_digest:
        status, body = client.post(DIGEST_PATH, {})
        print(f"\ndigest: HTTP {status} {json.dumps(body)}")
        failures += 0 if status == 200 else 1

    if not args.no_scenarios:
        print(f"\n{'all expectations met' if failures == 0 else f'{failures} step(s) did not match'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
