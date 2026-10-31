// Classify Severity (n8n Code node, "Run Once for All Items")
//
// Ordered rules, first match wins. Each alert gets a severity, the id and reason of the rule that
// matched (so every routing decision can be explained), a route and a rule-based summary.
//
//   severity  -> route
//   CRITICAL  -> PAGE    Slack + Jira + email to on-call
//   HIGH      -> NOTIFY  Slack + Jira
//   LOW       -> DIGEST  daily digest email
//   INFO      -> RESOLVE recovery events (Slack "resolved" if an alert was open)

// Business tier per pipeline / job (1 = revenue or regulatory impact). Unlisted pipelines are tier 3.
// In production this would be a config table owned by the data platform team.
const PIPELINE_TIERS = {
  nightly_sales_etl: 1,
  pl_load_finance_gl: 1,
  'gold.sales_daily': 1,
  pl_ingest_crm_accounts: 2,
  customer_360_refresh: 2,
  pl_refresh_marketing_lookup: 3,
};

const NON_PROD = new Set(['dev', 'test', 'qa', 'uat', 'staging', 'sandbox']);

const ROUTE_BY_SEVERITY = { CRITICAL: 'PAGE', HIGH: 'NOTIFY', LOW: 'DIGEST', INFO: 'RESOLVE' };

const RULES = [
  {
    id: 'R1_recovery',
    when: (a) => a.status === 'RESOLVED',
    severity: 'INFO',
    reason: 'Recovery event, closes any open alert for the same pipeline',
  },
  {
    id: 'R2_non_prod',
    when: (a) => NON_PROD.has(a.environment),
    severity: 'LOW',
    reason: 'Non-production environment',
  },
  {
    id: 'R3_tier1_failure',
    when: (a) => a.event_type === 'failure' && a.tier === 1,
    severity: 'CRITICAL',
    reason: 'Tier-1 pipeline failed in production',
  },
  {
    id: 'R4_upstream_sev0_sev1',
    when: (a) => a.event_type !== 'info' && ['Sev0', 'Sev1'].includes(a.upstream_severity),
    severity: 'CRITICAL',
    reason: 'Azure Monitor alert rule is Sev0/Sev1',
  },
  {
    id: 'R5_prod_failure',
    when: (a) => a.event_type === 'failure',
    severity: 'HIGH',
    reason: 'Production pipeline failure',
  },
  {
    id: 'R6_sla_breach_tier1_2',
    when: (a) => a.event_type === 'sla_breach' && a.tier <= 2,
    severity: 'HIGH',
    reason: 'SLA breach on a tier-1/2 pipeline',
  },
  {
    id: 'R7_default_low',
    when: () => true,
    severity: 'LOW',
    reason: 'Low-impact or informational event',
  },
];

function describe(alert) {
  if (alert.status === 'RESOLVED') return 'recovered';
  if (alert.event_type === 'failure') return 'failed';
  if (alert.event_type === 'sla_breach') return 'breached its SLA';
  return 'reported an event';
}

function classify(alert) {
  const tier = Object.prototype.hasOwnProperty.call(PIPELINE_TIERS, alert.pipeline) ? PIPELINE_TIERS[alert.pipeline] : 3;
  const ctx = { ...alert, tier };
  const rule = RULES.find((r) => r.when(ctx));
  const run = alert.run_id ? ` (run ${alert.run_id})` : '';
  return {
    ...alert,
    tier,
    severity: rule.severity,
    severity_rule: rule.id,
    severity_reason: rule.reason,
    route: ROUTE_BY_SEVERITY[rule.severity],
    summary: `[${rule.severity}] ${alert.source_label} ${alert.pipeline} ${describe(alert)} in ${alert.environment}${run}. ${rule.reason}.`,
    summary_source: 'rules',
  };
}

return $input.all().map((item) => ({ json: classify(item.json) }));
