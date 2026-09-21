/**
 * Light pre-scrub applied in the background service worker before a prompt
 * leaves the browser. This is defence in depth only: the local listener runs
 * the full `pii.scrub()` pass server-side and that is what decides what is
 * persisted. Keep this file dependency-free so it can be imported by the
 * service worker and by plain node in extension/scripts/test-prescrub.js.
 */

export const EMAIL_PATTERN = /[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}/g;

/**
 * Token shapes we redact. Ordered most specific first; every one of them maps
 * to the single placeholder [API_KEY].
 *
 * Why these markers are flat and not numbered like the server's
 * ([EMAIL_1], [API_KEY_2], ... - see pii._apply):
 *
 *   - the server cannot renumber or count what we replaced. By the time the
 *     payload reaches `apc serve` the raw value is gone, so a client-scrubbed
 *     email is invisible to `scrub()` and never appears in the record's
 *     `pii_findings`. That is true whatever marker we write.
 *   - writing [EMAIL_1] anyway would be actively wrong: the server numbers
 *     from 1 per record, over the text we hand it. Its api_key patterns are
 *     wider than ours (Bearer ..., password = ..., stripe-style keys), so a
 *     value we missed becomes [API_KEY_1] too - the same placeholder standing
 *     for two different secrets in one prompt.
 *
 * A flat marker cannot collide with [CATEGORY_N], so a reader can always tell
 * which side scrubbed what. The cost, documented in extension/README.md, is
 * that browser captures lose per-value identity (two different addresses both
 * read [EMAIL]) and contribute nothing to `pii_findings`. The listener's scrub
 * is still the one that decides what is persisted.
 */
export const TOKEN_PATTERNS = [
  /sk-ant-[A-Za-z0-9_-]{16,}/g,
  /sk-[A-Za-z0-9_-]{16,}/g,
  /ghp_[A-Za-z0-9]{20,}/g,
  /github_pat_[A-Za-z0-9_]{20,}/g,
  /AKIA[0-9A-Z]{16}/g,
  /xox[abpsr]-[A-Za-z0-9-]{10,}/g,
  /eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g,
];

export const EMAIL_PLACEHOLDER = '[EMAIL]';
export const API_KEY_PLACEHOLDER = '[API_KEY]';

/**
 * Replace emails and obvious API keys/JWTs in free text.
 *
 * @param {unknown} text
 * @returns {string} the scrubbed text ('' for non-string input)
 */
export function preScrub(text) {
  if (typeof text !== 'string' || text.length === 0) return '';
  let out = text;
  // Tokens first: they are the more specific shapes and a JWT payload can
  // otherwise be partially eaten by the email pattern.
  for (const pattern of TOKEN_PATTERNS) {
    pattern.lastIndex = 0;
    out = out.replace(pattern, API_KEY_PLACEHOLDER);
  }
  EMAIL_PATTERN.lastIndex = 0;
  out = out.replace(EMAIL_PATTERN, EMAIL_PLACEHOLDER);
  return out;
}

/**
 * Scrub only the prompt text of a capture payload. `account` is deliberately
 * left intact: the listener needs the real address to run its allowlist check
 * before it aliases/hashes it for storage.
 *
 * @param {Record<string, any>} payload
 * @returns {Record<string, any>} a new payload object
 */
export function preScrubPayload(payload) {
  const next = { ...payload };
  next.prompt = preScrub(payload && payload.prompt);
  return next;
}

export default preScrub;
