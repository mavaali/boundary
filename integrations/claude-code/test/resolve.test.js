const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resolveEnvelope } = require('../lib/resolve');

test('resolveEnvelope falls back to .boundary.json in cwd when no session envelope exists', () => {
  const baseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bbase-'));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'bcwd-'));
  fs.writeFileSync(path.join(cwd, '.boundary.json'), JSON.stringify({ min_writes: 3, writable_paths: ['docs/**'] }));
  const envelope = resolveEnvelope(baseDir, 's', cwd);
  assert.strictEqual(envelope.min_writes, 3);
});
