// Normalise Payload (n8n Code node, "Run Once for All Items")
//
// Maps the inbound shapes this router understands onto one canonical alert record:
//   1. Databricks job notification webhook  {event_type: "jobs.on_failure", workspace_id, run: {run_id}, job: {job_id, name}}
//   2. Azure Monitor common alert schema    {schemaId: "azureMonitorCommonAlertSchema", data: {essentials, alertContext}}
//      (this is what an Azure Data Factory metric alert such as PipelineFailedRuns sends to a webhook)
//   3. Generic JSON from in-house checks    {source, event_type, status, pipeline, environment, message, event_id}
// Any other JSON object is kept as source "unknown" (never silently dropped): it is logged and lands
// in the LOW digest. A body that is not a JSON object throws, which fails the execution and fires the
// error workflow.

// Databricks webhooks carry a workspace id, not an environment name. In production this lookup would
// live in a config table; the ids below are the sample ids used by the simulator.
const DATABRICKS_WORKSPACES = {
  '1234567890123456': { environment: 'prod', host: 'https://adb-1234567890123456.7.azuredatabricks.net' },
  '2345678901234567': { environment: 'dev', host: 'https://adb-2345678901234567.7.azuredatabricks.net' },
};

const DATABRICKS_EVENTS = {
  'jobs.on_failure': { event_type: 'failure', status: 'FIRING', detail: 'Job run failed' },
  'jobs.on_duration_warning_threshold_exceeded': {
    event_type: 'sla_breach',
    status: 'FIRING',
    detail: 'Run exceeded the job duration warning threshold',
  },
  'jobs.on_streaming_backlog_exceeded': {
    event_type: 'sla_breach',
    status: 'FIRING',
    detail: 'Streaming backlog exceeded the configured threshold',
  },
  // A successful run resolves an earlier failure for the same job.
  'jobs.on_success': { event_type: 'failure', status: 'RESOLVED', detail: 'Job run succeeded' },
  'jobs.on_start': { event_type: 'info', status: 'FIRING', detail: 'Job run started' },
};

// Azure Data Factory metric names -> canonical event type.
const AZURE_METRIC_EVENTS = {
  PipelineFailedRuns: 'failure',
  ActivityFailedRuns: 'failure',
  TriggerFailedRuns: 'failure',
  PipelineElapsedTimeRuns: 'sla_breach',
};

const ENV_ALIASES = { prd: 'prod', production: 'prod', development: 'dev', stg: 'staging' };

function clean(value, fallback = 'unknown') {
  const text = value === undefined || value === null ? '' : String(value).trim();
  return text || fallback;
}

function envFromName(name) {
  const match = /(?:^|[-_.])(prod|prd|production|dev|development|test|qa|uat|stg|staging|sandbox)(?:$|[-_.])/i.exec(
    name || '',
  );
  if (!match) return null;
  const env = match[1].toLowerCase();
  return ENV_ALIASES[env] || env;
}

function normaliseEnv(value) {
  const env = clean(value).toLowerCase();
  return ENV_ALIASES[env] || env;
}

