const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { resolveEnvelope } = require('../lib/resolve');

test('resolveEnvelope falls back to .boundary.json in cwd when no saved envelope exists', () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bstatedir-'));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'bcwd-'));
  fs.writeFileSync(path.join(cwd, '.boundary.json'), JSON.stringify({ min_writes: 3, writable_paths: ['docs/**'] }));
  const envelope = resolveEnvelope(stateDir, cwd);
  assert.strictEqual(envelope.min_writes, 3);
});

test('resolveEnvelope prefers a saved envelope.json in stateDir over .boundary.json in cwd', () => {
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bstatedir-'));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'bcwd-'));
  fs.writeFileSync(path.join(cwd, '.boundary.json'), JSON.stringify({ min_writes: 3, writable_paths: ['docs/**'] }));
  fs.mkdirSync(stateDir, { recursive: true });
  fs.writeFileSync(path.join(stateDir, 'envelope.json'), JSON.stringify({ min_writes: 99, writable_paths: ['saved/**'] }));
  const envelope = resolveEnvelope(stateDir, cwd);
  assert.strictEqual(envelope.min_writes, 99);
});
