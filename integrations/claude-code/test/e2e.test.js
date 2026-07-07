const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const ROOT = path.join(__dirname, '..');

function runHook(script, input, cwd) {
  return spawnSync('node', [path.join(ROOT, 'scripts', script)], { input: JSON.stringify(input), cwd, encoding: 'utf8' });
}

test('end-to-end: gate -> stage -> write -> refuse -> cardinality -> commit -> verdict', () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'be2e-cwd-'));
  fs.writeFileSync(path.join(cwd, '.boundary.json'),
    JSON.stringify({ writable_paths: ['out/**'], max_writes: 2, min_writes: 1, max_unstaged_reads: 3 }));

  const pre = (name, input) => runHook('enforce.js', { cwd, tool_name: name, tool_input: input }, cwd);

  // SessionStart resolves + saves the envelope, co-located under <cwd>/.boundary/state/
  runHook('start.js', { cwd }, cwd);
  assert.ok(fs.existsSync(path.join(cwd, '.boundary', 'state', 'envelope.json')));

  // 3 unstaged reads allowed (exit 0), 4th denied by the unstaged-read cap (exit 2)
  assert.strictEqual(pre('Read', { file_path: 'a' }).status, 0);
  assert.strictEqual(pre('Read', { file_path: 'b' }).status, 0);
  assert.strictEqual(pre('Read', { file_path: 'c' }).status, 0);
  const denyRead = pre('Read', { file_path: 'd' });
  assert.strictEqual(denyRead.status, 2);
  assert.match(denyRead.stderr, /stage/i);

  // write before staging denied (exit 2)
  assert.strictEqual(pre('Write', { file_path: 'out/a.md' }).status, 2);

  // stage (runs the real stage-write script, in the same cwd)
  const staged = spawnSync('node', [path.join(ROOT, 'scripts', 'stage-write.js'), 'MY', 'THESIS'], { cwd, encoding: 'utf8' });
  assert.strictEqual(staged.status, 0);
  assert.ok(fs.existsSync(path.join(cwd, '.boundary', 'state', 'staged.json')));

  // staged: in-allowlist write allows (1st), off-allowlist denied, 2nd write ok, 3rd hits cardinality
  assert.strictEqual(pre('Write', { file_path: 'out/a.md' }).status, 0);
  assert.strictEqual(pre('Write', { file_path: 'bad/x.md' }).status, 2);
  assert.strictEqual(pre('Write', { file_path: 'out/b.md' }).status, 0);
  assert.strictEqual(pre('Write', { file_path: 'out/c.md' }).status, 2);

  // commit-class bash denied
  assert.strictEqual(pre('Bash', { command: 'curl http://x' }).status, 2);

  // Stop event verdict (no transcript -> cost unavailable)
  const v = runHook('verdict.js', { cwd, transcript_path: '/nonexistent-transcript' }, cwd);
  assert.strictEqual(v.status, 0);
  const verdict = JSON.parse(fs.readFileSync(path.join(cwd, '.boundary', 'verdict.json'), 'utf8'));
  const chk = (n) => verdict.checks.find((x) => x.name === n);
  assert.strictEqual(verdict.schema, 'boundary.third-umpire/v1');
  assert.strictEqual(chk('staging_pivot').passed, true);
  assert.strictEqual(chk('produced_output').passed, true);          // 2 writes >= min 1
  assert.strictEqual(chk('writes_inside_allowlist').passed, false); // a write was refused
  assert.strictEqual(verdict.verdict, 'FAIL');
  assert.strictEqual(verdict.summary.estimated_dollars, 'unavailable');
});
