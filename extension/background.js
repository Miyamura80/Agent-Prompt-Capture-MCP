/**
 * Agent Prompt Capture - MV3 background service worker.
 *
 * Responsibilities:
 *   - receive {type:"capture", payload} from the content scripts
 *   - apply the light pre-scrub (defence in depth; the listener does the real one)
 *   - POST to `${serverUrl}/v1/prompts` with the X-APC-Token header
 *   - queue failures in chrome.storage.local (500 items / 4 MiB, drop oldest)
 *     and retry from a 1-minute chrome.alarms tick with exponential backoff
 *   - answer {type:"status"} / {type:"testConnection"} / {type:"clearQueue"}
 *     for the popup and the options page
 *
 * The only origin this worker ever talks to is the configured serverUrl, and
 * that URL must be a loopback http:// listener (lib/limits.isLoopbackServerUrl)
 * - see request(), which refuses anything else before fetch() is reached.
 */

import { preScrubPayload } from './lib/prescrub.js';
import {
  QUEUE_MAX_BYTES,
  QUEUE_MAX_ITEMS,
  capPayloadBytes,
  isLoopbackServerUrl,
  normaliseServerUrl,
  trimQueueToBudget,
} from './lib/limits.js';

export const CLIENT_VERSION = '0.1.0';

const SYNC_DEFAULTS = {
  serverUrl: 'http://127.0.0.1:47821',
  allowedAccounts: [],
  sources: {
    claude_web: true,
    claude_code_web: true,
    chatgpt_web: true,
    codex_cloud: true,
  },
};

const QUEUE_KEY = 'apc_queue';
const CAPTURES_KEY = 'apc_captures';
const LAST_ERROR_KEY = 'apc_last_error';
const TOKEN_KEY = 'token';
const RETRY_ALARM = 'apc-retry';
const DAY_MS = 24 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 8000;
const MAX_BACKOFF_MINUTES = 60;

const VALID_SOURCES = new Set([
  'claude_web',
  'claude_code_web',
  'chatgpt_web',
  'codex_cloud',
]);

/* ------------------------------------------------------------------ config */

export async function getOptions() {
  const stored = await chrome.storage.sync.get(SYNC_DEFAULTS);
  const sources = { ...SYNC_DEFAULTS.sources, ...(stored.sources || {}) };
  const allowedAccounts = Array.isArray(stored.allowedAccounts)
    ? stored.allowedAccounts
        .map((a) => String(a).trim().toLowerCase())
        .filter(Boolean)
    : [];
  return {
    serverUrl: normaliseServerUrl(stored.serverUrl || SYNC_DEFAULTS.serverUrl),
    allowedAccounts,
    sources,
  };
}

export async function getToken() {
  const stored = await chrome.storage.local.get({ [TOKEN_KEY]: '' });
  return String(stored[TOKEN_KEY] || '');
}

/* ------------------------------------------------------------------- http */

async function request(url, { method = 'GET', headers = {}, body } = {}) {
  // The single chokepoint for every outbound call: nothing leaves the worker
  // for anywhere but the local listener, whatever chrome.storage.sync says.
  if (!isLoopbackServerUrl(url)) {
    throw new Error(`refusing to contact ${url || '(no server URL)'}: the listener must be http://127.0.0.1 or http://localhost`);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method,
      headers,
      body,
      signal: controller.signal,
      // Never attach the user's cookies for the local listener.
      credentials: 'omit',
      cache: 'no-store',
    });
    const text = await res.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch {
      json = null;
    }
    return { ok: res.ok, status: res.status, json, text };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * POST one payload. Throws only on a transport/HTTP failure (those are worth a
 * retry). A 2xx is a *delivered* payload even when the listener declined to
 * store it, so the caller gets the body and decides what it means:
 * `{stored: true, id}` is a capture, `{stored: false, reason}` is not
 * (http_listener.do_POST: source_disabled / account_not_allowed / deduped).
 */
