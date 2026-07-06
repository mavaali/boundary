const { test } = require('node:test');
const assert = require('node:assert');
const { pathAllowed, normalizeRel } = require('../lib/paths');

test('normalizeRel rejects absolute and escaping paths', () => {
  assert.strictEqual(normalizeRel('/etc/passwd'), null);
  assert.strictEqual(normalizeRel('../secret.md'), null);
  assert.strictEqual(normalizeRel('reports/../secret.md'), 'secret.md'); // collapses but stays in-root
  assert.strictEqual(normalizeRel('a/b.md'), 'a/b.md');
});

test('pathAllowed: * stays within a segment', () => {
  assert.strictEqual(pathAllowed(['reports/*.md'], 'reports/a.md'), true);
  assert.strictEqual(pathAllowed(['reports/*.md'], 'reports/a/b.md'), false);
});

test('pathAllowed: ** spans segments (opt-in)', () => {
  assert.strictEqual(pathAllowed(['scratch/**'], 'scratch/a/b/c.md'), true);
  assert.strictEqual(pathAllowed(['scratch/**'], 'other/a.md'), false);
});

test('pathAllowed: case-sensitive, empty allowlist denies all', () => {
  assert.strictEqual(pathAllowed(['Reports/*.md'], 'reports/a.md'), false);
  assert.strictEqual(pathAllowed([], 'a.md'), false);
});

test('pathAllowed: absolute/escaping candidate is denied', () => {
  assert.strictEqual(pathAllowed(['**'], '/etc/passwd'), false);
  assert.strictEqual(pathAllowed(['**'], '../x.md'), false);
});
