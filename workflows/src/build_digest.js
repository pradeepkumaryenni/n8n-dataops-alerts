// Build Digest (n8n Code node, "Run Once for All Items")
//
// Input: one row from the "Fetch Pending LOW Alerts" query:
//   {alert_count, group_count, cutoff_alert_id, groups: [{fingerprint, pipeline, environment, source,
//    event_type, events, first_seen, last_seen, latest_summary}]}
// Output: the same row plus the digest email subject, HTML and plain-text bodies.

function escapeHtml(value) {
  return String(value === undefined || value === null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

function buildDigest(row) {
  const groups = Array.isArray(row.groups) ? row.groups : [];
  const alertCount = Number(row.alert_count || 0);
  const subject = `DataOps daily digest: ${plural(alertCount, 'low-severity alert')} across ${plural(groups.length, 'pipeline')}`;
  const header = ['Pipeline', 'Env', 'Source', 'Type', 'Events', 'First seen (UTC)', 'Last seen (UTC)', 'Latest summary'];
  const lines = groups.map((g) => [
    g.pipeline,
    g.environment,
    g.source,
    g.event_type,
    g.events,
    String(g.first_seen || '').slice(0, 19).replace('T', ' '),
    String(g.last_seen || '').slice(0, 19).replace('T', ' '),
    g.latest_summary,
  ]);
  const html = [
    `<h2>${escapeHtml(subject)}</h2>`,
    '<p>LOW alerts are not sent one by one. They are collected here once a day.</p>',
    '<table border="1" cellpadding="4" style="border-collapse:collapse">',
    `<tr>${header.map((h) => `<th align="left">${h}</th>`).join('')}</tr>`,
    ...lines.map((cells) => `<tr>${cells.map((c) => `<td>${escapeHtml(c)}</td>`).join('')}</tr>`),
    '</table>',
  ].join('\n');
  const text = [subject, '', ...lines.map((c) => `- ${c[0]} [${c[1]}] ${c[3]} x${c[4]}: ${c[7]}`)].join('\n');
  return { ...row, alert_count: alertCount, subject, html, text };
}

return $input.all().map((item) => ({ json: buildDigest(item.json) }));
