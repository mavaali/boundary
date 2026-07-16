# Boundary Claude Code Plugin — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-contained Claude Code plugin that enforces Boundary's envelope contract (write allowlist, min/max write cardinality, staging pivot, commit denylist) inside a CC session and emits a `boundary.third-umpire/v1` verdict with a post-hoc cost estimate.

**Architecture:** Pure logic in `lib/*.js` (config, path allowlist, commit detection, the `decide()` enforcement function, verdict grading, cost estimation) is unit-tested in isolation. Thin `scripts/*.js` are CC hook entry points (SessionStart / PreToolUse / SessionEnd) that read hook JSON on stdin, call `lib/`, persist per-session state under `${CLAUDE_PLUGIN_DATA}/<session_id>/`, and write stdout. No runtime dependencies beyond Node built-ins. The Python `boundary` engine is optional (verdict upgrade only).

**Tech Stack:** Node.js (built-ins: `fs`, `path`, `process`, `child_process`), `node --test` (built-in test runner), Claude Code plugin manifest + hooks.

**Spec:** `docs/superpowers/specs/2026-07-05-boundary-claude-code-plugin-design.md`

**Reference implementations to mirror (Python engine, in this repo):**
- Path allowlist: `boundary/envelope.py` — `_normalize_rel`, `_anchored_glob_match`, `_match_segments`, `path_allowed`.
- Commit denylist: `boundary/envelope.py` — `BASH_COMMIT_DENYLIST`, `_GIT_COMMIT_SUBCOMMANDS`, `_bash_command_is_commit`.
- Verdict shape + verdict rollup: `boundary/third_umpire.py` — `ThirdUmpireReport` (`verdict` property, `as_dict`), check names.
- Cost axes + formula: `boundary/envelope.py` — `estimate_cost`, `token_rates`.

---

## File Structure

All paths under `integrations/claude-code/` unless noted.

| File | Responsibility |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest (name `boundary`) |
| `hooks/hooks.json` | Declares SessionStart / PreToolUse / SessionEnd → scripts |
| `commands/stage.md` | `/boundary:stage` — records the staged thesis |
| `lib/paths.js` | `normalizeRel`, `anchoredGlobMatch`, `pathAllowed` (pure) |
| `lib/commit.js` | `bashCommandIsCommit` (pure) |
| `lib/envelope.js` | `loadEnvelope`, `decide` — the ordered enforcement (pure) |
| `lib/grade.js` | `grade(events)→verdict`, `toEngineTranscript` (pure) |
| `lib/cost.js` | `estimateCost(transcriptLines, rateCard)` + rate card (pure) |
| `lib/state.js` | session-dir read/write helpers (I/O, thin) |
| `scripts/start.js` | SessionStart handler |
| `scripts/enforce.js` | PreToolUse handler |
| `scripts/verdict.js` | SessionEnd handler |
| `test/*.test.js` | `node --test` unit + e2e fixtures |
| `README.md` | usage + explicit non-goals |

**Working branch:** `plan/boundary-cc-plugin` (already checked out). All commits land here.

---

### Task 1: Scaffold plugin + test harness

**Files:**
- Create: `integrations/claude-code/.claude-plugin/plugin.json`
- Create: `integrations/claude-code/test/smoke.test.js`

- [ ] **Step 1: Write the manifest**

`integrations/claude-code/.claude-plugin/plugin.json`:
```json
{
  "name": "boundary",
  "description": "Enforce an envelope contract (write allowlist, cardinality, staging pivot, commit denylist) on a Claude Code session and grade it with a boundary.third-umpire/v1 verdict.",
  "version": "0.1.0",
  "author": { "name": "Mihir Wagle" }
}
```

- [ ] **Step 2: Write a smoke test that proves `node --test` runs**

`integrations/claude-code/test/smoke.test.js`:
```js
const { test } = require('node:test');
const assert = require('node:assert');
test('node --test runs', () => { assert.strictEqual(1 + 1, 2); });
```

- [ ] **Step 3: Run it**

Run: `node --test integrations/claude-code/test/`
Expected: 1 test passes.

- [ ] **Step 4: Commit**

```bash
git add integrations/claude-code/.claude-plugin/plugin.json integrations/claude-code/test/smoke.test.js
git commit -m "feat(cc-plugin): scaffold plugin manifest + node --test harness"
```

---

