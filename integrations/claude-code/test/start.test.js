const { test } = require('node:test');
const assert = require('node:assert');
const { handle } = require('../scripts/start');
const { DEFAULTS } = require('../lib/envelope');

function fakeIo(config) {
  const _saved = {}; const _events = []; let _state = null;
  return {
    readConfig: () => config,
    saveEnvelope: (sid, env) => (_saved.env = env),
    writeState: (sid, s) => (_state = s),
    appendEvent: (sid, e) => _events.push(e),
    _saved, _events, get state() { return _state; },
  };
}

test('SessionStart with no config uses defaults, inits state, emits envelope_start', () => {
  const io = fakeIo(null);
  const r = handle({ session_id: 's', cwd: '/x' }, io);
  assert.strictEqual(r.envelope.max_writes, DEFAULTS.max_writes);
  assert.deepStrictEqual(io._saved.env.writable_paths, DEFAULTS.writable_paths);
  assert.deepStrictEqual(io.state, { staged: false, writes_executed: 0, unstaged_reads: 0 });
  assert.strictEqual(io._events[0].kind, 'envelope_start');
});

test('SessionStart merges .boundary.json over defaults', () => {
  const io = fakeIo({ max_writes: 2, writable_paths: ['out/*.md'] });
  const r = handle({ session_id: 's', cwd: '/x' }, io);
  assert.strictEqual(r.envelope.max_writes, 2);
  assert.deepStrictEqual(r.envelope.writable_paths, ['out/*.md']);
});
