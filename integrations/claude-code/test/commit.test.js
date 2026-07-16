const { test } = require('node:test');
const assert = require('node:assert');
const { bashCommandIsCommit } = require('../lib/commit');

test('plain commit binaries are flagged', () => {
  assert.deepStrictEqual(bashCommandIsCommit('curl http://x'), { isCommit: true, matched: 'curl' });
  assert.strictEqual(bashCommandIsCommit('/usr/bin/gh pr create').isCommit, true);
});

test('git subcommands: push/commit/tag flagged; status/log not', () => {
  assert.strictEqual(bashCommandIsCommit('git push origin main').isCommit, true);
  assert.strictEqual(bashCommandIsCommit('git status').isCommit, false);
});

test('env-var prefixes are stripped', () => {
  assert.strictEqual(bashCommandIsCommit('FOO=bar curl http://x').isCommit, true);
});

test('non-commit commands pass', () => {
  assert.strictEqual(bashCommandIsCommit('ls -la').isCommit, false);
  assert.strictEqual(bashCommandIsCommit('').isCommit, false);
});
