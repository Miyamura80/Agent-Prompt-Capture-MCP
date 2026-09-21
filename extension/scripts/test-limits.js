/**
 * Unit tests for the worker's byte budgets and the loopback rule.
 *
 *   node extension/scripts/test-limits.js
 *
 * Same style as test-prescrub.js: plain node, no test runner needed.
 */

import assert from 'node:assert/strict';
import {
  MAX_BODY_BYTES,
  byteLength,
  capPayloadBytes,
  isLoopbackServerUrl,
  normaliseServerUrl,
  serverUrlProblem,
  trimQueueToBudget,
  truncateToBytes,
} from '../lib/limits.js';

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

/* -------------------------------------------------------------- byteLength */

check('byteLength counts UTF-8 bytes, not code units', () => {
  assert.equal(byteLength('abc'), 3);
  assert.equal(byteLength('é'), 2);
  assert.equal(byteLength('😀'), 4); // 2 UTF-16 code units, 4 bytes
  assert.equal(byteLength(''), 0);
  assert.equal(byteLength(null), 0);
});

/* ---------------------------------------------------------- truncateToBytes */

check('truncateToBytes leaves a short string alone', () => {
  assert.equal(truncateToBytes('hello', 10), 'hello');
});

check('truncateToBytes caps on the byte size', () => {
  const out = truncateToBytes('abcdefghij', 4);
  assert.equal(out, 'abcd');
  assert.ok(byteLength(out) <= 4);
});

check('truncateToBytes never splits a multi-byte character', () => {
  // 5 emoji = 20 bytes; a 4-byte character cannot be cut in half.
  const text = '😀😀😀😀😀';
  for (let limit = 0; limit <= 20; limit += 1) {
    const out = truncateToBytes(text, limit);
    assert.ok(byteLength(out) <= limit, `limit ${limit} produced ${byteLength(out)} bytes`);
    assert.ok(!out.includes('�'), `limit ${limit} produced a replacement character`);
    assert.equal(out, '😀'.repeat(Math.floor(limit / 4)));
  }
});

check('truncateToBytes is byte-safe where a character cap would not be', () => {
  // 1000 emoji is 1000 UTF-16-based "characters" but 4000 bytes.
  const text = '😀'.repeat(1000);
  assert.equal(text.length, 2000);
  assert.equal(byteLength(text), 4000);
  assert.ok(byteLength(truncateToBytes(text, 1000)) <= 1000);
});

check('truncateToBytes handles a zero/negative budget', () => {
  assert.equal(truncateToBytes('abc', 0), '');
  assert.equal(truncateToBytes('abc', -5), '');
});

/* -------------------------------------------------------- trimQueueToBudget */

check('trimQueueToBudget keeps everything that fits', () => {
  const items = [{ a: 1 }, { a: 2 }, { a: 3 }];
  const out = trimQueueToBudget(items, { maxItems: 10, maxBytes: 1024 });
  assert.deepEqual(out.kept, items);
  assert.equal(out.dropped, 0);
});

check('trimQueueToBudget drops the oldest when over the item cap', () => {
  const items = [1, 2, 3, 4, 5].map((n) => ({ n }));
  const out = trimQueueToBudget(items, { maxItems: 3, maxBytes: 1024 });
  assert.deepEqual(
    out.kept.map((i) => i.n),
    [3, 4, 5],
  );
  assert.equal(out.dropped, 2);
});

check('trimQueueToBudget drops the oldest when over the byte budget', () => {
  // 20 items of ~1 KiB each against a 5 KiB budget.
  const items = [];
  for (let i = 0; i < 20; i += 1) items.push({ i, payload: { prompt: 'x'.repeat(1024) } });
  const out = trimQueueToBudget(items, { maxItems: 500, maxBytes: 5 * 1024 });
  assert.ok(out.kept.length > 0 && out.kept.length < 20, `kept ${out.kept.length}`);
  assert.ok(out.bytes <= 5 * 1024, `bytes ${out.bytes}`);
  assert.equal(out.kept[out.kept.length - 1].i, 19, 'the newest capture is always kept');
  assert.equal(out.dropped, 20 - out.kept.length);
});

check('trimQueueToBudget keeps 500 max-size prompts under the storage quota', () => {
  // The regression: 500 x ~1 MiB would be ~500 MiB, 50x chrome.storage.local.
  const item = { payload: { prompt: 'x'.repeat(MAX_BODY_BYTES - 1024) }, attempts: 1 };
  const items = [];
  for (let i = 0; i < 500; i += 1) items.push(item);
  const out = trimQueueToBudget(items);
  assert.ok(out.bytes <= 4 * 1024 * 1024, `queue would be ${out.bytes} bytes`);
  assert.ok(out.kept.length >= 1, 'at least the newest capture survives');
});

