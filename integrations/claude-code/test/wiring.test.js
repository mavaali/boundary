const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

test('plugin.json name is boundary', () => {
  const p = JSON.parse(fs.readFileSync(path.join(__dirname, '../.claude-plugin/plugin.json'), 'utf8'));
  assert.strictEqual(p.name, 'boundary');
});

test('hooks.json declares SessionStart, PreToolUse (with matcher), Stop', () => {
  const h = JSON.parse(fs.readFileSync(path.join(__dirname, '../hooks/hooks.json'), 'utf8'));
  assert.ok(h.hooks.SessionStart && h.hooks.Stop);
  assert.ok(!h.hooks.SessionEnd);
  const pre = h.hooks.PreToolUse[0];
  assert.match(pre.matcher, /Write\|Edit\|Bash\|Read\|Grep/);
  assert.match(pre.hooks[0].command, /enforce\.js/);
  assert.match(h.hooks.Stop[0].hooks[0].command, /verdict\.js/);
});

test('recordStage writes staged.json + a staged event', () => {
  const { recordStage } = require('../scripts/stage-write');
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bstage-'));
  recordStage(stateDir, 'MY THESIS');
  const staged = JSON.parse(fs.readFileSync(path.join(stateDir, 'staged.json'), 'utf8'));
  assert.strictEqual(staged.thesis, 'MY THESIS');
  const events = fs.readFileSync(path.join(stateDir, 'events.jsonl'), 'utf8');
  assert.match(events, /"kind":"staged"/);
});

test('enforce exempts the stage-write bash pre-stage (allows, not denies)', () => {
  const { handle } = require('../scripts/enforce');
  const { loadEnvelope } = require('../lib/envelope');
  const io = {
    loadEnvelope: () => loadEnvelope({ writable_paths: ['out/**'] }),
    readState: () => ({ writes_executed: 0, unstaged_reads: 0 }),
    readStaged: () => null,
    writeState: () => {}, appendEvent: () => {},
  };
  const res = handle({ tool_name: 'Bash', tool_input: { command: 'node /x/scripts/stage-write.js "T"' } }, io);
  assert.strictEqual(res.decision, 'allow');
});
