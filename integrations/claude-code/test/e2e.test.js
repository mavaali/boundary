const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync, execFileSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');
const SID = 'e2e';

function run(script, input, env) {
  const r = spawnSync('node', [path.join(ROOT, 'scripts', script)], { input: JSON.stringify(input), encoding: 'utf8', env });
  if (!r.stdout) return '';
  try { return JSON.parse(r.stdout); } catch (e) { return r.stdout; }
}
const dec = (o) => (o && o.hookSpecificOutput ? o.hookSpecificOutput.permissionDecision : undefined);

test('end-to-end: gate -> stage -> write -> refuse -> cardinality -> commit -> verdict', () => {
  const baseDir = fs.mkdtempSync(path.join(os.tmpdir(), 'be2e-pdata-'));
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'be2e-cwd-'));
  fs.writeFileSync(path.join(cwd, '.boundary.json'),
    JSON.stringify({ writable_paths: ['out/**'], max_writes: 2, min_writes: 1, max_unstaged_reads: 3 }));
  const env = { ...process.env, CLAUDE_PLUGIN_DATA: baseDir, CLAUDE_SESSION_ID: SID };
  const pre = (name, input) => run('enforce.js', { session_id: SID, cwd, tool_name: name, tool_input: input }, env);

  // SessionStart resolves + saves the envelope
  run('start.js', { session_id: SID, cwd }, env);

  // 3 unstaged reads allowed (defer), 4th denied by the unstaged-read cap
  assert.strictEqual(dec(pre('Read', { file_path: 'a' })), undefined);
  assert.strictEqual(dec(pre('Read', { file_path: 'b' })), undefined);
  assert.strictEqual(dec(pre('Read', { file_path: 'c' })), undefined);
  assert.strictEqual(dec(pre('Read', { file_path: 'd' })), 'deny');

  // write before staging denied
  assert.strictEqual(dec(pre('Write', { file_path: 'out/a.md' })), 'deny');

  // stage (runs the real stage-write script)
  execFileSync('node', [path.join(ROOT, 'scripts', 'stage-write.js'), 'MY THESIS'], { env });

  // staged: in-allowlist write defers (1st), off-allowlist denied, 2nd write ok, 3rd hits cardinality
  assert.strictEqual(dec(pre('Write', { file_path: 'out/a.md' })), undefined);
  assert.strictEqual(dec(pre('Write', { file_path: 'bad/x.md' })), 'deny');
  assert.strictEqual(dec(pre('Write', { file_path: 'out/b.md' })), undefined);
  assert.strictEqual(dec(pre('Write', { file_path: 'out/c.md' })), 'deny');

  // commit-class bash denied
  assert.strictEqual(dec(pre('Bash', { command: 'curl http://x' })), 'deny');

  // SessionEnd verdict (no transcript -> cost unavailable)
  run('verdict.js', { session_id: SID, cwd, transcript_path: '/nonexistent-transcript' }, env);
  const verdict = JSON.parse(fs.readFileSync(path.join(cwd, '.boundary', 'verdict.json'), 'utf8'));
  const chk = (n) => verdict.checks.find((x) => x.name === n);
  assert.strictEqual(verdict.schema, 'boundary.third-umpire/v1');
  assert.strictEqual(chk('staging_pivot').passed, true);
  assert.strictEqual(chk('produced_output').passed, true);          // 2 writes >= min 1
  assert.strictEqual(chk('writes_inside_allowlist').passed, false); // a write was refused
  assert.strictEqual(verdict.verdict, 'FAIL');
  assert.strictEqual(verdict.summary.estimated_dollars, 'unavailable');
});