### Task 2: `lib/paths.js` — write-allowlist matching

Mirror `boundary/envelope.py` `_normalize_rel` / `_anchored_glob_match` / `_match_segments`. `*`/`?` match within one path segment; `**` matches zero or more whole segments; matching is case-sensitive; absolute paths and paths escaping the root via `..` are rejected.

**Files:**
- Create: `integrations/claude-code/lib/paths.js`
- Test: `integrations/claude-code/test/paths.test.js`

- [ ] **Step 1: Write failing tests**

`integrations/claude-code/test/paths.test.js`:
```js
const { test } = require('node:test');
const assert = require('node:assert');
const { pathAllowed, normalizeRel } = require('../lib/paths');

test('normalizeRel rejects absolute and escaping paths', () => {
  assert.strictEqual(normalizeRel('/etc/passwd'), null);
  assert.strictEqual(normalizeRel('../secret.md'), null);
  assert.strictEqual(normalizeRel('reports/../secret.md'), 'secret.md'); // collapses but stays in-root
  assert.strictEqual(normalizeRel('a/b.md'), 'a/b.md');
});

test('pathAllowed: * stays within a segment', () => {
  assert.strictEqual(pathAllowed(['reports/*.md'], 'reports/a.md'), true);
  assert.strictEqual(pathAllowed(['reports/*.md'], 'reports/a/b.md'), false);
});

test('pathAllowed: ** spans segments (opt-in)', () => {
  assert.strictEqual(pathAllowed(['scratch/**'], 'scratch/a/b/c.md'), true);
  assert.strictEqual(pathAllowed(['scratch/**'], 'other/a.md'), false);
});

test('pathAllowed: case-sensitive, empty allowlist denies all', () => {
  assert.strictEqual(pathAllowed(['Reports/*.md'], 'reports/a.md'), false);
  assert.strictEqual(pathAllowed([], 'a.md'), false);
});

test('pathAllowed: absolute/escaping candidate is denied', () => {
  assert.strictEqual(pathAllowed(['**'], '/etc/passwd'), false);
  assert.strictEqual(pathAllowed(['**'], '../x.md'), false);
});
```

- [ ] **Step 2: Run to verify fail**

Run: `node --test integrations/claude-code/test/paths.test.js`
Expected: FAIL — `Cannot find module '../lib/paths'`.

- [ ] **Step 3: Implement**

`integrations/claude-code/lib/paths.js`:
```js
const path = require('node:path');

function normalizeRel(p) {
  const s = String(p).replace(/\\/g, '/').replace(/^\/+/, '');
  if (!s) return null;
  const norm = path.posix.normalize(s);
  if (norm === '.' || norm === '..' || norm.startsWith('../') || norm.startsWith('/')) return null;
  return norm;
}

// Translate one glob segment (with * ? and [..]) to an anchored RegExp, matching
// within a single path segment (no '/').
function segToRegExp(seg) {
  let re = '';
  for (const ch of seg) {
    if (ch === '*') re += '[^/]*';
    else if (ch === '?') re += '[^/]';
    else re += ch.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp('^' + re + '$');
}

function matchSegments(pat, parts) {
  if (pat.length === 0) return parts.length === 0;
  const [head, ...rest] = pat;
  if (head === '**') {
    if (matchSegments(rest, parts)) return true;
    return parts.length > 0 && matchSegments(pat, parts.slice(1));
  }
  if (parts.length === 0) return false;
  if (segToRegExp(head).test(parts[0])) return matchSegments(rest, parts.slice(1));
  return false;
}

function anchoredGlobMatch(pattern, p) {
  const pat = pattern.replace(/\\/g, '/').replace(/^\/+/, '').split('/').filter(Boolean);
  const parts = p.split('/').filter(Boolean);
  return matchSegments(pat, parts);
}

function pathAllowed(writablePaths, candidate) {
  if (!writablePaths || writablePaths.length === 0) return false;
  const norm = normalizeRel(candidate);
  if (norm === null) return false;
  return writablePaths.some((pat) => anchoredGlobMatch(pat, norm));
}

module.exports = { normalizeRel, anchoredGlobMatch, pathAllowed };
```

- [ ] **Step 4: Run to verify pass**

