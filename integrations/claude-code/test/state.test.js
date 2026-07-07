const { test } = require('node:test');
const assert = require('node:assert');
const os = require('node:os');
const fs = require('node:fs');
const path = require('node:path');
const state = require('../lib/state');

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'bstate-'));

test('readState returns defaults when missing', () => {
  const d = tmp();
  assert.deepStrictEqual(state.readState(d), { staged: false, writes_executed: 0, unstaged_reads: 0 });
});
test('writeState/readState round-trips', () => {
  const d = tmp();
  state.writeState(d, { staged: true, writes_executed: 2, unstaged_reads: 1 });
  assert.deepStrictEqual(state.readState(d), { staged: true, writes_executed: 2, unstaged_reads: 1 });
});
test('appendEvent accumulates; readEvents empty when missing', () => {
  const d = tmp();
  assert.deepStrictEqual(state.readEvents(d), []);
  state.appendEvent(d, { kind: 'a' });
  state.appendEvent(d, { kind: 'b' });
  assert.deepStrictEqual(state.readEvents(d).map((e) => e.kind), ['a', 'b']);
});
test('staged: null when missing, round-trips when written', () => {
  const d = tmp();
  assert.strictEqual(state.readStaged(d), null);
  state.writeStaged(d, { thesis: 'T' });
  assert.deepStrictEqual(state.readStaged(d), { thesis: 'T' });
});
test('saveEnvelope/readEnvelopeFile round-trips; null when missing', () => {
  const d = tmp();
  assert.strictEqual(state.readEnvelopeFile(d), null);
  state.saveEnvelope(d, { max_writes: 5 });
  assert.deepStrictEqual(state.readEnvelopeFile(d), { max_writes: 5 });
});
