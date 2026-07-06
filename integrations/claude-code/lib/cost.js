// USD per 1M tokens for a common subset of models (values match
// boundary/envelope.py token_rates where they overlap). This is an estimate card,
// not a full mirror; unlisted models fall back to the conservative flat rate in
// rateFor() below — NOT the engine's per-axis max_rate policy. Widen as needed.
const DEFAULT_RATES = {
  'claude-sonnet-4.6': { input: 3.0, cached: 0.30, cache_write: 3.75, output: 15.0 },
  'claude-opus-4.7':   { input: 15.0, cached: 1.50, cache_write: 18.75, output: 75.0 },
  'claude-haiku-4.5':  { input: 0.80, cached: 0.08, cache_write: 1.00, output: 4.0 },
};

function rateFor(rates, model) {
  return rates[model] || { input: 15.0, cached: 1.5, cache_write: 18.75, output: 75.0 }; // conservative fallback
}

function estimateCost(lines, rates) {
  let dollars = 0, inTok = 0, outTok = 0, seen = false;
  for (const l of lines || []) {
    const u = l && l.message && l.message.usage;
    if (!u || typeof u.input_tokens !== 'number') continue;
    seen = true;
    const r = rateFor(rates, (l.message && l.message.model) || '');
    const cached = u.cache_read_input_tokens || 0;
    const cacheWrite = u.cache_creation_input_tokens || 0;
    const fresh = Math.max((u.input_tokens || 0) - cached - cacheWrite, 0);
    const out = u.output_tokens || 0;
    dollars += (fresh / 1e6) * r.input + (cached / 1e6) * r.cached
             + (cacheWrite / 1e6) * r.cache_write + (out / 1e6) * r.output;
    inTok += u.input_tokens || 0;
    outTok += out;
  }
  if (!seen) return { dollars: null, in_tok: 0, out_tok: 0 };
  return { dollars, in_tok: inTok, out_tok: outTok };
}

module.exports = { estimateCost, DEFAULT_RATES };
