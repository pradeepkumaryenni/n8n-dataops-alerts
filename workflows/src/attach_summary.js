// Attach Summary (n8n Code node, "Run Once for All Items")
//
// The "LLM Summary (optional)" node before this one is disabled by default, so n8n passes the
// classified alert straight through and the rule-based summary is kept. If the LLM node is enabled
// and returns an OpenAI-style chat completion, its text replaces the summary. If the LLM call fails
// (the node is set to continue on error) the rule-based summary is kept: the LLM is advisory and is
// never on the critical path of an alert.

const classified = $('Classify Severity').all();

function llmText(json) {
  const choice = json && Array.isArray(json.choices) ? json.choices[0] : null;
  const text = choice && choice.message ? choice.message.content : null;
  return typeof text === 'string' && text.trim() ? text.trim().slice(0, 600) : null;
}

return $input.all().map((item, index) => {
  const alert = classified[index] ? classified[index].json : item.json;
  const text = llmText(item.json);
  return {
    json: {
      ...alert,
      summary: text || alert.summary,
      summary_source: text ? 'llm' : 'rules',
    },
  };
});
