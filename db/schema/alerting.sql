-- Alert log, de-duplication state and delivery log for the DataOps alert router.
-- Applied to the `dataops_alerts` database by db/init/00-init.sql on first start,
-- and by tests/test_dedup_sql.py against a throwaway database.

CREATE SCHEMA IF NOT EXISTS alerting;

-- Every alert that reaches n8n, including suppressed duplicates.
CREATE TABLE IF NOT EXISTS alerting.alert_log (
    alert_id       BIGSERIAL PRIMARY KEY,
    event_id       TEXT        NOT NULL UNIQUE,      -- idempotency key derived from the source event
    fingerprint    TEXT        NOT NULL,             -- "same problem" key used for de-duplication
    received_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    occurred_at    TIMESTAMPTZ,
    source         TEXT        NOT NULL,
    event_type     TEXT        NOT NULL,
    status         TEXT        NOT NULL CHECK (status IN ('FIRING', 'RESOLVED')),
    severity       TEXT        NOT NULL CHECK (severity IN ('CRITICAL', 'HIGH', 'LOW', 'INFO')),
    pipeline       TEXT        NOT NULL,
    environment    TEXT        NOT NULL,
    decision       TEXT        NOT NULL CHECK (decision IN ('PAGE', 'NOTIFY', 'DIGEST', 'SUPPRESS', 'RESOLVE', 'LOG_ONLY')),
    occurrence     INT         NOT NULL,             -- nth event for this fingerprint in the current window
    severity_rule  TEXT,
    summary        TEXT,
    digested_at    TIMESTAMPTZ,                      -- set when a LOW alert went out in the daily digest
    alert          JSONB       NOT NULL              -- full normalised alert, including the raw payload
);

CREATE INDEX IF NOT EXISTS alert_log_fingerprint_idx ON alerting.alert_log (fingerprint, received_at DESC);
CREATE INDEX IF NOT EXISTS alert_log_digest_idx ON alerting.alert_log (alert_id) WHERE severity = 'LOW' AND digested_at IS NULL;

-- One row per fingerprint with an open notification window.
CREATE TABLE IF NOT EXISTS alerting.dedup_state (
    fingerprint        TEXT        PRIMARY KEY,
    window_started_at  TIMESTAMPTZ NOT NULL,
    last_seen_at       TIMESTAMPTZ NOT NULL,
    occurrences        INT         NOT NULL,
    max_severity       TEXT        NOT NULL,
    opened_by_alert_id BIGINT
);

