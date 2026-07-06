const { test } = require('node:test');
const assert = require('node:assert');
const { grade } = require('../lib/grade');

const evs = (...e) => e;
test('clean staged run with a write passes', () => {
  const v = grade(evs(
    { kind: 'envelope_start' }, { kind: 'staged' }, { kind: 'write_allowed' }, { kind: 'envelope_end' }
  ), { min_writes: 1, require_staging: true }, { writes_executed: 1 });
  assert.strictEqual(v.schema, 'boundary.third-umpire/v1');
  assert.strictEqual(v.verdict, 'PASS');
});
test('write_refused fails writes_inside_allowlist and verdict', () => {
  const v = grade(evs({ kind: 'write_refused', tool: 'Write', detail: 'x' }), { min_writes: 1 }, { writes_executed: 0 });
  assert.strictEqual(v.verdict, 'FAIL');
  const c = v.checks.find((x) => x.name === 'writes_inside_allowlist');
  assert.strictEqual(c.passed, false);
});
test('under min_writes fails produced_output', () => {
  const v = grade(evs({ kind: 'staged' }), { min_writes: 2, require_staging: true }, { writes_executed: 1 });
  const c = v.checks.find((x) => x.name === 'produced_output');
  assert.strictEqual(c.passed, false);
});
test('missing staged event fails staging_pivot', () => {
  const v = grade(evs({ kind: 'write_allowed' }), { min_writes: 1, require_staging: true, writable_paths: ['out/**'] }, { writes_executed: 1 });
  const c = v.checks.find((x) => x.name === 'staging_pivot');
  assert.strictEqual(c.passed, false);
});
test('require_staging with empty writable_paths has no staging_pivot check (matches decide()\'s stagingOn)', () => {
  const v = grade(evs({ kind: 'write_allowed' }), { min_writes: 1, require_staging: true, writable_paths: [] }, { writes_executed: 1 });
  const c = v.checks.find((x) => x.name === 'staging_pivot');
  assert.strictEqual(c, undefined);
});
