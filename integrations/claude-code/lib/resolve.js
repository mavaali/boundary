const fs = require('node:fs');
const path = require('node:path');
const { loadEnvelope } = require('./envelope');
const { sessionDir } = require('./state');

// Three-tier envelope resolution shared by enforce.js and verdict.js so they never
// disagree: saved session envelope -> re-read .boundary.json from cwd -> defaults.
function resolveEnvelope(baseDir, sessionId, cwd) {
  try { return JSON.parse(fs.readFileSync(path.join(sessionDir(baseDir, sessionId), 'envelope.json'), 'utf8')); } catch (e) {}
  try { return loadEnvelope(JSON.parse(fs.readFileSync(path.join(cwd || '.', '.boundary.json'), 'utf8'))); } catch (e) {}
  return loadEnvelope(null);
}
module.exports = { resolveEnvelope };
