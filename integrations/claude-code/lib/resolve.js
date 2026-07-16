const fs = require('node:fs');
const path = require('node:path');
const { loadEnvelope } = require('./envelope');
const { readEnvelopeFile } = require('./state');

// Envelope resolution: saved session envelope -> .boundary.json in cwd -> defaults.
function resolveEnvelope(stateDir, cwd) {
  const saved = readEnvelopeFile(stateDir);
  if (saved) return saved;
  try { return loadEnvelope(JSON.parse(fs.readFileSync(path.join(cwd || '.', '.boundary.json'), 'utf8'))); } catch (e) {}
  return loadEnvelope(null);
}
module.exports = { resolveEnvelope };