Run: `node --test integrations/claude-code/test/paths.test.js`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add integrations/claude-code/lib/paths.js integrations/claude-code/test/paths.test.js
git commit -m "feat(cc-plugin): anchored write-allowlist matching (mirrors engine)"
```

---

### Task 3: `lib/commit.js` — bash commit denylist

Mirror `boundary/envelope.py` `_bash_command_is_commit`: basename of argv[0] against the denylist; strip leading `FOO=bar` env assignments; the single `git` exception inspects argv[1] against `{push, commit, tag}`. No regex on arguments.

**Files:**
- Create: `integrations/claude-code/lib/commit.js`
- Test: `integrations/claude-code/test/commit.test.js`

- [ ] **Step 1: Write failing tests**

```js
const { test } = require('node:test');
const assert = require('node:assert');
const { bashCommandIsCommit } = require('../lib/commit');

test('plain commit binaries are flagged', () => {
  assert.deepStrictEqual(bashCommandIsCommit('curl http://x'), { isCommit: true, matched: 'curl' });
  assert.strictEqual(bashCommandIsCommit('/usr/bin/gh pr create').isCommit, true);
});
test('git subcommands: push/commit/tag flagged; status/log not', () => {
  assert.strictEqual(bashCommandIsCommit('git push origin main').isCommit, true);
  assert.strictEqual(bashCommandIsCommit('git status').isCommit, false);
});
test('env-var prefixes are stripped', () => {
  assert.strictEqual(bashCommandIsCommit('FOO=bar curl http://x').isCommit, true);
});
test('non-commit commands pass', () => {
  assert.strictEqual(bashCommandIsCommit('ls -la').isCommit, false);
  assert.strictEqual(bashCommandIsCommit('').isCommit, false);
});
```

- [ ] **Step 2: Run to verify fail.** Run: `node --test integrations/claude-code/test/commit.test.js` — FAIL (module missing).

- [ ] **Step 3: Implement**

```js
const path = require('node:path');

const DENYLIST = new Set(['curl', 'wget', 'gh', 'az', 'mail', 'sendmail', 'osascript', 'git']);
const GIT_COMMIT_SUBCOMMANDS = new Set(['push', 'commit', 'tag']);

function bashCommandIsCommit(command) {
  const none = { isCommit: false, matched: '' };
  if (!command || !command.trim()) return none;
  let parts = command.trim().split(/\s+/);
  let head = parts[0];
  while (head.includes('=') && !head.startsWith('/') && !head.startsWith('.')) {
    if (parts.length < 2) return none;
    parts = parts.slice(1);
    head = parts[0];
  }
  const base = path.basename(head);
  if (!DENYLIST.has(base)) return none;
  if (base === 'git') {
    const sub = parts[1] || '';
    if (!GIT_COMMIT_SUBCOMMANDS.has(sub)) return none;
    return { isCommit: true, matched: `git ${sub}` };
  }
  return { isCommit: true, matched: base };
}

module.exports = { bashCommandIsCommit, DENYLIST };
```

- [ ] **Step 4: Run to verify pass.** Expected: PASS (4).

- [ ] **Step 5: Commit**
```bash
git add integrations/claude-code/lib/commit.js integrations/claude-code/test/commit.test.js
git commit -m "feat(cc-plugin): bash commit denylist (mirrors engine)"
```

---

### Task 4: `lib/envelope.js` — config loading + defaults

**Files:**
- Create: `integrations/claude-code/lib/envelope.js`
- Test: `integrations/claude-code/test/envelope-load.test.js`

- [ ] **Step 1: Write failing tests**

```js
const { test } = require('node:test');
const assert = require('node:assert');
const { loadEnvelope, DEFAULTS } = require('../lib/envelope');

