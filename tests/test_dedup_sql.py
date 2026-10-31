"""Tests for alerting.register_alert() (idempotency + de-duplication) against a real Postgres.

Skipped unless TEST_DATABASE_URL points at a Postgres server where the user may create databases,
e.g. the compose one:  TEST_DATABASE_URL=postgresql://dataops:<password>@127.0.0.1:5433/postgres
CI runs them against a Postgres service container. Each test session uses a throwaway database.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import Counter

import pytest

from conftest import ROOT

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run the SQL tests")

SCHEMA_SQL = (ROOT / "db" / "schema" / "alerting.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def db_url():
    name = f"alerting_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = psycopg.conninfo.make_conninfo(DSN, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(SCHEMA_SQL)
        yield url
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def conn(db_url):
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("TRUNCATE alerting.delivery_log, alerting.alert_log, alerting.dedup_state RESTART IDENTITY CASCADE")
        yield c


def alert(fingerprint="databricks:prod:nightly_sales_etl:failure", severity="CRITICAL", route="PAGE",
          status="FIRING", event_id=None):
    return {
        "event_id": event_id or f"evt-{uuid.uuid4()}",
        "fingerprint": fingerprint,
        "status": status,
        "severity": severity,
        "route": route,
        "source": fingerprint.split(":")[0],
        "environment": fingerprint.split(":")[1],
        "pipeline": fingerprint.split(":")[2],
        "event_type": fingerprint.split(":")[3],
        "severity_rule": "test",
        "summary": "test alert",
        "occurred_at": None,
    }


def register(conn, payload, window=15):
    row = conn.execute("SELECT alert_id, decision, occurrence, is_replay FROM alerting.register_alert(%s::jsonb, %s)",
                       (json.dumps(payload), window)).fetchone()
    return {"alert_id": row[0], "decision": row[1], "occurrence": row[2], "is_replay": row[3]}


def count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def test_first_alert_takes_the_route_from_the_rules(conn):
    assert register(conn, alert())["decision"] == "PAGE"
    assert register(conn, alert(fingerprint="a:prod:p:failure", severity="HIGH", route="NOTIFY"))["decision"] == "NOTIFY"
    assert register(conn, alert(fingerprint="b:dev:p:failure", severity="LOW", route="DIGEST"))["decision"] == "DIGEST"


def test_repeat_inside_window_is_suppressed_and_counted(conn):
    first = register(conn, alert())
    second = register(conn, alert())
    third = register(conn, alert())
    assert first["decision"] == "PAGE"
    assert (second["decision"], second["occurrence"]) == ("SUPPRESS", 2)
    assert (third["decision"], third["occurrence"]) == ("SUPPRESS", 3)
    assert count(conn, "SELECT count(*) FROM alerting.alert_log") == 3, "suppressed alerts are still logged"


def test_same_event_twice_is_a_replay_with_no_side_effects(conn):
    payload = alert(event_id="azure:/subscriptions/x/alerts/1:FIRING")
    first = register(conn, payload)
    again = register(conn, payload)
    assert again == {**first, "is_replay": True}
    assert count(conn, "SELECT count(*) FROM alerting.alert_log") == 1
    assert count(conn, "SELECT occurrences FROM alerting.dedup_state") == 1


def test_escalation_inside_window_notifies_again(conn):
    fp = "azure_monitor:prod:pl_x:failure"
    assert register(conn, alert(fingerprint=fp, severity="HIGH", route="NOTIFY"))["decision"] == "NOTIFY"
    assert register(conn, alert(fingerprint=fp, severity="CRITICAL", route="PAGE"))["decision"] == "PAGE"
    assert register(conn, alert(fingerprint=fp, severity="HIGH", route="NOTIFY"))["decision"] == "SUPPRESS"


def test_window_expiry_reopens(conn):
    assert register(conn, alert())["decision"] == "PAGE"
    conn.execute("UPDATE alerting.dedup_state SET window_started_at = window_started_at - interval '16 minutes'")
    assert register(conn, alert(), window=15)["decision"] == "PAGE"
    assert register(conn, alert(), window=15)["decision"] == "SUPPRESS"


def test_recovery_resolves_an_open_alert_and_closes_the_window(conn):
    assert register(conn, alert())["decision"] == "PAGE"
    resolved = register(conn, alert(status="RESOLVED", severity="INFO", route="RESOLVE"))
    assert resolved["decision"] == "RESOLVE"
    assert count(conn, "SELECT count(*) FROM alerting.dedup_state") == 0
    assert register(conn, alert())["decision"] == "PAGE", "a new failure after recovery alerts again"


def test_recovery_with_nothing_open_is_log_only(conn):
    assert register(conn, alert(status="RESOLVED", severity="INFO", route="RESOLVE"))["decision"] == "LOG_ONLY"


def test_recovery_of_a_low_alert_is_log_only(conn):
    fp = "databricks:dev:nightly_sales_etl:failure"
    assert register(conn, alert(fingerprint=fp, severity="LOW", route="DIGEST"))["decision"] == "DIGEST"
    assert register(conn, alert(fingerprint=fp, status="RESOLVED", severity="INFO", route="RESOLVE"))["decision"] == "LOG_ONLY"


def test_missing_required_fields_raise(conn):
    payload = alert()
    del payload["fingerprint"]
    with pytest.raises(psycopg.errors.RaiseException):
        register(conn, payload)


def test_concurrent_burst_yields_exactly_one_notification(db_url, conn):
    """20 different events for the same problem arrive at the same moment."""
    n = 20
    barrier = threading.Barrier(n)
    decisions: list[str] = []
    lock = threading.Lock()

    def worker():
        with psycopg.connect(db_url, autocommit=True) as c:
            barrier.wait()
            d = register(c, alert(fingerprint="databricks:prod:customer_360_refresh:failure",
                                  severity="HIGH", route="NOTIFY"))["decision"]
        with lock:
            decisions.append(d)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Counter(decisions) == {"NOTIFY": 1, "SUPPRESS": n - 1}
    occurrences = [r[0] for r in conn.execute("SELECT occurrence FROM alerting.alert_log ORDER BY occurrence")]
    assert occurrences == list(range(1, n + 1)), "every event got a distinct occurrence number"


def test_concurrent_replays_insert_once(db_url, conn):
    payload = alert(event_id="databricks:1:2:3:jobs.on_failure")
    n = 10
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker():
        with psycopg.connect(db_url, autocommit=True) as c:
            barrier.wait()
            r = register(c, payload)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if not r["is_replay"]) == 1
    assert count(conn, "SELECT count(*) FROM alerting.alert_log") == 1


def test_overview_view_joins_deliveries(conn):
    first = register(conn, alert())
    conn.execute("INSERT INTO alerting.delivery_log (alert_id, channel, status, external_ref) VALUES (%s, 'jira', 'SENT', 'DATA-1')",
                 (first["alert_id"],))
    row = conn.execute("SELECT decision, deliveries FROM alerting.alert_overview WHERE alert_id = %s",
                       (first["alert_id"],)).fetchone()
    assert row == ("PAGE", "jira:SENT(DATA-1)")
