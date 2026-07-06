const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { loadEnvelope } = require('../lib/envelope');
const stateLib = require('../lib/state');

function handle(input, io) {
  const sid = input.session_id;
  const envelope = loadEnvelope(io.readConfig(input));
  io.saveEnvelope(sid, envelope);
  io.writeState(sid, { staged: false, writes_executed: 0, unstaged_reads: 0 });
  io.appendEvent(sid, { kind: 'envelope_start', tool: '', detail: `writable_paths=${JSON.stringify(envelope.writable_paths)}` });
  return { envelope };
}

function realIo() {
  const baseDir = process.env.CLAUDE_PLUGIN_DATA || os.tmpdir();
  return {
    readConfig(input) {
      try { return JSON.parse(fs.readFileSync(path.join(input.cwd || '.', '.boundary.json'), 'utf8')); } catch (e) { return null; }
    },
    saveEnvelope(sid, env) {
      fs.writeFileSync(path.join(stateLib.sessionDir(baseDir, sid), 'envelope.json'), JSON.stringify(env));
    },
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
    handle(input, realIo());
    process.exit(0);
  });
}

module.exports = { handle };
