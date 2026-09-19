/**
 * Unit test for the background worker's pre-scrub.
 *
 *   node extension/scripts/test-prescrub.js
 *
 * Plain node:test-free assertions so this runs anywhere node 22 runs.
 */

import assert from 'node:assert/strict';
import { preScrub, preScrubPayload } from '../lib/prescrub.js';

let passed = 0;
let failed = 0;

function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${err.message}`);
  }
}

/* ------------------------------------------------------------------ emails */

check('replaces a plain email', () => {
  assert.equal(preScrub('mail me at alice@example.com ok?'), 'mail me at [EMAIL] ok?');
});

check('replaces several emails and plus-addressing', () => {
  assert.equal(
    preScrub('a.b+tag@sub.example.co.uk and c@d.io'),
    '[EMAIL] and [EMAIL]',
  );
});

check('leaves a bare handle without a TLD alone', () => {
  assert.equal(preScrub('ping @alice or foo@bar'), 'ping @alice or foo@bar');
});

/* ------------------------------------------------------------- API tokens */

check('redacts an OpenAI-style key', () => {
  assert.equal(preScrub('key=sk-abcdefghij0123456789ABCD'), 'key=[API_KEY]');
});

check('redacts an Anthropic-style key', () => {
  assert.equal(
    preScrub('ANTHROPIC_API_KEY=sk-ant-api03-AAAAbbbbCCCCddddEEEEffff'),
    'ANTHROPIC_API_KEY=[API_KEY]',
  );
});

check('redacts a GitHub PAT (classic)', () => {
  assert.equal(preScrub('token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123'), 'token [API_KEY]');
});

check('redacts a GitHub fine-grained PAT', () => {
  assert.equal(
    preScrub('github_pat_11ABCDEFG0aBcDeFgHiJkLmNoPqRsTuVwXyZ_0123'),
    '[API_KEY]',
  );
});

check('redacts an AWS access key id', () => {
  assert.equal(preScrub('AKIAIOSFODNN7EXAMPLE is mine'), '[API_KEY] is mine');
});

check('redacts a Slack token', () => {
  assert.equal(preScrub('xoxb-1234567890-abcdefghij'), '[API_KEY]');
});

check('redacts a JWT', () => {
  const jwt =
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk';
  assert.equal(preScrub(`Authorization: Bearer ${jwt}`), 'Authorization: Bearer [API_KEY]');
});

check('redacts a token and an email in one pass', () => {
  assert.equal(
    preScrub('me@work.com uses sk-abcdefghij0123456789ABCD'),
    '[EMAIL] uses [API_KEY]',
  );
});

/* --------------------------------------------------------------- non-hits */

check('leaves ordinary prose and code alone', () => {
  const text = 'Refactor `parse_config()` in src/config.py so version 1.2.3-beta still loads.';
  assert.equal(preScrub(text), text);
});

check('leaves a short sk- word alone', () => {
  assert.equal(preScrub('sk-short'), 'sk-short');
});

check('preserves line breaks', () => {
  assert.equal(preScrub('line one\nline two\n\nline four'), 'line one\nline two\n\nline four');
});

/* ------------------------------------------------------------- edge cases */

check('handles non-string input', () => {
  assert.equal(preScrub(undefined), '');
  assert.equal(preScrub(null), '');
  assert.equal(preScrub(42), '');
  assert.equal(preScrub(''), '');
});

check('is idempotent', () => {
  const once = preScrub('a@b.com sk-abcdefghij0123456789ABCD');
  assert.equal(preScrub(once), once);
});

check('regexes are not left stateful between calls', () => {
  const text = 'x@y.com';
  assert.equal(preScrub(text), '[EMAIL]');
  assert.equal(preScrub(text), '[EMAIL]');
  assert.equal(preScrub(text), '[EMAIL]');
});

/* ---------------------------------------------------------------- payload */

check('preScrubPayload scrubs the prompt but keeps the account intact', () => {
  const payload = {
    source: 'chatgpt_web',
    prompt: 'email me@work.com about sk-abcdefghij0123456789ABCD',
    account: 'me@work.com',
    conversation_id: 'abc',
    url: 'https://chatgpt.com/c/abc',
    title: 'Some chat',
    ts: '2026-09-19T20:11:03.123Z',
    client_version: '0.1.0',
  };
  const out = preScrubPayload(payload);
  assert.equal(out.prompt, 'email [EMAIL] about [API_KEY]');
  assert.equal(out.account, 'me@work.com', 'the server needs the real address for the allowlist');
  assert.equal(out.source, 'chatgpt_web');
  assert.equal(out.url, 'https://chatgpt.com/c/abc');
  assert.equal(payload.prompt, 'email me@work.com about sk-abcdefghij0123456789ABCD', 'input untouched');
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
