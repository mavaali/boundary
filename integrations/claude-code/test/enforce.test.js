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
    writeState: (sid, s) => _writes.push(s),
    appendEvent: (sid, e) => _events.push(e),
    _writes, _events,
  };
}

test('unstaged Write is denied with reason + staging_required event persisted', () => {
  const io = fakeIo();
  const out = handle({ session_id: 's', tool_name: 'Write', tool_input: { file_path: 'out/x.md' } }, io);
  assert.strictEqual(out.hookSpecificOutput.permissionDecision, 'deny');
  assert.match(out.hookSpecificOutput.permissionDecisionReason, /stage/i);
  assert.strictEqual(io._events[0].kind, 'staging_required');
});

test('staged in-allowlist Write defers (no permissionDecision) and increments counter', () => {
  const io = fakeIo({ staged: { thesis: 'T' } });
  const out = handle({ session_id: 's', tool_name: 'Write', tool_input: { file_path: 'out/x.md' } }, io);
  assert.strictEqual(out.hookSpecificOutput.permissionDecision, undefined);
  assert.strictEqual(out.hookSpecificOutput.hookEventName, 'PreToolUse');
  assert.strictEqual(io._writes[0].writes_executed, 1);
  assert.strictEqual(io._events[0].kind, 'write_allowed');
});

test('refused write re-anchors on staged thesis', () => {
  const io = fakeIo({ staged: { thesis: 'THESIS-XYZ' }, counters: { writes_executed: 2, unstaged_reads: 0 } });
  const out = handle({ session_id: 's', tool_name: 'Write', tool_input: { file_path: 'out/x.md' } }, io);
  assert.strictEqual(out.hookSpecificOutput.permissionDecision, 'deny');
  assert.match(out.hookSpecificOutput.permissionDecisionReason, /THESIS-XYZ/);
});
