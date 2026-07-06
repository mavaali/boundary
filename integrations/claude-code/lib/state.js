const fs = require('node:fs');
const path = require('node:path');

function sessionDir(baseDir, sessionId) {
  const dir = path.join(baseDir, sessionId);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}
function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}
function readState(baseDir, sessionId) {
  return readJson(path.join(sessionDir(baseDir, sessionId), 'state.json'),
    { staged: false, writes_executed: 0, unstaged_reads: 0 });
}
function writeState(baseDir, sessionId, state) {
  fs.writeFileSync(path.join(sessionDir(baseDir, sessionId), 'state.json'), JSON.stringify(state));
}
function appendEvent(baseDir, sessionId, event) {
  fs.appendFileSync(path.join(sessionDir(baseDir, sessionId), 'events.jsonl'), JSON.stringify(event) + '\n');
}
function readEvents(baseDir, sessionId) {
  const file = path.join(sessionDir(baseDir, sessionId), 'events.jsonl');
  try {
    return fs.readFileSync(file, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  } catch { return []; }
}
function writeStaged(baseDir, sessionId, staged) {
  fs.writeFileSync(path.join(sessionDir(baseDir, sessionId), 'staged.json'), JSON.stringify(staged));
}
function readStaged(baseDir, sessionId) {
  return readJson(path.join(sessionDir(baseDir, sessionId), 'staged.json'), null);
}
module.exports = { sessionDir, readState, writeState, appendEvent, readEvents, writeStaged, readStaged };
