const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { loadEnvelope, decide } = require('../lib/envelope');
const stateLib = require('../lib/state');

// Pure, testable core: (hook input, io) -> hook-output object.
function handle(input, io) {
  const sid = input.session_id;
  const envelope = io.loadEnvelope(input);
  const counters = io.readState(sid);
  const staged = io.readStaged(sid);            // { thesis, ... } or null
  const state = {
    writes_executed: counters.writes_executed || 0,
    unstaged_reads: counters.unstaged_reads || 0,
    staged: staged !== null,
    thesis: staged ? staged.thesis : undefined,
  };
  const r = decide(envelope, state, { name: input.tool_name, input: input.tool_input });
  io.writeState(sid, {
    staged: state.staged,
    writes_executed: r.state.writes_executed,
    unstaged_reads: r.state.unstaged_reads,
  });
  if (r.event) io.appendEvent(sid, r.event);
  const hookSpecificOutput = { hookEventName: 'PreToolUse' };
  if (r.decision === 'deny') {
    hookSpecificOutput.permissionDecision = 'deny';
    hookSpecificOutput.permissionDecisionReason = r.reason;
  }
  // On allow: no permissionDecision -> Claude Code's normal permission flow.
  return { hookSpecificOutput };
}

// Real io over lib/state + the plugin data dir + the resolved session envelope.
function realIo() {
  const baseDir = process.env.CLAUDE_PLUGIN_DATA || os.tmpdir();
  return {
    loadEnvelope(input) {
      const dir = stateLib.sessionDir(baseDir, input.session_id);
      try { return JSON.parse(fs.readFileSync(path.join(dir, 'envelope.json'), 'utf8')); } catch (e) {}
      try { return loadEnvelope(JSON.parse(fs.readFileSync(path.join(input.cwd || '.', '.boundary.json'), 'utf8'))); } catch (e) {}
      return loadEnvelope(null);
    },
    readState: (sid) => stateLib.readState(baseDir, sid),
    readStaged: (sid) => stateLib.readStaged(baseDir, sid),
    writeState: (sid, s) => stateLib.writeState(baseDir, sid, s),
    appendEvent: (sid, e) => stateLib.appendEvent(baseDir, sid, e),
  };
}

if (require.main === module) {
  let raw = '';
  process.stdin.on('data', (c) => (raw += c));
  process.stdin.on('end', () => {
    let input = {};
    try { input = JSON.parse(raw); } catch (e) {}
    process.stdout.write(JSON.stringify(handle(input, realIo())));
    process.exit(0);
  });
}

module.exports = { handle };
