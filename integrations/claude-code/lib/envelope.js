const { pathAllowed } = require('./paths');
const { bashCommandIsCommit } = require('./commit');

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

function ev(kind, tool, detail) { return { kind, tool, detail }; }

function decide(envelope, state, tool) {
  const s = { ...state };
  const name = tool.name;
  // Mirror the engine: staging gates only when require_staging AND writable_paths is
  // non-empty (envelope.py staging_required = require_staging and bool(writable_paths)).
  const stagingOn = envelope.require_staging && (envelope.writable_paths || []).length > 0;

  // 1. Unstaged-read cap
  if (stagingOn && !s.staged && (name === 'Read' || name === 'Grep')) {
    if (s.unstaged_reads >= envelope.max_unstaged_reads) {
      return deny('staging_required', name,
        `unstaged reads exhausted (${s.unstaged_reads}/${envelope.max_unstaged_reads}). Call /boundary:stage before more reads.`, s);
    }
    s.unstaged_reads += 1;
    return { decision: 'allow', reason: '', event: null, state: s };
  }

  const mutating = name === 'Write' || name === 'Edit' || name === 'Bash';

  // 2. Staging gate
  if (stagingOn && !s.staged && mutating) {
    return deny('staging_required', name,
      'Stage a thesis with /boundary:stage before writing or running commands.', s);
  }

  // 5. Commit denylist (Bash). NB: unlike the Python engine, a successful Bash is
  // intentionally NOT counted against max_writes — in CC, Bash is not a first-class
  // write tool and PreToolUse sees only the pre-execution call. This matches the spec's
  // enforcement list; do not "fix" it toward the engine.
  if (name === 'Bash' && envelope.deny_commits) {
    const c = bashCommandIsCommit((tool.input && tool.input.command) || '');
    if (c.isCommit) return deny('bash_commit_blocked', name, `command starts with '${c.matched}' (irreversible)`, s, false);
  }

  // 3+4. Write allowlist + cardinality
  if (name === 'Write' || name === 'Edit') {
    const p = (tool.input && tool.input.file_path) || '';
    if (!pathAllowed(envelope.writable_paths, p)) {
      return deny('write_refused', name, `path '${p}' not in writable_paths`, s);
    }
    if (s.writes_executed >= envelope.max_writes) {
      return deny('limit_hit', name, `max_writes (${envelope.max_writes}) reached`, s);
    }
    s.writes_executed += 1;
    return { decision: 'allow', reason: '', event: ev('write_allowed', name, `path=${p}`), state: s };
  }

  // default: allow (Read/Grep after staging, Bash non-commit, etc.)
  return { decision: 'allow', reason: '', event: null, state: s };

  function deny(kind, toolName, detail, st, reanchor = true) {
    let reason = `ENVELOPE REFUSED: ${detail}`;
    if (reanchor && st.staged && st.thesis) {
      reason += `\n\nResume from your staged thesis; do not restart research:\n${st.thesis}`;
    }
    return { decision: 'deny', reason, event: ev(kind, toolName, detail), state: st };
  }
}

module.exports = { DEFAULTS, loadEnvelope, decide };