async function postPrompt(payload, options, token) {
  if (!options.serverUrl) throw new Error('No server URL configured');
  const res = await request(`${options.serverUrl}/v1/prompts`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-APC-Token': token,
    },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}${res.text ? `: ${res.text.slice(0, 200)}` : ''}`);
  }
  const body = res.json;
  const stored = !!(body && body.stored === true);
  return {
    stored,
    reason: body && typeof body.reason === 'string' ? body.reason : null,
    body,
  };
}

export async function checkHealth(serverUrl) {
  const base = normaliseServerUrl(serverUrl);
  if (!base) return { reachable: false, error: 'No server URL configured' };
  try {
    const res = await request(`${base}/v1/health`);
    if (!res.ok) return { reachable: false, error: `HTTP ${res.status}` };
    return { reachable: true, health: res.json };
  } catch (err) {
    return { reachable: false, error: describeError(err) };
  }
}

async function fetchConfig(serverUrl, token) {
  const base = normaliseServerUrl(serverUrl);
  try {
    const res = await request(`${base}/v1/config`, {
      headers: { 'X-APC-Token': token },
    });
    if (!res.ok) return { ok: false, error: `HTTP ${res.status}` };
    return { ok: true, config: res.json };
  } catch (err) {
    return { ok: false, error: describeError(err) };
  }
}

function describeError(err) {
  if (!err) return 'Unknown error';
  if (err.name === 'AbortError') return 'Timed out';
  return String(err.message || err);
}

/* ------------------------------------------------------------------ state */

async function setLastError(message) {
  await chrome.storage.local.set({
    [LAST_ERROR_KEY]: message ? { message: String(message), ts: new Date().toISOString() } : null,
  });
}

async function getLastError() {
  const stored = await chrome.storage.local.get({ [LAST_ERROR_KEY]: null });
  return stored[LAST_ERROR_KEY] || null;
}

/**
 * Serialise every read-modify-write of the captures list, the same way the
 * queue is serialised: two concurrent captures must not read the same list and
 * write back a single timestamp between them.
 */
let capturesChain = Promise.resolve();

function withCapturesLock(fn) {
  const run = capturesChain.then(() => fn());
  capturesChain = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

/** Add `n` capture timestamps (only ever called for `stored: true` records). */
async function recordCaptures(n) {
  if (!(n > 0)) return countLast24h();
  return withCapturesLock(async () => {
    const stored = await chrome.storage.local.get({ [CAPTURES_KEY]: [] });
    const cutoff = Date.now() - DAY_MS;
    const list = (Array.isArray(stored[CAPTURES_KEY]) ? stored[CAPTURES_KEY] : [])
      .filter((t) => typeof t === 'number' && t >= cutoff);
    for (let i = 0; i < n; i += 1) list.push(Date.now());
    await chrome.storage.local.set({ [CAPTURES_KEY]: list });
    return list.length;
  });
}

async function countLast24h() {
  const stored = await chrome.storage.local.get({ [CAPTURES_KEY]: [] });
  const cutoff = Date.now() - DAY_MS;
  return (Array.isArray(stored[CAPTURES_KEY]) ? stored[CAPTURES_KEY] : [])
    .filter((t) => typeof t === 'number' && t >= cutoff).length;
}

/* ------------------------------------------------------------------ queue */

/**
 * Every queue read-modify-write goes through this one promise chain. The
 * worker is single-threaded but `await` is not: a capture and the alarm's
 * flush could otherwise both read the same array and write back a version that
 * silently loses the other's item.
 */
let queueChain = Promise.resolve();

function withQueueLock(fn) {
  const run = queueChain.then(() => fn());
  queueChain = run.then(
    () => undefined,
    () => undefined,
  );
  return run;
}

async function readQueue() {
  const stored = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
  return Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
}

/**
 * Persist the queue inside both budgets (500 items AND 4 MiB of serialized
 * JSON, so 500 near-1 MiB prompts cannot blow the 10 MiB storage.local quota),
 * and treat a quota rejection as "drop the oldest half and try again" rather
 * than as a lost capture.
 *
 * When even an empty queue cannot be written the call reports `persisted:
 * false` and hands back the queue that is *actually* stored (whatever was there
 * before, untouched). It must never claim the queue is now empty: the caller
 * would treat the items it meant to remove as removed while storage still holds
 * them, and the next alarm would post them all over again.
 *
 * @returns {Promise<{queue: object[], dropped: number, persisted: boolean, error: string|null}>}
 */
export async function writeQueue(queue) {
  const trimmed = trimQueueToBudget(queue, {
    maxItems: QUEUE_MAX_ITEMS,
    maxBytes: QUEUE_MAX_BYTES,
  });
  let candidate = trimmed.kept;
  let dropped = trimmed.dropped;
  let lastErr = null;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      await chrome.storage.local.set({ [QUEUE_KEY]: candidate });
      return { queue: candidate, dropped, persisted: true, error: null };
    } catch (err) {
      // QUOTA_BYTES / QUOTA_BYTES_PER_ITEM, or the profile's disk is full.
      lastErr = err;
      if (candidate.length === 0) break;
      const half = Math.max(1, Math.floor(candidate.length / 2));
      dropped += half;
      candidate = candidate.slice(half); // the oldest go first
    }
  }
  // Nothing was written, so nothing was dropped either: storage still holds the
  // previous queue. Report that queue, not an imaginary empty one.
  const error = lastErr ? describeError(lastErr) : 'storage quota exceeded';
  await setLastError(`Could not persist the retry queue: ${error}`);
  let stored = [];
  try {
    stored = await readQueue();
  } catch {
    stored = [];
  }
  return { queue: stored, dropped: 0, persisted: false, error };
}

export function backoffMs(attempts) {
  const minutes = Math.min(MAX_BACKOFF_MINUTES, Math.pow(2, Math.max(0, attempts - 1)));
  return minutes * 60 * 1000;
}

/**
 * Queue a payload the POST could not deliver. The caller owns `apc_last_error`:
 * it already has a transport error to report, and a drop warning raised here
 * would only be overwritten by it (the user would never learn that older
 * captures were thrown away), so the facts are returned instead of stored.
 */
async function enqueue(payload) {
  const result = await withQueueLock(async () => {
    const queue = await readQueue();
    queue.push({ payload, attempts: 1, nextAt: Date.now() + backoffMs(1) });
    return writeQueue(queue);
  });
  await ensureRetryAlarm();
  return result;
}

/**
 * Why an item may no longer be posted. The worker's allowlist checks are not
 * only for fresh captures: a queued prompt belongs to a source or an account
 * the user may have turned off *after* the POST failed, and replaying it then
 * would send data the current settings forbid.
 */
function queueItemProblem(payload, options) {
  const problem = validatePayload(payload);
  if (problem) return problem;
  if (options.sources[payload.source] === false) return 'source_disabled';
  const account = String(payload.account).trim().toLowerCase();
  if (options.allowedAccounts.length === 0 || !options.allowedAccounts.includes(account)) {
    return 'account_not_allowed';
  }
  return null;
}

export async function flushQueue() {
  return withQueueLock(async () => {
    let queue = await readQueue();
    if (queue.length === 0) return { sent: 0, stored: 0, dropped: 0, remaining: 0 };

    const options = await getOptions();
    if (!isLoopbackServerUrl(options.serverUrl)) {
      // Nothing may be posted anywhere else, and retrying would never succeed.
      const message = `Server URL ${options.serverUrl || '(unset)'} is not a local listener; not retrying`;
      await setLastError(message);
      return { sent: 0, stored: 0, dropped: 0, remaining: queue.length, error: message };
    }
    const token = await getToken();
    const now = Date.now();
    const keep = [];
    let sent = 0;
    let stored = 0;
    let dropped = 0;
    let lastError = null;

    for (const item of queue) {
      if (!item || !item.payload) {
        dropped += 1;
        continue;
      }
      // Re-check against the settings as they are *now*, not as they were when
      // the capture was queued.
      if (queueItemProblem(item.payload, options)) {
        dropped += 1;
        continue;
      }
      if (typeof item.nextAt === 'number' && item.nextAt > now) {
        keep.push(item);
        continue;
      }
      try {
        const result = await postPrompt(item.payload, options, token);
        // Delivered: dequeue either way. Only a stored record is a capture.
        sent += 1;
        if (result.stored) stored += 1;
      } catch (err) {
        lastError = describeError(err);
        const attempts = (item.attempts || 0) + 1;
        keep.push({ ...item, attempts, nextAt: Date.now() + backoffMs(attempts) });
      }
    }

    const written = await writeQueue(keep);
    if (!written.persisted) {
      // The replacement queue never reached storage, so the queue on disk is
      // still the one we started from - delivered items included. Counting them
      // as sent (and as captures) here is what made the next alarm re-post
      // prompts the listener already had, so count nothing and say why.
      await setLastError(
        `Could not persist the retry queue: ${written.error}; ${sent} delivered item(s) stay queued`,
      );
      return {
        sent: 0,
        stored: 0,
        dropped: 0,
        remaining: written.queue.length,
        persisted: false,
        error: written.error,
      };
    }
    queue = written.queue;
    dropped += written.dropped;
    if (stored > 0) await recordCaptures(stored);
    if (lastError) await setLastError(`Retry failed: ${lastError}`);
    else if (sent > 0) await setLastError(null);
    return { sent, stored, dropped, remaining: queue.length, persisted: true };
  });
}

async function ensureRetryAlarm() {
  try {
    const existing = await chrome.alarms.get(RETRY_ALARM);
    if (!existing) {
      await chrome.alarms.create(RETRY_ALARM, { periodInMinutes: 1, delayInMinutes: 1 });
    }
  } catch {
    // alarms unavailable (should not happen with the "alarms" permission)
  }
}

/* ---------------------------------------------------------------- capture */

function validatePayload(payload) {
  if (!payload || typeof payload !== 'object') return 'payload missing';
  if (typeof payload.prompt !== 'string' || payload.prompt.trim() === '') return 'empty prompt';
  if (!VALID_SOURCES.has(payload.source)) return `unknown source ${payload.source}`;
  if (typeof payload.account !== 'string' || payload.account.trim() === '') return 'unknown account';
  return null;
}

export async function handleCapture(rawPayload) {
  const problem = validatePayload(rawPayload);
  if (problem) return { ok: false, reason: problem };

  const options = await getOptions();
  const account = String(rawPayload.account).trim().toLowerCase();

  if (!isLoopbackServerUrl(options.serverUrl)) {
    // Never queue this: the destination itself is the problem, and a queued
    // payload would just keep trying to leave the machine.
    const message = `Server URL ${options.serverUrl || '(unset)'} is not a local listener (http://127.0.0.1 or http://localhost)`;
    await setLastError(message);
    return { ok: false, reason: 'server_not_loopback', error: message };
  }
  if (options.sources[rawPayload.source] === false) {
    return { ok: false, reason: 'source_disabled' };
  }
  // Belt and braces: the content script already checked, the listener checks again.
  if (options.allowedAccounts.length === 0 || !options.allowedAccounts.includes(account)) {
    return { ok: false, reason: 'account_not_allowed' };
  }

  let payload = preScrubPayload(rawPayload);
  payload.account = account;
  payload.client_version = CLIENT_VERSION;
  if (!payload.ts) payload.ts = new Date().toISOString();
  payload = capPayloadBytes(payload);

  const token = await getToken();
  try {
    const result = await postPrompt(payload, options, token);
    // A 202 with `stored: false` (deduped, disabled, not allowlisted) is a
    // delivered payload that is NOT a capture: it must not raise last24h.
    if (result.stored) await recordCaptures(1);
    await setLastError(null);
    return {
      ok: true,
      queued: false,
      stored: result.stored,
      reason: result.reason,
      response: result.body,
    };
  } catch (err) {
    const message = describeError(err);
    const queued = await enqueue(payload);
    // Both facts belong in the one slot the popup reads: the POST failed, AND
    // the queue may have dropped older captures (or refused the new one). The
    // transport error used to overwrite the drop warning, so a capture that was
    // gone for good was reported as a retry that had not happened yet.
    const notes = [];
    if (queued.dropped > 0) notes.push(`retry queue full: dropped ${queued.dropped} older capture(s)`);
    if (!queued.persisted) notes.push(`this capture could not be queued: ${queued.error}`);
    await setLastError(notes.length ? `${message} (${notes.join('; ')})` : message);
    return { ok: false, queued: !!queued.persisted, dropped: queued.dropped, error: message };
  }
}

