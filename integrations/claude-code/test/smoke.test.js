const { test } = require('node:test');
const assert = require('node:assert');
test('node --test runs', () => { assert.strictEqual(1 + 1, 2); });
