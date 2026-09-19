/**
 * Agent Prompt Capture - MV3 background service worker.
 *
 * Responsibilities:
 *   - receive {type:"capture", payload} from the content scripts
 *   - apply the light pre-scrub (defence in depth; the listener does the real one)
 *   - POST to `${serverUrl}/v1/prompts` with the X-APC-Token header
 *   - queue failures in chrome.storage.local (cap 500, drop oldest) and retry
 *     from a 1-minute chrome.alarms tick with exponential backoff
 *   - answer {type:"status"} / {type:"testConnection"} / {type:"clearQueue"}
 *     for the popup and the options page
 *
 * The only origin this worker ever talks to is the configured serverUrl.
 */

import { preScrubPayload } from './lib/prescrub.js';

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
const QUEUE_MAX = 500;
const CAPTURES_KEY = 'apc_captures';
const LAST_ERROR_KEY = 'apc_last_error';
const TOKEN_KEY = 'token';
const RETRY_ALARM = 'apc-retry';
const DAY_MS = 24 * 60 * 60 * 1000;
const REQUEST_TIMEOUT_MS = 8000;
const MAX_BACKOFF_MINUTES = 60;
const MAX_PROMPT_CHARS = 500000; // keeps us well under the listener's 1 MiB cap

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

function normaliseServerUrl(url) {
  return String(url || '').trim().replace(/\/+$/, '');
}

/* ------------------------------------------------------------------- http */

async function request(url, { method = 'GET', headers = {}, body } = {}) {
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
  return res.json;
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

async function recordCapture() {
  const stored = await chrome.storage.local.get({ [CAPTURES_KEY]: [] });
  const cutoff = Date.now() - DAY_MS;
  const list = (Array.isArray(stored[CAPTURES_KEY]) ? stored[CAPTURES_KEY] : [])
    .filter((t) => typeof t === 'number' && t >= cutoff);
  list.push(Date.now());
  await chrome.storage.local.set({ [CAPTURES_KEY]: list });
  return list.length;
}

async function countLast24h() {
  const stored = await chrome.storage.local.get({ [CAPTURES_KEY]: [] });
  const cutoff = Date.now() - DAY_MS;
  return (Array.isArray(stored[CAPTURES_KEY]) ? stored[CAPTURES_KEY] : [])
    .filter((t) => typeof t === 'number' && t >= cutoff).length;
}

/* ------------------------------------------------------------------ queue */

async function readQueue() {
  const stored = await chrome.storage.local.get({ [QUEUE_KEY]: [] });
  return Array.isArray(stored[QUEUE_KEY]) ? stored[QUEUE_KEY] : [];
}

async function writeQueue(queue) {
  await chrome.storage.local.set({ [QUEUE_KEY]: queue.slice(-QUEUE_MAX) });
}

export function backoffMs(attempts) {
  const minutes = Math.min(MAX_BACKOFF_MINUTES, Math.pow(2, Math.max(0, attempts - 1)));
  return minutes * 60 * 1000;
}

async function enqueue(payload) {
  const queue = await readQueue();
  queue.push({ payload, attempts: 1, nextAt: Date.now() + backoffMs(1) });
  // cap 500, drop oldest
  await writeQueue(queue);
  await ensureRetryAlarm();
}

async function flushQueue() {
  let queue = await readQueue();
  if (queue.length === 0) return { sent: 0, remaining: 0 };

  const options = await getOptions();
  const token = await getToken();
  const now = Date.now();
  const keep = [];
  let sent = 0;
  let lastError = null;

  for (const item of queue) {
    if (!item || !item.payload) continue;
    if (typeof item.nextAt === 'number' && item.nextAt > now) {
      keep.push(item);
      continue;
    }
    try {
      await postPrompt(item.payload, options, token);
      sent += 1;
    } catch (err) {
      lastError = describeError(err);
      const attempts = (item.attempts || 0) + 1;
      keep.push({ ...item, attempts, nextAt: Date.now() + backoffMs(attempts) });
    }
  }

  queue = keep;
  await writeQueue(queue);
  if (sent > 0) {
    const stored = await chrome.storage.local.get({ [CAPTURES_KEY]: [] });
    const cutoff = Date.now() - DAY_MS;
    const list = (Array.isArray(stored[CAPTURES_KEY]) ? stored[CAPTURES_KEY] : [])
      .filter((t) => typeof t === 'number' && t >= cutoff);
    for (let i = 0; i < sent; i += 1) list.push(Date.now());
    await chrome.storage.local.set({ [CAPTURES_KEY]: list });
  }
  if (lastError) await setLastError(`Retry failed: ${lastError}`);
  else if (sent > 0) await setLastError(null);
  return { sent, remaining: queue.length };
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
  if (payload.prompt.length > MAX_PROMPT_CHARS) {
    payload.prompt = payload.prompt.slice(0, MAX_PROMPT_CHARS);
    payload.truncated = true;
  }

  const token = await getToken();
  try {
    const body = await postPrompt(payload, options, token);
    await recordCapture();
    await setLastError(null);
    return { ok: true, queued: false, response: body };
  } catch (err) {
    const message = describeError(err);
    await enqueue(payload);
    await setLastError(message);
    return { ok: false, queued: true, error: message };
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
      (async () => {
        await chrome.storage.local.set({ [QUEUE_KEY]: [] });
        await setLastError(null);
        return { ok: true, queued: 0 };
      })().then(sendResponse, (err) => sendResponse({ ok: false, error: describeError(err) }));
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
