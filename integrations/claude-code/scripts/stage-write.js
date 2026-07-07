const path = require('node:path');
const stateLib = require('../lib/state');

function recordStage(stateDir, thesis) {
  stateLib.writeStaged(stateDir, { thesis: thesis || '' });
  stateLib.appendEvent(stateDir, { kind: 'staged', tool: 'stage_proposal', detail: (thesis || '').slice(0, 120) });
}

if (require.main === module) {
  const root = process.env.CLAUDE_PROJECT_DIR || process.cwd();
  const stateDir = path.join(root, '.boundary', 'state');
  const thesis = process.argv.slice(2).join(' ');
  recordStage(stateDir, thesis);
  process.stdout.write('[boundary] staged.\n');
}
module.exports = { recordStage };
