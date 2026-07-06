const os = require('node:os');
const stateLib = require('../lib/state');

// Testable core: record staging for a session.
function recordStage(baseDir, sessionId, thesis) {
  stateLib.writeStaged(baseDir, sessionId, { thesis: thesis || '' });
  stateLib.appendEvent(baseDir, sessionId, { kind: 'staged', tool: 'stage_proposal', detail: (thesis || '').slice(0, 120) });
}

if (require.main === module) {
  const baseDir = process.env.CLAUDE_PLUGIN_DATA || os.tmpdir();
  // LIVE-VALIDATE (Task 15): does Claude Code expose a session id to Bash-invoked
  // scripts? If not, the staging command needs rework (e.g. cwd-keyed state).
  const sessionId = process.env.CLAUDE_SESSION_ID || 'default';
  const thesis = process.argv.slice(2).join(' ');
  recordStage(baseDir, sessionId, thesis);
  process.stdout.write('[boundary] staged.\n');
}

module.exports = { recordStage };
