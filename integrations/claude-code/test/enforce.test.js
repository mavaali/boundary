const { test } = require('node:test');
const assert = require('node:assert');
const { handle } = require('../scripts/enforce');
const { loadEnvelope } = require('../lib/envelope');

function fakeIo({ staged = null, counters = { writes_executed: 0, unstaged_reads: 0 }, envelope } = {}) {
  const _writes = []; const _events = [];
  return {
    loadEnvelope: () => envelope || loadEnvelope({ writable_paths: ['out/**'], max_writes: 2 }),
    readState: () => counters,
    readStaged: () => staged,
    writeState: (s) => _writes.push(s),
    appendEvent: (e) => _events.push(e),
    _writes, _events,
  };
}

test('unstaged Write is denied with reason + staging_required event persisted', () => {
  const io = fakeIo();
  const res = handle({ tool_name: 'Write', tool_input: { file_path: 'out/x.md' } }, io);
  assert.strictEqual(res.decision, 'deny');
  assert.match(res.reason, /stage/i);
  assert.strictEqual(io._events[0].kind, 'staging_required');
});

test('staged in-allowlist Write allows and increments counter', () => {
  const io = fakeIo({ staged: { thesis: 'T' } });
  const res = handle({ tool_name: 'Write', tool_input: { file_path: 'out/x.md' } }, io);
  assert.strictEqual(res.decision, 'allow');
  assert.strictEqual(io._writes[0].writes_executed, 1);
  assert.strictEqual(io._events[0].kind, 'write_allowed');
});

test('refused write re-anchors on staged thesis', () => {
  const io = fakeIo({ staged: { thesis: 'THESIS-XYZ' }, counters: { writes_executed: 2, unstaged_reads: 0 } });
  const res = handle({ tool_name: 'Write', tool_input: { file_path: 'out/x.md' } }, io);
  assert.strictEqual(res.decision, 'deny');
  assert.match(res.reason, /THESIS-XYZ/);
});

test('unstaged Bash command merely mentioning stage-write.js in a comment is DENIED, not exempt', () => {
  const io = fakeIo({ staged: null });
  const res = handle({ tool_name: 'Bash', tool_input: { command: 'git push origin main # stage-write.js' } }, io);
  assert.strictEqual(res.decision, 'deny');
});

test('real node stage-write.js invocation, unstaged, allows (exempt)', () => {
  const io = fakeIo({ staged: null });
  const res = handle({ tool_name: 'Bash', tool_input: { command: 'node /x/scripts/stage-write.js "T"' } }, io);
  assert.strictEqual(res.decision, 'allow');
});