test('absent config yields defaults', () => {
  const env = loadEnvelope(null);
  assert.strictEqual(env.require_staging, DEFAULTS.require_staging);
  assert.strictEqual(env.max_writes, DEFAULTS.max_writes);
  assert.ok(Array.isArray(env.writable_paths));
});
test('partial config overrides only named keys', () => {
  const env = loadEnvelope({ max_writes: 2, writable_paths: ['out/*.md'] });
  assert.strictEqual(env.max_writes, 2);
  assert.deepStrictEqual(env.writable_paths, ['out/*.md']);
  assert.strictEqual(env.min_writes, DEFAULTS.min_writes); // untouched
});
```

- [ ] **Step 2: Run to verify fail.**

- [ ] **Step 3: Implement** (append to a new `lib/envelope.js`)

```js
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
```

- [ ] **Step 4: Run to verify pass.**

- [ ] **Step 5: Commit**
```bash
git add integrations/claude-code/lib/envelope.js integrations/claude-code/test/envelope-load.test.js
git commit -m "feat(cc-plugin): envelope config loading + defaults"
```

---

### Task 5: `lib/envelope.js` — `decide()` enforcement (rule by rule)

`decide(envelope, state, tool)` is pure: `tool = { name, input }` (from the hook's `tool_name`/`tool_input`), `state = { staged, writes_executed, unstaged_reads }`. Returns `{ decision: 'allow'|'deny', reason, event, state }` where `event` is `{ kind, tool, detail }` (or null) and `state` is the updated counters. Build it one rule at a time; each rule is a red→green→commit cycle. **Order matters** and mirrors `boundary/envelope.py` `_make_enforced_tool`.

**Files:** Modify `lib/envelope.js`; Test `integrations/claude-code/test/decide.test.js`.

Add a helper at the top of the test file:
```js
const { test } = require('node:test');
const assert = require('node:assert');
const { loadEnvelope, decide } = require('../lib/envelope');
const base = (over) => ({ staged: false, writes_executed: 0, unstaged_reads: 0, ...over });
const env = (over) => loadEnvelope({ writable_paths: ['out/**'], max_writes: 2, min_writes: 1, max_unstaged_reads: 3, ...over });
```

- [ ] **Step 5a — Unstaged-read cap.**
  - Test: `Read` past `max_unstaged_reads` while unstaged → deny with a `staging_required` event; under the cap → allow and increments `unstaged_reads`.
  ```js
  test('read past unstaged cap denies', () => {
    const r = decide(env(), base({ unstaged_reads: 3 }), { name: 'Read', input: { file_path: 'a' } });
    assert.strictEqual(r.decision, 'deny');
    assert.match(r.reason, /stage/i);
    assert.strictEqual(r.event.kind, 'staging_required');
  });
  test('read under cap allows and counts', () => {
    const r = decide(env(), base({ unstaged_reads: 1 }), { name: 'Read', input: { file_path: 'a' } });
    assert.strictEqual(r.decision, 'allow');
    assert.strictEqual(r.state.unstaged_reads, 2);
  });
  ```
  - Implement the first branch of `decide` (Read/Grep cap). Run → PASS. Commit `feat(cc-plugin): decide() unstaged-read cap`.

- [ ] **Step 5b — Staging gate on Write/Edit/Bash.**
  - Test: `Write` while `require_staging` and not staged → deny, event `staging_required`, reason replays "stage first".
  ```js
  test('write before staging denies', () => {
    const r = decide(env(), base(), { name: 'Write', input: { file_path: 'out/x.md' } });
    assert.strictEqual(r.decision, 'deny');
    assert.strictEqual(r.event.kind, 'staging_required');
  });
  ```
  - Extend `decide`. Run → PASS. Commit.

- [ ] **Step 5c — Write allowlist (staged).**
  - Test: staged `Write` to `out/x.md` → allow, `write_allowed`, `writes_executed` → 1; to `other/x.md` → deny, `write_refused`, counter unchanged.
  - Extend. Commit `feat(cc-plugin): decide() write allowlist`.

- [ ] **Step 5d — Cardinality.**
  - Test: staged `Write` with `writes_executed: 2`, `max_writes: 2` → deny, `limit_hit`.
  - Extend. Commit.

- [ ] **Step 5e — Commit denylist.**
  - Test: staged `Bash` `curl ...` → deny, `bash_commit_blocked` (only when `deny_commits`); staged `Bash` `ls` → allow.
  - Extend (call `bashCommandIsCommit`). Commit.

- [ ] **Step 5f — Refused-write re-anchor.**
  - Test: when a `Write` is denied for allowlist/cardinality AND `state.staged` with a thesis passed in `state.thesis`, `reason` includes the thesis text and "resume … do not restart".
  ```js
  test('refused write re-anchors on the staged thesis', () => {
    const s = base({ staged: true, writes_executed: 2, thesis: 'THESIS-XYZ' });
    const r = decide(env(), s, { name: 'Write', input: { file_path: 'out/x.md' } });
    assert.strictEqual(r.decision, 'deny');
    assert.match(r.reason, /THESIS-XYZ/);
    assert.match(r.reason, /resume/i);
  });
  ```
  - Extend the deny paths to include `state.thesis` in the reason. Commit `feat(cc-plugin): decide() re-anchor refused writes on staged thesis`.

Reference `decide()` skeleton (final shape after 5a–5f):
```js
const { pathAllowed } = require('./paths');
const { bashCommandIsCommit } = require('./commit');

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
```

---

### Task 6: `lib/grade.js` — self-contained verdict

`grade(events, envelope, summary)` → `boundary.third-umpire/v1` document. Mirror `boundary/third_umpire.py` verdict rollup (FAIL if any `severity:"fail"` check fails, else WARN if any `warn` fails, else PASS) and reuse the engine's check *names*.

**Files:** Create `lib/grade.js`; Test `test/grade.test.js`.

- [ ] **Step 1: Failing tests**
```js
const { test } = require('node:test');
const assert = require('node:assert');
const { grade } = require('../lib/grade');