check('trimQueueToBudget copes with an empty or bogus queue', () => {
  assert.deepEqual(trimQueueToBudget([]).kept, []);
  assert.deepEqual(trimQueueToBudget(null).kept, []);
});

/* --------------------------------------------------------- capPayloadBytes */

function samplePayload(prompt) {
  return {
    source: 'chatgpt_web',
    prompt,
    account: 'me@work.com',
    conversation_id: '11111111-2222-3333-4444-555555555555',
    url: 'https://chatgpt.com/c/11111111-2222-3333-4444-555555555555',
    title: 'Some chat',
    ts: '2026-09-19T20:11:03.123Z',
    client_version: '0.1.0',
  };
}

check('capPayloadBytes leaves a normal prompt untouched', () => {
  const payload = samplePayload('summarise the changelog');
  const out = capPayloadBytes(payload);
  assert.equal(out.prompt, 'summarise the changelog');
  assert.equal(out.truncated, undefined);
});

check('capPayloadBytes keeps the serialized body under the listener cap', () => {
  const payload = samplePayload('x'.repeat(4 * 1024 * 1024));
  const out = capPayloadBytes(payload);
  assert.equal(out.truncated, true);
  assert.ok(byteLength(JSON.stringify(out)) <= MAX_BODY_BYTES, 'body still too large');
});

check('capPayloadBytes accounts for multi-byte characters', () => {
  // 600k emoji = 2.4 MB: a 500k-character cap would have sent ~2 MB.
  const out = capPayloadBytes(samplePayload('😀'.repeat(600000)));
  assert.equal(out.truncated, true);
  assert.ok(byteLength(JSON.stringify(out)) <= MAX_BODY_BYTES, 'body still too large');
  assert.ok(!out.prompt.includes('\ufffd'), 'a character was cut in half');
});

check('capPayloadBytes accounts for JSON escaping', () => {
  // Every newline becomes two bytes (\n) once serialized.
  const out = capPayloadBytes(samplePayload('\n'.repeat(900 * 1024)));
  assert.ok(byteLength(JSON.stringify(out)) <= MAX_BODY_BYTES, 'body still too large');
});

/* ------------------------------------------------------------ loopback rule */

check('loopback http URLs are accepted', () => {
  assert.equal(isLoopbackServerUrl('http://127.0.0.1:47821'), true);
  assert.equal(isLoopbackServerUrl('http://localhost:47821'), true);
  assert.equal(isLoopbackServerUrl('http://127.0.0.1'), true);
  assert.equal(isLoopbackServerUrl('http://localhost:47821/v1/prompts'), true);
});

check('everything else is refused', () => {
  assert.equal(isLoopbackServerUrl('https://127.0.0.1:47821'), false, 'https is not what apc serve speaks');
  assert.equal(isLoopbackServerUrl('http://example.com'), false);
  assert.equal(isLoopbackServerUrl('https://example.com'), false);
  assert.equal(isLoopbackServerUrl('http://127.0.0.1.evil.com'), false, 'a suffixed host is not loopback');
  assert.equal(isLoopbackServerUrl('http://localhost.evil.com'), false);
  assert.equal(isLoopbackServerUrl('http://user:pw@127.0.0.1'), false);
  assert.equal(isLoopbackServerUrl('ftp://127.0.0.1'), false);
  assert.equal(isLoopbackServerUrl('127.0.0.1:47821'), false, 'no scheme is not a URL');
  assert.equal(isLoopbackServerUrl(''), false);
  assert.equal(isLoopbackServerUrl(null), false);
});

