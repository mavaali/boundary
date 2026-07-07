const fs = require('node:fs');
const path = require('node:path');

function ensureDir(stateDir) { fs.mkdirSync(stateDir, { recursive: true }); return stateDir; }
function readJson(file, fallback) { try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (e) { return fallback; } }

function readState(stateDir) {
  return readJson(path.join(stateDir, 'state.json'), { staged: false, writes_executed: 0, unstaged_reads: 0 });
}
function writeState(stateDir, state) { ensureDir(stateDir); fs.writeFileSync(path.join(stateDir, 'state.json'), JSON.stringify(state)); }
function appendEvent(stateDir, event) { ensureDir(stateDir); fs.appendFileSync(path.join(stateDir, 'events.jsonl'), JSON.stringify(event) + '\n'); }
function readEvents(stateDir) {
  try { return fs.readFileSync(path.join(stateDir, 'events.jsonl'), 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)); }
  catch (e) { return []; }
}
function writeStaged(stateDir, staged) { ensureDir(stateDir); fs.writeFileSync(path.join(stateDir, 'staged.json'), JSON.stringify(staged)); }
function readStaged(stateDir) { return readJson(path.join(stateDir, 'staged.json'), null); }
function saveEnvelope(stateDir, env) { ensureDir(stateDir); fs.writeFileSync(path.join(stateDir, 'envelope.json'), JSON.stringify(env)); }
function readEnvelopeFile(stateDir) { return readJson(path.join(stateDir, 'envelope.json'), null); }

module.exports = { ensureDir, readState, writeState, appendEvent, readEvents, writeStaged, readStaged, saveEnvelope, readEnvelopeFile };
