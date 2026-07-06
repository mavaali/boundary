const { test } = require('node:test');
const assert = require('node:assert');
const { estimateCost, DEFAULT_RATES } = require('../lib/cost');

// Assumed transcript usage shape (validated in a later task): assistant lines carry
// message.usage {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}.
const line = (u) => ({ type: 'assistant', message: { model: 'claude-sonnet-4.6', usage: u } });

test('sums usage and prices per axis', () => {
  const r = estimateCost([
    line({ input_tokens: 1000, output_tokens: 500 }),
    line({ input_tokens: 2000, output_tokens: 100 }),
  ], DEFAULT_RATES);
  assert.strictEqual(r.in_tok, 3000);
  assert.strictEqual(r.out_tok, 600);
  assert.ok(r.dollars > 0);
});
test('absent usage degrades to unavailable', () => {
  const r = estimateCost([{ type: 'user' }], DEFAULT_RATES);
  assert.strictEqual(r.dollars, null);
});