check('serverUrlProblem explains each rejection and passes loopback', () => {
  assert.equal(serverUrlProblem('http://127.0.0.1:47821'), null);
  assert.equal(serverUrlProblem('http://localhost:47821/'), null);
  assert.ok(serverUrlProblem(''));
  assert.ok(serverUrlProblem('not a url'));
  assert.ok(/http:\/\//.test(serverUrlProblem('https://127.0.0.1:47821')));
  assert.ok(/127\.0\.0\.1 or localhost/.test(serverUrlProblem('http://example.com')));
});

check('normaliseServerUrl trims whitespace and trailing slashes', () => {
  assert.equal(normaliseServerUrl('  http://127.0.0.1:47821//  '), 'http://127.0.0.1:47821');
  assert.equal(normaliseServerUrl(null), '');
});

/* ------------------------------------------- the worker's queue durability */

/*
 * background.js is a service-worker module: importing it needs a `chrome` to
 * exist first (it registers listeners and warms the retry alarm at load).
 * This fake is just enough of the storage/alarms API, plus a switch that makes
 * writes to one key fail the way QUOTA_BYTES_PER_ITEM does.
 */
function fakeChrome() {
  const local = new Map();
  const sync = new Map();
  const failKeys = new Set();
  const area = (map) => ({
    async get(defaults) {
      const wanted = typeof defaults === 'string' ? { [defaults]: undefined } : defaults || {};
      const out = {};
      for (const key of Object.keys(wanted)) out[key] = map.has(key) ? map.get(key) : wanted[key];
      return out;
    },
    async set(items) {
      for (const key of Object.keys(items)) {
        if (failKeys.has(key)) throw new Error('QUOTA_BYTES_PER_ITEM quota exceeded');
      }
      for (const [key, value] of Object.entries(items)) map.set(key, value);
    },
  });
  const noop = { addListener() {} };
  return {
    local,
    sync,
    failKeys,
    api: {
      storage: { local: area(local), sync: area(sync) },
      alarms: { async get() { return null; }, async create() {}, onAlarm: noop },
      runtime: { onMessage: noop, onInstalled: noop, onStartup: noop },
    },
  };
}

const fake = fakeChrome();
globalThis.chrome = fake.api;
// Every POST succeeds; only storage is broken in these tests.
globalThis.fetch = async () => ({
  ok: true,
  status: 202,
  async text() {
    return JSON.stringify({ stored: true, id: 'id-1' });
  },
});

const bg = await import('../background.js');

function queueItem(prompt) {
  return {
    payload: {
      source: 'chatgpt_web',
      prompt,
      account: 'me@work.com',
      url: 'https://chatgpt.com/c/x',
      ts: '2026-09-19T20:11:03.123Z',
    },
    attempts: 1,
    nextAt: Date.now() - 1000,
  };
}

function resetStorage(queue) {
  fake.failKeys.clear();
  fake.local.clear();
  fake.sync.clear();
  fake.local.set('apc_queue', queue);
  fake.local.set('apc_captures', []);
  fake.local.set('token', 'test-token');
  fake.sync.set('serverUrl', 'http://127.0.0.1:47821');
  fake.sync.set('allowedAccounts', ['me@work.com']);
  fake.sync.set('sources', { claude_web: true, claude_code_web: true, chatgpt_web: true, codex_cloud: true });
}

async function checkAsync(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`ok   ${name}`);
  } catch (err) {
    failed += 1;
    console.error(`FAIL ${name}\n     ${err.message}`);
  }
}

await checkAsync('writeQueue reports a failed persist instead of claiming an empty queue', async () => {
  const stored = [queueItem('already queued')];
  resetStorage(stored);
  fake.failKeys.add('apc_queue');

  const out = await bg.writeQueue([]);
  assert.equal(out.persisted, false, 'a write that never landed must not report success');
  assert.equal(out.dropped, 0, 'nothing was written, so nothing was dropped');
  assert.deepEqual(out.queue, stored, 'the caller must be told what is actually stored');
  assert.deepEqual(fake.local.get('apc_queue'), stored, 'storage must be left as it was');
});

await checkAsync('writeQueue reports success when the write lands', async () => {
  resetStorage([queueItem('already queued')]);
  const out = await bg.writeQueue([]);
  assert.equal(out.persisted, true);
  assert.deepEqual(fake.local.get('apc_queue'), []);
});

await checkAsync('flushQueue dequeues and counts a delivery that was persisted', async () => {
  resetStorage([queueItem('deliver me')]);
  const out = await bg.flushQueue();
  assert.equal(out.sent, 1);
  assert.equal(out.stored, 1);
  assert.equal(out.remaining, 0);
  assert.deepEqual(fake.local.get('apc_queue'), []);
  assert.equal(fake.local.get('apc_captures').length, 1);
});

await checkAsync('flushQueue counts nothing when the shorter queue could not be written', async () => {
  // The regression: the delivered item stayed in storage while flushQueue
  // reported it as dequeued, so the next alarm posted it all over again.
  const stored = [queueItem('deliver me')];
  resetStorage(stored);
  fake.failKeys.add('apc_queue');

  const out = await bg.flushQueue();
  assert.equal(out.persisted, false);
  assert.equal(out.sent, 0, 'an item still on disk has not been dequeued');
  assert.equal(out.stored, 0, 'and must not be counted as a capture');
  assert.equal(out.remaining, 1);
  assert.deepEqual(fake.local.get('apc_queue'), stored, 'the queue on disk is unchanged');
  assert.equal(fake.local.get('apc_captures').length, 0);
  assert.ok(
    /could not persist/i.test(String(fake.local.get('apc_last_error').message)),
    'the popup must be told the queue could not be written',
  );
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
