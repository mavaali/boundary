const { test } = require('node:test');
const assert = require('node:assert');
const os = require('node:os');
const fs = require('node:fs');
const path = require('node:path');
const state = require('../lib/state');

const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'bstate-'));

test('readState returns defaults when missing', () => {
  const b = tmp();
  assert.deepStrictEqual(state.readState(b, 's1'), { staged: false, writes_executed: 0, unstaged_reads: 0 });
});
test('writeState/readState round-trips', () => {
  const b = tmp();
  state.writeState(b, 's1', { staged: true, writes_executed: 2, unstaged_reads: 1 });
  assert.deepStrictEqual(state.readState(b, 's1'), { staged: true, writes_executed: 2, unstaged_reads: 1 });
});
test('appendEvent accumulates; readEvents empty when missing', () => {
  const b = tmp();
  assert.deepStrictEqual(state.readEvents(b, 's1'), []);
  state.appendEvent(b, 's1', { kind: 'a' });
  state.appendEvent(b, 's1', { kind: 'b' });
  assert.deepStrictEqual(state.readEvents(b, 's1').map((e) => e.kind), ['a', 'b']);
});
test('staged: null when missing, round-trips when written', () => {
  const b = tmp();
  assert.strictEqual(state.readStaged(b, 's1'), null);
  state.writeStaged(b, 's1', { thesis: 'T' });
  assert.deepStrictEqual(state.readStaged(b, 's1'), { thesis: 'T' });
});
