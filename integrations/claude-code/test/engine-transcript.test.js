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
