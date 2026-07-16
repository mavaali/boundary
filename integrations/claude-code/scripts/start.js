const fs = require('node:fs');
const path = require('node:path');
const { loadEnvelope } = require('../lib/envelope');
const stateLib = require('../lib/state');

function handle(input, io) {
  const envelope = loadEnvelope(io.readConfig());
  io.saveEnvelope(envelope);
  io.writeState({ staged: false, writes_executed: 0, unstaged_reads: 0 });
  io.appendEvent({ kind: 'envelope_start', tool: '', detail: `writable_paths=${JSON.stringify(envelope.writable_paths)}` });
  return { envelope };
}

function realIo(input) {
  const stateDir = path.join(input.cwd || '.', '.boundary', 'state');
  return {
    readConfig: () => { try { return JSON.parse(fs.readFileSync(path.join(input.cwd || '.', '.boundary.json'), 'utf8')); } catch (e) { return null; } },
    saveEnvelope: (env) => stateLib.saveEnvelope(stateDir, env),
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
    try { handle(input, realIo(input)); } catch (e) {}
    process.exit(0);
  });
}
module.exports = { handle };
