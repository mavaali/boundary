const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const { grade, toEngineTranscript } = require('../lib/grade');
const { estimateCost, DEFAULT_RATES } = require('../lib/cost');
const stateLib = require('../lib/state');

const WRITE_KINDS = ['write_allowed', 'write_refused', 'limit_hit'];

function handle(input, io) {
  const sid = input.session_id;
  const envelope = io.readEnvelope(sid);
  io.appendEvent(sid, { kind: 'envelope_end', tool: '', detail: '' });
  const events = io.readEvents(sid);
  const counters = io.readState(sid);
  const summary = {
    writes_executed: counters.writes_executed || 0,
    writes_attempted: events.filter((e) => WRITE_KINDS.includes(e.kind)).length,
  };
  const verdict = grade(events, envelope, summary);

  // Post-hoc cost estimate; degrades to "unavailable", never throws.
  let cost = null;
  try {
    const lines = io.readTranscript(input.transcript_path);
    cost = lines ? estimateCost(lines, DEFAULT_RATES) : null;
  } catch (e) { cost = null; }
  if (cost && cost.dollars != null) {
    verdict.summary.estimated_dollars = Number(cost.dollars.toFixed(6));
    verdict.summary.in_tok = cost.in_tok;
    verdict.summary.out_tok = cost.out_tok;
  } else {
    verdict.summary.estimated_dollars = 'unavailable';
  }

  // Optional engine upgrade (best-effort; requires the transcript transform).
  if (io.hasEngine()) {
    try {
      const engineVerdict = io.runEngine(toEngineTranscript(events, envelope, summary));
      if (engineVerdict) verdict.engine = engineVerdict;
    } catch (e) { /* best-effort */ }
  }

  io.writeVerdict(input.cwd, verdict);
  return verdict;
}

function realIo() {
  const baseDir = process.env.CLAUDE_PLUGIN_DATA || os.tmpdir();
  return {
    readEnvelope(sid) {
      try { return JSON.parse(fs.readFileSync(path.join(stateLib.sessionDir(baseDir, sid), 'envelope.json'), 'utf8')); }
      catch (e) { return require('../lib/envelope').loadEnvelope(null); }
    },
    readEvents: (sid) => stateLib.readEvents(baseDir, sid),
    readState: (sid) => stateLib.readState(baseDir, sid),
    appendEvent: (sid, e) => stateLib.appendEvent(baseDir, sid, e),
    readTranscript(p) {
      if (!p) return null;
      return fs.readFileSync(p, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
    },
    hasEngine() {
      try { return spawnSync('boundary', ['--help'], { stdio: 'ignore' }).status === 0; }
      catch (e) { return false; }
    },
    runEngine(transcriptLines) {
      const tmp = path.join(os.tmpdir(), `boundary-cc-${process.pid}-${Date.now()}.jsonl`);
      fs.writeFileSync(tmp, transcriptLines.map((l) => JSON.stringify(l)).join('\n') + '\n');
      try {
        const r = spawnSync('boundary', ['third-umpire', tmp, '--format', 'json'], { encoding: 'utf8' });
        return r.stdout ? JSON.parse(r.stdout) : null;
      } catch (e) { return null; } finally { try { fs.unlinkSync(tmp); } catch (e) {} }
    },
    writeVerdict(cwd, verdict) {
      const dir = path.join(cwd || '.', '.boundary');
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'verdict.json'), JSON.stringify(verdict, null, 2));
      const cost = typeof verdict.summary.estimated_dollars === 'number'
        ? `$${verdict.summary.estimated_dollars.toFixed(4)}` : 'cost unavailable';
      process.stdout.write(`[boundary] verdict: ${verdict.verdict} (${cost})\n`);
    },
  };
}

if (require.main === module) {
  let raw = '';
  process.stdin.on('data', (c) => (raw += c));
  process.stdin.on('end', () => {
    let input = {};
    try { input = JSON.parse(raw); } catch (e) {}
    try { handle(input, realIo()); } catch (e) { process.stderr.write(`[boundary] verdict error: ${e.message}\n`); }
    process.exit(0);
  });
}

module.exports = { handle };
