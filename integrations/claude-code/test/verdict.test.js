const { test } = require('node:test');
const assert = require('node:assert');
const { handle } = require('../scripts/verdict');

function fakeIo(over = {}) {
  const _events = [];
  const base = {
    readEnvelope: () => ({ writable_paths: ['out/**'], min_writes: 1, require_staging: true }),
    readEvents: () => [{ kind: 'staged' }, { kind: 'write_allowed', tool: 'Write' }],
    readState: () => ({ writes_executed: 1, unstaged_reads: 0 }),
    appendEvent: (sid, e) => _events.push(e),
    readTranscript: () => [{ type: 'assistant', message: { model: 'claude-sonnet-4.6', usage: { input_tokens: 1000, output_tokens: 200 } } }],
    hasEngine: () => false,
    runEngine: () => null,
    writeVerdict: (cwd, v) => { fakeIo._written = v; },
  };
  return Object.assign(base, over, { _events });
}

test('produces a v1 verdict with the right overall verdict', () => {
  const v = handle({ session_id: 's', cwd: '/x', transcript_path: '/t' }, fakeIo());
  assert.strictEqual(v.schema, 'boundary.third-umpire/v1');
  assert.strictEqual(v.verdict, 'PASS');
  assert.strictEqual(fakeIo({})._events === undefined ? false : true, true); // sanity
});

test('cost estimate populates summary.estimated_dollars when transcript has usage', () => {
  const v = handle({ session_id: 's', cwd: '/x', transcript_path: '/t' }, fakeIo());
  assert.strictEqual(typeof v.summary.estimated_dollars, 'number');
});

test('garbled/missing transcript degrades to "unavailable", no throw', () => {
  const v = handle({ session_id: 's', cwd: '/x', transcript_path: '/t' }, fakeIo({
    readTranscript: () => { throw new Error('bad transcript'); },
  }));
  assert.strictEqual(v.summary.estimated_dollars, 'unavailable');
});

test('engine upgrade attached only when hasEngine', () => {
  const withEngine = handle({ session_id: 's', cwd: '/x', transcript_path: '/t' }, fakeIo({
    hasEngine: () => true,
    runEngine: () => ({ schema: 'boundary.third-umpire/v1', verdict: 'PASS', checks: [] }),
  }));
  assert.ok(withEngine.engine && withEngine.engine.verdict === 'PASS');
  const noEngine = handle({ session_id: 's', cwd: '/x', transcript_path: '/t' }, fakeIo());
  assert.strictEqual(noEngine.engine, undefined);
});