const evs = (...e) => e;
test('clean staged run with a write passes', () => {
  const v = grade(evs(
    { kind: 'envelope_start' }, { kind: 'staged' }, { kind: 'write_allowed' }, { kind: 'envelope_end' }
  ), { min_writes: 1 }, { writes_executed: 1 });
  assert.strictEqual(v.schema, 'boundary.third-umpire/v1');
  assert.strictEqual(v.verdict, 'PASS');
});
test('write_refused fails writes_inside_allowlist and verdict', () => {
  const v = grade(evs({ kind: 'write_refused', tool: 'Write', detail: 'x' }), { min_writes: 1 }, { writes_executed: 0 });
  assert.strictEqual(v.verdict, 'FAIL');
  const c = v.checks.find((x) => x.name === 'writes_inside_allowlist');
  assert.strictEqual(c.passed, false);
});
test('under min_writes fails produced_output', () => {
  const v = grade(evs({ kind: 'staged' }), { min_writes: 2 }, { writes_executed: 1 });
  const c = v.checks.find((x) => x.name === 'produced_output');
  assert.strictEqual(c.passed, false);
});
test('missing staged event fails staging_pivot', () => {
  const v = grade(evs({ kind: 'write_allowed' }), { min_writes: 1, require_staging: true }, { writes_executed: 1 });
  const c = v.checks.find((x) => x.name === 'staging_pivot');
  assert.strictEqual(c.passed, false);
});
```

- [ ] **Step 2: Run → fail.**

- [ ] **Step 3: Implement**
```js
function check(name, passed, severity, detail) { return { name, passed, severity, detail }; }

function grade(events, envelope, summary) {
  const has = (k) => events.some((e) => e.kind === k);
  const writesExec = (summary && summary.writes_executed) || 0;
  const minWrites = (envelope && envelope.min_writes) != null ? envelope.min_writes : 1;
  const checks = [];

  checks.push(check('writes_inside_allowlist', !has('write_refused'), 'fail',
    has('write_refused') ? 'a write was refused (outside allowlist)' : 'all writes targeted allowed paths'));

  checks.push(check('produced_output', writesExec >= minWrites, 'fail',
    `${writesExec} write(s), min_writes=${minWrites}`));

  if (envelope && envelope.require_staging) {
    checks.push(check('staging_pivot', has('staged'), 'fail',
      has('staged') ? 'staged before writing' : 'staging required but never staged'));
  }

  checks.push(check('commit_denylist_held', true, 'info',
    has('bash_commit_blocked') ? 'blocked at least one commit-class command' : 'no commit-class commands attempted'));

  const verdict = checks.some((c) => !c.passed && c.severity === 'fail') ? 'FAIL'
    : checks.some((c) => !c.passed && c.severity === 'warn') ? 'WARN' : 'PASS';

  return { schema: 'boundary.third-umpire/v1', verdict, transcript_path: null, summary: summary || {}, checks };
}
module.exports = { grade };
```

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(cc-plugin): self-contained boundary.third-umpire/v1 verdict`.

---

### Task 7: `lib/grade.js` — engine-transcript transform

`toEngineTranscript(events, envelope, summary)` → array of transcript line objects the Python engine ingests: an `{type:'envelope_start', ...}` line, an `{type:'envelope_end', ..., events:[{kind,tool,detail,iteration}]}` line (events **nested**, keyed on `kind`), and an `{type:'end', iterations}` line. This is the ONLY shape `boundary third-umpire` accepts — a flat events log grades vacuously. See spec §Verdict.

