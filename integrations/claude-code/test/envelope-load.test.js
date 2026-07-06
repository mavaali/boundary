const { test } = require('node:test');
const assert = require('node:assert');
const { loadEnvelope, DEFAULTS } = require('../lib/envelope');

test('absent config yields defaults', () => {
  const env = loadEnvelope(null);
  assert.strictEqual(env.require_staging, DEFAULTS.require_staging);
  assert.strictEqual(env.max_writes, DEFAULTS.max_writes);
  assert.ok(Array.isArray(env.writable_paths));
});
test('partial config overrides only named keys', () => {
  const env = loadEnvelope({ max_writes: 2, writable_paths: ['out/*.md'] });
  assert.strictEqual(env.max_writes, 2);
  assert.deepStrictEqual(env.writable_paths, ['out/*.md']);
  assert.strictEqual(env.min_writes, DEFAULTS.min_writes); // untouched
});
