const DEFAULTS = {
  writable_paths: ['scratch/**'],
  max_writes: 10,
  min_writes: 1,
  require_staging: true,
  max_unstaged_reads: 3,
  deny_commits: true,
};

function loadEnvelope(config) {
  return { ...DEFAULTS, ...(config || {}) };
}

module.exports = { DEFAULTS, loadEnvelope };