**Files:** Modify `lib/grade.js`; Test `test/engine-transcript.test.js`.

- [ ] **Step 1: Failing test**
```js
const { test } = require('node:test');
const assert = require('node:assert');
const { toEngineTranscript } = require('../lib/grade');

test('produces envelope_start, envelope_end-with-nested-events, end', () => {
  const lines = toEngineTranscript(
    [{ kind: 'write_refused', tool: 'Write', detail: 'x' }],
    { writable_paths: ['out/**'], min_writes: 1, max_writes: 2, require_staging: true },
    { writes_executed: 0, writes_attempted: 1 }
  );
  const byType = Object.fromEntries(lines.map((l) => [l.type, l]));
  assert.ok(byType.envelope_start && byType.envelope_end && byType.end);
  assert.deepStrictEqual(byType.envelope_start.writable_paths, ['out/**']);
  assert.strictEqual(byType.envelope_end.events[0].kind, 'write_refused');
  assert.strictEqual(byType.envelope_end.events[0].tool, 'write_file'); // CC name mapped to engine name
  assert.ok('iteration' in byType.envelope_end.events[0]);
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```js
// Map CC tool names to the engine's tool vocabulary. The engine's staging_pivot /
// budget_pacing checks filter write_allowed events on ("write_file","edit_file","bash")
// (third_umpire.py) — without this mapping the optional engine verdict sees zero writes
// and mis-grades those checks. Self-contained verdict (lib/grade.js) is unaffected.
const ENGINE_TOOL = { Write: 'write_file', Edit: 'edit_file', Bash: 'bash', Read: 'read_file', Grep: 'grep' };

function toEngineTranscript(events, envelope, summary) {
  const nested = events
    .filter((e) => e.kind !== 'envelope_start' && e.kind !== 'envelope_end')
    .map((e, i) => ({ kind: e.kind, tool: ENGINE_TOOL[e.tool] || e.tool || '', detail: e.detail || '', iteration: i + 1 }));
  return [
    { type: 'envelope_start', writable_paths: envelope.writable_paths, min_writes: envelope.min_writes,
      max_writes: envelope.max_writes, require_staging: envelope.require_staging },
    { type: 'envelope_end', writes_executed: summary.writes_executed || 0,
      writes_attempted: summary.writes_attempted || 0, events: nested },
    { type: 'end', iterations: nested.length },
  ];
}
module.exports = { grade, toEngineTranscript };
```
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(cc-plugin): engine-transcript transform for the optional verdict upgrade`.

---

### Task 8: `lib/cost.js` — post-hoc cost estimate

`estimateCost(transcriptLines, rateCard)` → `{ dollars, in_tok, out_tok }` or `{ dollars: null }` (→ `"unavailable"`) when token fields are absent/unparseable. `dollars` prices four axes (input / cached / cache-write / output); `in_tok`/`out_tok` are display aggregates. Mirror `boundary/envelope.py` `estimate_cost` pricing. **The token-field shape of the CC transcript is the pre-build assumption (spec §Spend visibility) — Task 15 validates it; this task codes against a documented shape and degrades gracefully.**

**Files:** Create `lib/cost.js`; Test `test/cost.test.js`.

- [ ] **Step 1: Failing tests**
```js
const { test } = require('node:test');
const assert = require('node:assert');
const { estimateCost, DEFAULT_RATES } = require('../lib/cost');

// Assumed transcript usage shape (validate in Task 15): assistant lines carry
// message.usage {input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens}.
const line = (u) => ({ type: 'assistant', message: { model: 'claude-sonnet-4.6', usage: u } });

test('sums usage and prices per axis', () => {
  const r = estimateCost([
    line({ input_tokens: 1000, output_tokens: 500 }),
    line({ input_tokens: 2000, output_tokens: 100 }),
  ], DEFAULT_RATES);
  assert.strictEqual(r.in_tok, 3000);
  assert.strictEqual(r.out_tok, 600);
  assert.ok(r.dollars > 0);
});
test('absent usage degrades to unavailable', () => {
  const r = estimateCost([{ type: 'user' }], DEFAULT_RATES);
  assert.strictEqual(r.dollars, null);
});
```

- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
```js
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
```
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(cc-plugin): post-hoc cost estimator (degrades to unavailable)`.

---

### Task 9: `lib/state.js` — session-dir persistence

Thin I/O around `${CLAUDE_PLUGIN_DATA}/<session_id>/`: `sessionDir(env, id)`, `readState/writeState`, `appendEvent`, `readStaged/writeStaged`, `readEvents`. Kept minimal; logic lives in the pure modules.

**Files:** Create `lib/state.js`; Test `test/state.test.js` (uses a `tmp` dir via `CLAUDE_PLUGIN_DATA` override param).

- [ ] **Step 1: Failing test** — write then read state round-trips; `appendEvent` accumulates; missing files return sensible defaults (`{ staged:false, writes_executed:0, unstaged_reads:0 }`, `[]`, `null`). Use an injected base dir: `readState(baseDir, id)`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** with `fs.mkdirSync(dir,{recursive:true})`, `fs.writeFileSync`, `fs.appendFileSync`, JSON per line for events; each function takes `(baseDir, sessionId, ...)` so tests pass a tmp dir and scripts pass `process.env.CLAUDE_PLUGIN_DATA`.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(cc-plugin): per-session state persistence`.

---

### Task 10: `scripts/enforce.js` — PreToolUse handler

Thin: parse stdin JSON → `{ session_id, tool_name, tool_input, cwd }`; load envelope (from `state.json` written at SessionStart, or re-read `.boundary.json`); read state (+ `staged.json` → merge `staged`/`thesis` into state); `decide(...)`; persist new state; if `event`, `appendEvent`; print the CC hook JSON. Expose a pure `handle(input, io)` for tests; the file's bottom does the real stdin/stdout/`io`.

**Files:** Create `scripts/enforce.js`; Test `test/enforce.test.js`.