function isoOrNull(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

// Small, dependency-free FNV-1a hash, used only to derive an id when a payload has none.
function fnv1a(text) {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

function fromDatabricks(body) {
  const mapped = DATABRICKS_EVENTS[body.event_type] || { event_type: 'info', status: 'FIRING', detail: body.event_type };
  const job = body.job || {};
  const run = body.run || {};
  const workspaceId = clean(body.workspace_id);
  const workspace = DATABRICKS_WORKSPACES[workspaceId] || {};
  const runId = run.run_id === undefined || run.run_id === null ? null : String(run.run_id);
  const jobId = clean(job.job_id);
  return {
    source: 'databricks',
    source_label: 'Databricks job',
    source_event: body.event_type,
    // Databricks sends no event id; (workspace, job, run, event type) identifies one notification.
    event_id: `databricks:${workspaceId}:${jobId}:${runId || 'na'}:${body.event_type}`,
    event_type: mapped.event_type,
    status: mapped.status,
    pipeline: clean(job.name),
    environment: workspace.environment || envFromName(job.name) || 'unknown',
    run_id: runId,
    run_url: workspace.host && runId ? `${workspace.host}/jobs/${jobId}/runs/${runId}` : null,
    // The webhook does not include the error text; production would enrich it via the Jobs API (runs/get).
    error_message: mapped.detail,
    upstream_severity: null,
    occurred_at: null,
  };
}

function fromAzureMonitor(body) {
  const essentials = body.data.essentials || {};
  const context = body.data.alertContext || {};
  const condition = (context.condition && Array.isArray(context.condition.allOf) && context.condition.allOf[0]) || {};
  const dims = {};
  for (const d of condition.dimensions || []) dims[d.name] = d.value;
  const resourceId = (essentials.alertTargetIDs || [])[0] || '';
  const resourceName = clean(resourceId.split('/').pop() || (essentials.configurationItems || [])[0]);
  const metric = condition.metricName || null;
  const isDataFactory = /microsoft\.datafactory/i.test(resourceId) || /DataFactory/i.test(condition.metricNamespace || '');
  const status = essentials.monitorCondition === 'Resolved' ? 'RESOLVED' : 'FIRING';
  const custom = body.data.customProperties || {};
  let detail = essentials.description || null;
  if (!detail && metric) {
    detail = `${metric} ${condition.operator || ''} ${condition.threshold ?? ''}`.replace(/\s+/g, ' ').trim();
    if (condition.metricValue !== undefined) detail += ` (observed ${condition.metricValue})`;
    if (dims.FailureType) detail += `, FailureType=${dims.FailureType}`;
  }
  return {
    source: 'azure_monitor',
    source_label: isDataFactory ? 'ADF pipeline' : 'Azure resource',
    source_event: `${essentials.signalType || 'Unknown'}/${metric || essentials.alertRule || 'alert'}`,
    // Fired and Resolved notifications share alertId, so the status is part of the idempotency key.
    event_id: `azure:${essentials.alertId || fnv1a(JSON.stringify(body))}:${status}`,
    event_type: AZURE_METRIC_EVENTS[metric] || (/fail/i.test(essentials.alertRule || '') ? 'failure' : 'info'),
    status,
    pipeline: clean(dims.Name || dims.PipelineName || essentials.alertRule),
    environment: normaliseEnv(custom.environment || envFromName(resourceName) || 'unknown'),
    run_id: dims.RunId || null,
    run_url: resourceId ? `https://portal.azure.com/#resource${resourceId}/overview` : null,
    error_message: detail,
    upstream_severity: essentials.severity || null,
    occurred_at: isoOrNull(status === 'RESOLVED' ? essentials.resolvedDateTime : essentials.firedDateTime),
    resource: resourceName,
    alert_rule: essentials.alertRule || null,
  };
}

function fromGeneric(body) {
  const source = clean(body.source).toLowerCase().replace(/[^a-z0-9_.-]+/g, '_');
  const status = String(body.status || 'FIRING').toUpperCase() === 'RESOLVED' ? 'RESOLVED' : 'FIRING';
  const eventType = ['failure', 'sla_breach'].includes(body.event_type) ? body.event_type : 'info';
  return {
    source,
    source_label: clean(body.source_label, 'Pipeline'),
    source_event: body.event_type || null,
    event_id: `${source}:${body.event_id ? String(body.event_id) : fnv1a(JSON.stringify(body))}`,
    event_type: eventType,
    status,
    pipeline: clean(body.pipeline),
    environment: normaliseEnv(body.environment || envFromName(body.pipeline) || 'unknown'),
    run_id: body.run_id === undefined || body.run_id === null ? null : String(body.run_id),
    run_url: body.run_url || null,
    error_message: body.message || body.error || null,
    upstream_severity: body.severity || null,
    occurred_at: isoOrNull(body.occurred_at),
  };
}

function fromUnknown(body) {
  const text = JSON.stringify(body);
  return {
    source: 'unknown',
    source_label: 'Unrecognised sender',
    source_event: null,
    event_id: `unknown:${fnv1a(text)}`,
    event_type: 'info',
    status: 'FIRING',
    pipeline: 'unknown',
    environment: 'unknown',
    run_id: null,
    run_url: null,
    error_message: `Unrecognised payload: ${text.slice(0, 300)}`,
    upstream_severity: null,
    occurred_at: null,
  };
}

function normalise(body, receivedAt) {
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('Unsupported payload: expected a JSON object in the request body');
  }
  let alert;
  if (typeof body.event_type === 'string' && body.event_type.startsWith('jobs.') && body.job) {
    alert = fromDatabricks(body);
  } else if (body.schemaId === 'azureMonitorCommonAlertSchema' && body.data && body.data.essentials) {
    alert = fromAzureMonitor(body);
  } else if (body.source && body.pipeline) {
    alert = fromGeneric(body);
  } else {
    alert = fromUnknown(body);
  }
  alert.fingerprint = [alert.source, alert.environment, alert.pipeline, alert.event_type].join(':').toLowerCase();
  alert.received_at = receivedAt;
  alert.raw = body;
  return alert;
}

const receivedAt = new Date().toISOString();
return $input.all().map((item) => ({ json: normalise(item.json.body, receivedAt) }));
