const { test } = require('node:test');
const assert = require('node:assert');
const { loadEnvelope, decide } = require('../lib/envelope');
const base = (over) => ({ staged: false, writes_executed: 0, unstaged_reads: 0, ...over });
const env = (over) => loadEnvelope({ writable_paths: ['out/**'], max_writes: 2, min_writes: 1, max_unstaged_reads: 3, ...over });

test('read past unstaged cap denies', () => {
  const r = decide(env(), base({ unstaged_reads: 3 }), { name: 'Read', input: { file_path: 'a' } });
  assert.strictEqual(r.decision, 'deny');
  assert.match(r.reason, /stage/i);
  assert.strictEqual(r.event.kind, 'staging_required');
});
test('read under cap allows and counts', () => {
  const r = decide(env(), base({ unstaged_reads: 1 }), { name: 'Read', input: { file_path: 'a' } });
  assert.strictEqual(r.decision, 'allow');
  assert.strictEqual(r.state.unstaged_reads, 2);
});
test('write before staging denies', () => {
  const r = decide(env(), base(), { name: 'Write', input: { file_path: 'out/x.md' } });
  assert.strictEqual(r.decision, 'deny');
  assert.strictEqual(r.event.kind, 'staging_required');
});
test('staged write to allowed path allows and counts', () => {
  const r = decide(env(), base({ staged: true }), { name: 'Write', input: { file_path: 'out/x.md' } });
  assert.strictEqual(r.decision, 'allow');
  assert.strictEqual(r.event.kind, 'write_allowed');
  assert.strictEqual(r.state.writes_executed, 1);
});
test('staged write to disallowed path denies, counter unchanged', () => {
  const r = decide(env(), base({ staged: true }), { name: 'Write', input: { file_path: 'other/x.md' } });
  assert.strictEqual(r.decision, 'deny');
  assert.strictEqual(r.event.kind, 'write_refused');
  assert.strictEqual(r.state.writes_executed, 0);
});
test('staged write at max_writes denies', () => {
  const r = decide(env(), base({ staged: true, writes_executed: 2 }), { name: 'Write', input: { file_path: 'out/x.md' } });
  assert.strictEqual(r.decision, 'deny');
  assert.strictEqual(r.event.kind, 'limit_hit');
});
test('staged bash curl denies as commit', () => {
  const r = decide(env(), base({ staged: true }), { name: 'Bash', input: { command: 'curl http://x' } });
  assert.strictEqual(r.decision, 'deny');
  assert.strictEqual(r.event.kind, 'bash_commit_blocked');
});
test('staged bash ls allows', () => {
  const r = decide(env(), base({ staged: true }), { name: 'Bash', input: { command: 'ls -la' } });
  assert.strictEqual(r.decision, 'allow');
});
test('refused write re-anchors on the staged thesis', () => {
  const s = base({ staged: true, writes_executed: 2, thesis: 'THESIS-XYZ' });
  const r = decide(env(), s, { name: 'Write', input: { file_path: 'out/x.md' } });
  assert.strictEqual(r.decision, 'deny');
  assert.match(r.reason, /THESIS-XYZ/);
  assert.match(r.reason, /resume/i);
});
