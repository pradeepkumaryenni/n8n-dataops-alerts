-- Runs once, on the first start of the Postgres container (empty data volume).
-- POSTGRES_DB (n8n's own database) is created by the image; this adds the alert-log database.
CREATE DATABASE dataops_alerts;
\connect dataops_alerts
\i /schema/alerting.sql
