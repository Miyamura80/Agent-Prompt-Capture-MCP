/**
 * Byte-aware limits and the loopback rule, shared by the service worker, the
 * options page and the node tests.
 *
 * Keep this file dependency-free (no `chrome.*`): it is imported by plain node
 * in extension/scripts/test-limits.js.
 */

/** The listener refuses a body larger than this (http_listener.MAX_BODY_BYTES). */
export const MAX_BODY_BYTES = 1024 * 1024;

/**
 * Head-room for the JSON envelope (account, url, title, ts, ...) plus the
 * escaping the prompt itself picks up once it is JSON-encoded.
 */
export const BODY_OVERHEAD_BYTES = 16 * 1024;

/** First cut for the prompt text, refined against the real serialized body. */
export const MAX_PROMPT_BYTES = MAX_BODY_BYTES - BODY_OVERHEAD_BYTES;

/** Retry-queue budget. chrome.storage.local gives us 10 MiB in total. */
export const QUEUE_MAX_ITEMS = 500;
export const QUEUE_MAX_BYTES = 4 * 1024 * 1024;

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** UTF-8 size of `text` in bytes (what `fetch` actually puts on the wire). */
export function byteLength(text) {
  return encoder.encode(String(text == null ? '' : text)).length;
}

/**
 * Truncate `text` to at most `maxBytes` UTF-8 bytes without splitting a
 * multi-byte character (a `String.prototype.slice` cap counts UTF-16 code
 * units, which says nothing about the byte size of the request).
 *
 * @param {string} text
 * @param {number} maxBytes
 * @returns {string}
 */
export function truncateToBytes(text, maxBytes) {
  const str = String(text == null ? '' : text);
  if (!(maxBytes > 0)) return '';
  const bytes = encoder.encode(str);
  if (bytes.length <= maxBytes) return str;
  let cut = maxBytes;
  // Walk back off a continuation byte (0b10xxxxxx) so we cut on a boundary.
  while (cut > 0 && (bytes[cut] & 0xc0) === 0x80) cut -= 1;
  return decoder.decode(bytes.subarray(0, cut));
}

/**
 * Keep the newest items that fit both budgets; the oldest are dropped first.
 * Sizes are measured on each item's own JSON so one huge capture cannot push
 * the serialized queue past the storage quota.
 *
 * @template T
 * @param {T[]} items
 * @param {{maxItems?: number, maxBytes?: number}} [budget]
 * @returns {{kept: T[], dropped: number, bytes: number}}
 */
export function trimQueueToBudget(items, budget = {}) {
  const list = Array.isArray(items) ? items : [];
  const maxItems = typeof budget.maxItems === 'number' ? budget.maxItems : QUEUE_MAX_ITEMS;
  const maxBytes = typeof budget.maxBytes === 'number' ? budget.maxBytes : QUEUE_MAX_BYTES;
  const kept = [];
  let bytes = 2; // the enclosing "[]"
  for (let i = list.length - 1; i >= 0 && kept.length < maxItems; i -= 1) {
    let size;
    try {
      size = byteLength(JSON.stringify(list[i])) + 1; // +1 for the comma
    } catch {
      continue; // unserialisable: it could never have been stored anyway
    }
    if (bytes + size > maxBytes) break;
    bytes += size;
    kept.push(list[i]);
  }
  kept.reverse();
  return { kept, dropped: list.length - kept.length, bytes };
}

/**
 * The only destinations this extension may ever contact: the local listener,
 * over plain http, on a loopback name. This mirrors manifest.json's
 * host_permissions and `apc serve`, which binds loopback unless it is started
 * with --allow-remote. Enforced in the worker so a synced or tampered
 * `serverUrl` cannot turn the extension into an exfiltration channel.
 *
 * @param {string} url
 * @returns {boolean}
 */
export function isLoopbackServerUrl(url) {
  let parsed;
  try {
    parsed = new URL(String(url || '').trim());
  } catch {
    return false;
  }
  if (parsed.protocol !== 'http:') return false;
  if (parsed.username || parsed.password) return false;
  const host = parsed.hostname.toLowerCase();
  return host === '127.0.0.1' || host === 'localhost';
}

/**
 * Cap `payload.prompt` so the *serialized request body* stays inside the
 * listener's MAX_BODY_BYTES. A character cap cannot do this: one emoji is four
 * UTF-8 bytes, and JSON escaping inflates newlines, quotes and control
 * characters further, so the measurement has to be made on the encoded body.
 * `truncated: true` is set whenever anything was cut.
 *
 * @param {Record<string, any>} payload
 * @returns {Record<string, any>} a new payload that fits
 */
export function capPayloadBytes(payload) {
  const prompt = String((payload && payload.prompt) || '');
  let budget = MAX_PROMPT_BYTES;
  let out = { ...payload, prompt };
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const cut = truncateToBytes(prompt, budget);
    out = { ...payload, prompt: cut };
    if (cut.length < prompt.length) out.truncated = true;
    const over = byteLength(JSON.stringify(out)) - MAX_BODY_BYTES;
    if (over <= 0) return out;
    budget = Math.max(1024, budget - over - 1024);
  }
  return out;
}

/** Trim whitespace and any trailing slashes; never throws. */
export function normaliseServerUrl(url) {
  return String(url == null ? '' : url)
    .trim()
    .replace(/\/+$/, '');
}

/** Human-readable reason a server URL was rejected, or null when it is fine. */
export function serverUrlProblem(url) {
  const base = normaliseServerUrl(url);
  if (!base) return 'No server URL configured';
  let parsed;
  try {
    parsed = new URL(base);
  } catch {
    return 'That server URL is not a valid URL.';
  }
  if (parsed.protocol !== 'http:') {
    return 'The listener is local-only: the URL must start with http:// (not ' + parsed.protocol + '//).';
  }
  if (!isLoopbackServerUrl(base)) {
    return 'The listener is local-only: the host must be 127.0.0.1 or localhost.';
  }
  return null;
}