- [ ] **Step 1: Failing test** — call `handle(input, fakeIo)` where `fakeIo` supplies envelope+state and captures writes; assert the returned object equals:
```json
{ "hookSpecificOutput": { "hookEventName": "PreToolUse",
  "permissionDecision": "deny", "permissionDecisionReason": "ENVELOPE REFUSED: ..." } }
```
for an unstaged Write, and `permissionDecision:"allow"` for a staged in-allowlist Write, and that the event/state were persisted via `fakeIo`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement** `handle(input, io)` calling `decide`, mapping `{decision,reason}` → the `hookSpecificOutput` object (`allow`→`"allow"`, `deny`→`"deny"` + reason), persisting via `io`. Bottom of file: read `process.stdin`, build real `io` over `lib/state` + `lib/envelope`, `console.log(JSON.stringify(result))`, `process.exit(0)`.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(cc-plugin): PreToolUse enforce handler`.

---

### Task 11: `scripts/start.js` — SessionStart handler

Reads `.boundary.json` from `cwd` (via `lib/envelope.loadEnvelope`), writes initial `state.json`, appends an `envelope_start` event, stores the resolved envelope for later hooks (write `envelope.json` in the session dir). No stdout decision needed (SessionStart doesn't gate). Pure `handle(input, io)` + thin bottom.

**Files:** Create `scripts/start.js`; Test `test/start.test.js`.
- [ ] Steps 1–5 as above: test that `handle` initializes state to defaults-merged config and emits `envelope_start`. Commit `feat(cc-plugin): SessionStart init handler`.

---

### Task 12: `scripts/verdict.js` — SessionEnd handler

Reads the session dir: envelope, `events.jsonl`, state → appends `envelope_end` → `grade(events, envelope, summary)`. Reads `input.transcript_path`; if present and parseable, `estimateCost(lines, DEFAULT_RATES)` and set `verdict.summary.estimated_dollars` (+ `in_tok`/`out_tok`); on any parse failure set `estimated_dollars: "unavailable"` (never throw). If `boundary` is on `PATH` (`child_process.spawnSync('boundary', ['--help'])` succeeds), write `toEngineTranscript(...)` to a temp file, run `boundary third-umpire <file> --format json`, and attach the parsed result as `verdict.engine`. Write `.boundary/verdict.json` (in `cwd`) + a one-line summary to stdout.

**Files:** Create `scripts/verdict.js`; Test `test/verdict.test.js`.
- [ ] **Step 1: Failing tests** — `handle(input, io)` with injected events/envelope/transcript:
  - produces a `boundary.third-umpire/v1` doc with the right verdict;
  - with a transcript carrying usage, `summary.estimated_dollars` is a number;
  - with no/garbled transcript, `summary.estimated_dollars === 'unavailable'` and no throw;
  - engine call is gated behind an injected `io.hasEngine` flag (test both branches; do NOT spawn a real process in unit tests).
- [ ] **Step 2–4:** implement, run red→green.
- [ ] **Step 5: Commit** `feat(cc-plugin): SessionEnd verdict handler (+cost, +optional engine)`.

---

### Task 13: Wiring — hooks.json + command + defaults

**Files:** Create `hooks/hooks.json`, `commands/stage.md`; Test `test/wiring.test.js`.

- [ ] **Step 1: Failing test** — parse `hooks/hooks.json`, assert: a `PreToolUse` entry with matcher `Write|Edit|Bash|Read|Grep` → `${CLAUDE_PLUGIN_ROOT}/scripts/enforce.js`; a `SessionStart` → `start.js`; a `SessionEnd` → `verdict.js`; and that `.claude-plugin/plugin.json` parses with `name: "boundary"`.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**

`hooks/hooks.json`:
```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/scripts/start.js" }] }],
    "PreToolUse":  [{ "matcher": "Write|Edit|Bash|Read|Grep",
                      "hooks": [{ "type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/scripts/enforce.js" }] }],
    "SessionEnd":  [{ "hooks": [{ "type": "command", "command": "node ${CLAUDE_PLUGIN_ROOT}/scripts/verdict.js" }] }]
  }
}
```

`commands/stage.md` (frontmatter + body) records the thesis by shelling `node scripts/stage-write.js` — OR simpler, the command instructs Claude to run a one-liner writing `staged.json`. Define a tiny `scripts/stage-write.js` that reads thesis args from argv/stdin and writes `staged.json` + a `staged` event. Add a matching unit test (thesis persisted).

- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `feat(cc-plugin): hook wiring + /boundary:stage command`.

---

### Task 14: End-to-end fixture

**Files:** Test `test/e2e.test.js`.

- [ ] **Step 1: Write the fixture test** — drive the handlers in sequence against a tmp `CLAUDE_PLUGIN_DATA`:
  `start` → 4× `Read` (4th denied) → `stage-write` → `Write out/a.md` (allow) → `Write bad/x.md` (deny) → `Write out/b.md`, `out/c.md` until cardinality deny → `Bash "curl ..."` (deny) → `verdict`. Assert final verdict: `staging_pivot` PASS, `produced_output` PASS, `writes_inside_allowlist` FAIL (a refused write occurred), overall `FAIL`, and `summary.estimated_dollars` present (feed a synthetic transcript).
- [ ] **Step 2: Run → fail (until all prior tasks done).**
- [ ] **Step 3: Fix any integration gaps surfaced.**
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** `test(cc-plugin): end-to-end session fixture`.

---

### Task 15: README + live-CC validation checklist

**Files:** Create `integrations/claude-code/README.md`.

- [ ] **Step 1:** Write the README: install (drop-in plugin), `.boundary.json` example, what it enforces, and the **explicit non-goals** verbatim from the spec (live spend enforcement / taint / prose-grounding), and that spend appears as a **post-hoc estimate, not a cap**.
- [ ] **Step 2:** Add a `## Validate against a live Claude Code` checklist (manual, cannot be unit-tested):
  - Install the plugin; confirm `PreToolUse` `deny` actually blocks a Write (permission_mode interactions — see spec Risks).
  - Confirm `${CLAUDE_PLUGIN_DATA}` is populated and stable across a session.
  - **Confirm the transcript at `transcript_path` carries per-turn `message.usage` token fields** (the spend-visibility assumption); if the shape differs, adjust `lib/cost.js`'s reader and its test.
  - Confirm `SessionEnd` fires and writes `.boundary/verdict.json`.
- [ ] **Step 3: Commit** `docs(cc-plugin): README + non-goals + live-validation checklist`.

---

## Definition of done

- `node --test integrations/claude-code/test/` is green.
- The e2e fixture asserts a realistic verdict end-to-end.
- README states the non-goals; the live-CC checklist is the one thing that must be run by a human against a real Claude Code before release (esp. the transcript-usage shape).
- Nothing depends on Python; the engine path is gated and tested on both branches with an injected flag.