/* --------------------------------------------------------------- messages */

async function buildStatus() {
  const options = await getOptions();
  const token = await getToken();
  const health = await checkHealth(options.serverUrl);
  const queue = await readQueue();
  return {
    ok: true,
    serverUrl: options.serverUrl,
    hasToken: token.length > 0,
    allowedAccounts: options.allowedAccounts,
    sources: options.sources,
    reachable: health.reachable,
    health: health.health || null,
    healthError: health.error || null,
    last24h: await countLast24h(),
    queued: queue.length,
    lastError: await getLastError(),
    clientVersion: CLIENT_VERSION,
  };
}

async function testConnection() {
  const options = await getOptions();
  const token = await getToken();
  const health = await checkHealth(options.serverUrl);
  if (!health.reachable) {
    return { ok: false, serverUrl: options.serverUrl, reachable: false, error: health.error };
  }
  const config = await fetchConfig(options.serverUrl, token);
  return {
    ok: config.ok,
    serverUrl: options.serverUrl,
    reachable: true,
    health: health.health || null,
    config: config.config || null,
    error: config.ok ? null : config.error,
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || typeof message.type !== 'string') return undefined;

  switch (message.type) {
    case 'capture':
      handleCapture(message.payload).then(sendResponse, (err) =>
        sendResponse({ ok: false, error: describeError(err) }),
      );
      return true;
    case 'status':
      buildStatus().then(sendResponse, (err) =>
        sendResponse({ ok: false, error: describeError(err) }),
      );
      return true;
    case 'testConnection':
      testConnection().then(sendResponse, (err) =>
        sendResponse({ ok: false, error: describeError(err) }),
      );
      return true;
    case 'clearQueue':
      withQueueLock(async () => {
        await chrome.storage.local.set({ [QUEUE_KEY]: [] });
        await setLastError(null);
        return { ok: true, queued: 0 };
      }).then(sendResponse, (err) => sendResponse({ ok: false, error: describeError(err) }));
      return true;
    case 'flushQueue':
      flushQueue().then(
        (r) => sendResponse({ ok: true, ...r }),
        (err) => sendResponse({ ok: false, error: describeError(err) }),
      );
      return true;
    default:
      return undefined;
  }
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm && alarm.name === RETRY_ALARM) {
    flushQueue().catch(() => {});
  }
});

chrome.runtime.onInstalled.addListener(() => {
  ensureRetryAlarm().catch(() => {});
});

chrome.runtime.onStartup.addListener(() => {
  ensureRetryAlarm().catch(() => {});
});

// The worker can be spun up by any event; make sure the retry tick exists.
ensureRetryAlarm().catch(() => {});
