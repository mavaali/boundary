const path = require('node:path');
const { decide } = require('../lib/envelope');
const { resolveEnvelope } = require('../lib/resolve');
const stateLib = require('../lib/state');

function isStageWriteInvocation(command) {
  const parts = (command || '').trim().split(/\s+/);
  return parts.length >= 2 && path.basename(parts[0]) === 'node' && path.basename(parts[1]) === 'stage-write.js';
}

// Pure core: (hook input, io) -> { decision: 'allow'|'deny', reason }.
function handle(input, io) {
  if (input.tool_name === 'Bash' && isStageWriteInvocation((input.tool_input && input.tool_input.command) || '')) {
    return { decision: 'allow', reason: '' };
  }
  const envelope = io.loadEnvelope();
  const counters = io.readState();
  const staged = io.readStaged();
  const state = {
    writes_executed: counters.writes_executed || 0,
    unstaged_reads: counters.unstaged_reads || 0,
    staged: staged !== null,
    thesis: staged ? staged.thesis : undefined,
  };
  const r = decide(envelope, state, { name: input.tool_name, input: input.tool_input });
  io.writeState({ staged: state.staged, writes_executed: r.state.writes_executed, unstaged_reads: r.state.unstaged_reads });
  if (r.event) io.appendEvent(r.event);
  return { decision: r.decision, reason: r.reason };
}

function realIo(input) {
  const stateDir = path.join(input.cwd || '.', '.boundary', 'state');
  return {
    loadEnvelope: () => resolveEnvelope(stateDir, input.cwd),
    readState: () => stateLib.readState(stateDir),
    readStaged: () => stateLib.readStaged(stateDir),
    writeState: (s) => stateLib.writeState(stateDir, s),
    appendEvent: (e) => stateLib.appendEvent(stateDir, e),
  };
}

if (require.main === module) {
  let raw = '';
  process.stdin.on('data', (c) => (raw += c));
  process.stdin.on('end', () => {
    let input = {};
    try { input = JSON.parse(raw); } catch (e) {}
    let res = { decision: 'allow', reason: '' };
    try { res = handle(input, realIo(input)); } catch (e) { process.exit(0); } // fail-open: a hook bug must not brick the session
    if (res.decision === 'deny') { process.stderr.write((res.reason || 'ENVELOPE REFUSED') + '\n'); process.exit(2); }
    process.exit(0);
  });
}

module.exports = { handle, isStageWriteInvocation };
