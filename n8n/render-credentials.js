// Builds the n8n credentials import file from environment variables at container start.
// Nothing secret is stored in the repository: values come from .env / the compose environment,
// and the generated file only lives in the import container's /tmp until it is imported
// (n8n encrypts credential data with N8N_ENCRYPTION_KEY on import).
'use strict';

const env = (name, fallback = '') => {
  const value = process.env[name];
  return value === undefined || value === '' ? fallback : value;
};

const credentials = [
  {
    id: 'dataopsPostgres1',
    name: 'DataOps alert log (Postgres)',
    type: 'postgres',
    data: {
      host: env('ALERT_DB_HOST', 'postgres'),
      port: Number(env('ALERT_DB_PORT', '5432')),
      database: env('ALERT_DB_NAME', 'dataops_alerts'),
      user: env('ALERT_DB_USER'),
      password: env('ALERT_DB_PASSWORD'),
      ssl: env('ALERT_DB_SSL', 'disable'),
      allowUnauthorizedCerts: false,
      maxConnections: 10,
    },
  },
  {
    id: 'dataopsSmtpMail1',
    name: 'SMTP (alert email)',
    type: 'smtp',
    data: {
      host: env('SMTP_HOST', 'mailpit'),
      port: Number(env('SMTP_PORT', '1025')),
      secure: env('SMTP_SECURE', 'false') === 'true',
      disableStartTls: env('SMTP_DISABLE_STARTTLS', 'true') === 'true',
      user: env('SMTP_USER'),
      password: env('SMTP_PASSWORD'),
    },
  },
  {
    id: 'dataopsJiraAuth1',
    name: 'Jira API (basic auth)',
    type: 'httpBasicAuth',
    data: { user: env('JIRA_USER_EMAIL'), password: env('JIRA_API_TOKEN') },
  },
  {
    id: 'dataopsHookAuth1',
    name: 'Alert webhook (basic auth)',
    type: 'httpBasicAuth',
    data: { user: env('ALERT_WEBHOOK_USER'), password: env('ALERT_WEBHOOK_PASSWORD') },
  },
  {
    // Only used by the optional, disabled-by-default "LLM Summary" node. Empty unless LLM_API_KEY is set.
    id: 'dataopsLlmKey001',
    name: 'LLM API key (optional)',
    type: 'httpHeaderAuth',
    data: { name: 'Authorization', value: env('LLM_API_KEY') ? `Bearer ${env('LLM_API_KEY')}` : '' },
  },
];

const missing = [
  ['ALERT_DB_USER', credentials[0].data.user],
  ['ALERT_DB_PASSWORD', credentials[0].data.password],
  ['ALERT_WEBHOOK_USER', credentials[3].data.user],
  ['ALERT_WEBHOOK_PASSWORD', credentials[3].data.password],
].filter(([, value]) => !value).map(([name]) => name);

if (missing.length) {
  console.error(`render-credentials: missing required env vars: ${missing.join(', ')}`);
  process.exit(1);
}

process.stdout.write(JSON.stringify(credentials, null, 2));
