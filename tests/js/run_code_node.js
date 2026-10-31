// Runs the jsCode of an n8n Code node outside n8n, with a minimal $input / $('Node') shim, so the
// exact code that n8n executes can be unit-tested.
//
// stdin : {"code": "...", "items": [<json>, ...], "refs": {"Node Name": [<json>, ...]}}
// stdout: {"ok": true, "items": [<json>, ...]}  or  {"ok": false, "error": "message"}
'use strict';

const chunks = [];
process.stdin.on('data', (chunk) => chunks.push(chunk));
process.stdin.on('end', () => {
  const { code, items, refs } = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const wrap = (list) => list.map((json) => ({ json }));
  const accessor = (list) => ({ all: () => list, first: () => list[0], item: list[0] });
  const $input = accessor(wrap(items));
  const $ = (name) => {
    if (!refs || !Object.prototype.hasOwnProperty.call(refs, name)) {
      throw new Error(`test harness: no data for node reference $('${name}')`);
    }
    return accessor(wrap(refs[name]));
  };
  try {
    // eslint-disable-next-line no-new-func
    const run = new Function('$input', '$', code);
    const out = run($input, $);
    process.stdout.write(JSON.stringify({ ok: true, items: out.map((item) => item.json) }));
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: String((err && err.message) || err) }));
  }
});