-- One row per notification attempt that finished (sent, or failed after retries).
CREATE TABLE IF NOT EXISTS alerting.delivery_log (
    delivery_id   BIGSERIAL PRIMARY KEY,
    alert_id      BIGINT      NOT NULL REFERENCES alerting.alert_log (alert_id),
    channel       TEXT        NOT NULL CHECK (channel IN ('slack', 'jira', 'email')),
    status        TEXT        NOT NULL CHECK (status IN ('SENT', 'FAILED')),
    external_ref  TEXT,                               -- e.g. the Jira issue key
    detail        TEXT,
    delivered_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS delivery_log_alert_idx ON alerting.delivery_log (alert_id);

-- Written by the n8n error workflow when an execution of the router fails.
CREATE TABLE IF NOT EXISTS alerting.workflow_error (
    error_id       BIGSERIAL PRIMARY KEY,
    logged_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    workflow_id    TEXT,
    workflow_name  TEXT,
    execution_id   TEXT,
    execution_url  TEXT,
    failed_node    TEXT,
    error_message  TEXT
);

CREATE OR REPLACE FUNCTION alerting.severity_rank(p_severity TEXT)
RETURNS INT
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_severity WHEN 'CRITICAL' THEN 3 WHEN 'HIGH' THEN 2 WHEN 'LOW' THEN 1 ELSE 0 END
$$;

-- register_alert: idempotency + de-duplication + logging in one atomic call.
--
-- Input: the normalised, classified alert from n8n (needs event_id, fingerprint, status,
-- severity, route, source, event_type, pipeline, environment).
-- Output: one row with the final decision:
--   PAGE / NOTIFY / DIGEST  first alert for this fingerprint in the window (route from the rules)
--   SUPPRESS                same fingerprint inside the window and not an escalation
--   RESOLVE                 recovery for a fingerprint that had an open HIGH/CRITICAL window
--   LOG_ONLY                recovery with nothing open (or only LOW) - logged, nobody notified
--   is_replay = true        the exact same source event was already registered; nothing changes
--
-- The advisory lock serialises concurrent calls for the same fingerprint, so a burst of
-- identical alerts arriving in parallel still yields exactly one notification.
CREATE OR REPLACE FUNCTION alerting.register_alert(p_alert JSONB, p_window_minutes INT DEFAULT 15)
RETURNS TABLE (
    alert_id          BIGINT,
    decision          TEXT,
    occurrence        INT,
    is_replay         BOOLEAN,
    window_started_at TIMESTAMPTZ,
    alert             JSONB
)
LANGUAGE plpgsql AS $$
#variable_conflict use_column
DECLARE
    v_fp        TEXT := p_alert ->> 'fingerprint';
    v_event     TEXT := p_alert ->> 'event_id';
    v_status    TEXT := p_alert ->> 'status';
    v_severity  TEXT := p_alert ->> 'severity';
    v_route     TEXT := p_alert ->> 'route';
    v_now       TIMESTAMPTZ := clock_timestamp();
    v_state     alerting.dedup_state%ROWTYPE;
    v_found     BOOLEAN;
    v_decision  TEXT;
    v_occ       INT;
    v_window    TIMESTAMPTZ;
    v_id        BIGINT;
    v_existing  alerting.alert_log%ROWTYPE;
BEGIN
    IF v_fp IS NULL OR v_event IS NULL OR v_status IS NULL OR v_severity IS NULL THEN
        RAISE EXCEPTION 'register_alert: fingerprint, event_id, status and severity are required';
    END IF;
    IF p_window_minutes IS NULL OR p_window_minutes < 1 THEN
        p_window_minutes := 15;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(v_fp, 0));

    -- 1. Idempotency: a webhook retry of an event we already have.
    SELECT * INTO v_existing FROM alerting.alert_log l WHERE l.event_id = v_event;
    IF FOUND THEN
        RETURN QUERY SELECT v_existing.alert_id, v_existing.decision, v_existing.occurrence, TRUE,
                            NULL::TIMESTAMPTZ, v_existing.alert;
        RETURN;
    END IF;

    SELECT * INTO v_state FROM alerting.dedup_state s WHERE s.fingerprint = v_fp;
    v_found := FOUND;

    IF v_status = 'RESOLVED' THEN
        -- 2. Recovery closes the window, so the next failure alerts again.
        IF v_found AND alerting.severity_rank(v_state.max_severity) >= 2 THEN
            v_decision := 'RESOLVE';
        ELSE
            v_decision := 'LOG_ONLY';
        END IF;
        v_occ := 1;
        v_window := CASE WHEN v_found THEN v_state.window_started_at END;
        DELETE FROM alerting.dedup_state s WHERE s.fingerprint = v_fp;

    ELSIF v_found
          AND v_state.window_started_at > v_now - make_interval(mins => p_window_minutes)
          AND alerting.severity_rank(v_severity) <= alerting.severity_rank(v_state.max_severity) THEN
        -- 3. Repeat inside the window and not an escalation: count it, do not notify.
        v_decision := 'SUPPRESS';
        v_occ := v_state.occurrences + 1;
        v_window := v_state.window_started_at;
        UPDATE alerting.dedup_state s
           SET occurrences = v_occ, last_seen_at = v_now
         WHERE s.fingerprint = v_fp;

    ELSE
        -- 4. New problem, expired window, or escalation: open a new window and notify.
        v_decision := COALESCE(v_route, 'DIGEST');
        v_occ := 1;
        v_window := v_now;
    END IF;

    INSERT INTO alerting.alert_log (
        event_id, fingerprint, received_at, occurred_at, source, event_type, status, severity,
        pipeline, environment, decision, occurrence, severity_rule, summary, alert)
    VALUES (
        v_event, v_fp, v_now, (p_alert ->> 'occurred_at')::TIMESTAMPTZ,
        COALESCE(p_alert ->> 'source', 'unknown'), COALESCE(p_alert ->> 'event_type', 'unknown'),
        v_status, v_severity,
        COALESCE(p_alert ->> 'pipeline', 'unknown'), COALESCE(p_alert ->> 'environment', 'unknown'),
        v_decision, v_occ, p_alert ->> 'severity_rule', p_alert ->> 'summary', p_alert)
    RETURNING alert_id INTO v_id;

    IF v_decision IN ('PAGE', 'NOTIFY', 'DIGEST') THEN
        INSERT INTO alerting.dedup_state AS s
               (fingerprint, window_started_at, last_seen_at, occurrences, max_severity, opened_by_alert_id)
        VALUES (v_fp, v_now, v_now, 1, v_severity, v_id)
        ON CONFLICT (fingerprint) DO UPDATE
           SET window_started_at = EXCLUDED.window_started_at,
               last_seen_at = EXCLUDED.last_seen_at,
               occurrences = 1,
               max_severity = EXCLUDED.max_severity,
               opened_by_alert_id = EXCLUDED.opened_by_alert_id;
    END IF;

    RETURN QUERY SELECT v_id, v_decision, v_occ, FALSE, v_window, p_alert;
END;
$$;

-- Convenience view used in the demo and README queries.
CREATE OR REPLACE VIEW alerting.alert_overview AS
SELECT a.alert_id,
       to_char(a.received_at AT TIME ZONE 'UTC', 'HH24:MI:SS.MS') AS received_utc,
       a.source,
       a.pipeline,
       a.environment,
       a.event_type,
       a.status,
       a.severity,
       a.decision,
       a.occurrence,
       COALESCE(string_agg(d.channel || ':' || d.status || COALESCE('(' || d.external_ref || ')', ''),
                           ', ' ORDER BY d.delivery_id), '') AS deliveries
  FROM alerting.alert_log a
  LEFT JOIN alerting.delivery_log d ON d.alert_id = a.alert_id
 GROUP BY a.alert_id;
