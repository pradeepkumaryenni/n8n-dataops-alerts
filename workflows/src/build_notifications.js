// Build Notifications (n8n Code node, "Run Once for All Items")
//
// Input: the row returned by alerting.register_alert() (alert_id, decision, occurrence, is_replay,
// window_started_at, alert). Output: the alert plus ready-to-send Slack, Jira and email payloads and
// the `action` the router switches on. Environment-specific values (Jira project key, issue type,
// addresses) are added in the sending nodes from env vars, so this code stays pure and testable.

const EMOJI = { CRITICAL: ':rotating_light:', HIGH: ':warning:', LOW: ':information_source:', INFO: ':white_check_mark:' };
const NOTIFYING = new Set(['PAGE', 'NOTIFY', 'RESOLVE', 'DIGEST']);

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function verb(a) {
  if (a.status === 'RESOLVED') return 'recovered';
  if (a.event_type === 'failure') return 'failed';
  if (a.event_type === 'sla_breach') return 'breached SLA';
  return 'event';
}

function slackMessage(a) {
  const resolved = a.decision === 'RESOLVE';
  const title = resolved
    ? `RESOLVED: ${a.pipeline} (${a.environment})`
    : `${a.severity}: ${a.pipeline} ${verb(a)} (${a.environment})`;
  const run = a.run_url ? `<${a.run_url}|${a.run_id || 'open'}>` : a.run_id || '-';
  return {
    text: `${EMOJI[resolved ? 'INFO' : a.severity] || ''} ${title}`.trim(),
    blocks: [
      { type: 'header', text: { type: 'plain_text', text: title.slice(0, 150) } },
      {
        type: 'section',
        fields: [
          { type: 'mrkdwn', text: `*Source*\n${a.source_label}` },
          { type: 'mrkdwn', text: `*Environment*\n${a.environment}` },
          { type: 'mrkdwn', text: `*Run*\n${run}` },
          { type: 'mrkdwn', text: `*Alert id*\n${a.alert_id}` },
        ],
      },
      {
        type: 'section',
        text: {
          type: 'mrkdwn',
          text: `*Summary:* ${a.summary}\n*Detail:* ${a.error_message || '-'}\n*Rule:* ${a.severity_rule}`,
        },
      },
      { type: 'context', elements: [{ type: 'mrkdwn', text: `fingerprint \`${a.fingerprint}\`` }] },
    ],
  };
}

function jiraFields(a) {
  const description = [
    a.summary,
    '',
    `*Source:* ${a.source_label} (${a.source_event || a.source})`,
    `*Pipeline:* ${a.pipeline}`,
    `*Environment:* ${a.environment}`,
    `*Run:* ${a.run_id || '-'} ${a.run_url || ''}`.trim(),
    `*Detail:* ${a.error_message || '-'}`,
    `*Severity rule:* ${a.severity_rule} - ${a.severity_reason}`,
    `*Fingerprint:* ${a.fingerprint}`,
    `*Alert id:* ${a.alert_id}`,
  ].join('\n');
  return {
    summary: `[${a.severity}] ${a.pipeline} ${verb(a)} (${a.environment})`.slice(0, 250),
    description,
    priority: { name: a.severity === 'CRITICAL' ? 'Highest' : 'High' },
    labels: ['dataops-alert', `severity-${String(a.severity).toLowerCase()}`, `source-${a.source}`],
  };
}

function email(a) {
  const runCell = a.run_url
    ? `<a href="${escapeHtml(a.run_url)}">${escapeHtml(a.run_id || a.run_url)}</a>`
    : escapeHtml(a.run_id || '-');
  const rows = [
    ['Severity', escapeHtml(`${a.severity} (rule ${a.severity_rule})`), `${a.severity} (rule ${a.severity_rule})`],
    ['Why', escapeHtml(a.severity_reason), a.severity_reason],
    ['Source', escapeHtml(`${a.source_label} - ${a.source_event || a.source}`), `${a.source_label} - ${a.source_event || a.source}`],
    ['Pipeline', escapeHtml(a.pipeline), a.pipeline],
    ['Environment', escapeHtml(a.environment), a.environment],
    ['Run', runCell, `${a.run_id || '-'} ${a.run_url || ''}`.trim()],
    ['Detail', escapeHtml(a.error_message || '-'), a.error_message || '-'],
    ['Fingerprint', escapeHtml(a.fingerprint), a.fingerprint],
    ['Alert id', escapeHtml(a.alert_id), String(a.alert_id)],
  ];
  const html = [
    `<h2>${escapeHtml(a.summary)}</h2>`,
    '<table cellpadding="4" style="border-collapse:collapse">',
    ...rows.map(([label, htmlValue], i) => `<tr${i % 2 ? '' : ' style="background:#f4f4f4"'}><th align="left">${label}</th><td>${htmlValue}</td></tr>`),
    '</table>',
    '<p>Sent by the n8n DataOps alert router. Repeats of this alert are suppressed for the de-duplication window.</p>',
  ].join('\n');
  const text = [a.summary, '', ...rows.map(([label, , plain]) => `${label}: ${plain}`)].join('\n');
  return { subject: `[${a.severity}] ${a.pipeline} ${verb(a)} in ${a.environment}`, html, text };
}

function build(row) {
  const a = {
    ...row.alert,
    alert_id: Number(row.alert_id),
    decision: row.decision,
    occurrence: Number(row.occurrence),
    is_replay: row.is_replay === true || row.is_replay === 'true',
  };
  a.action = !a.is_replay && NOTIFYING.has(a.decision) ? a.decision : 'NONE';
  a.slack = slackMessage(a);
  a.jira = { fields: jiraFields(a) };
  a.email = email(a);
  return a;
}

return $input.all().map((item) => ({ json: build(item.json) }));
