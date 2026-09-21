/**
 * Agent Prompt Capture - shared content-script helpers.
 *
 * Loaded before content/claude.js and content/chatgpt.js. Classic script (not
 * a module): everything hangs off globalThis.APC and there is no top-level
 * await anywhere in this file.
 *
 * Hard rules enforced here:
 *   - we never call preventDefault() or stopPropagation(); the page behaves
 *     exactly as it would without the extension
 *   - we never send anything anywhere ourselves; the background worker owns
 *     the single network destination (the configured serverUrl)
 *   - nothing is captured when the account is unknown or not allowlisted
 */
(function () {
  'use strict';

  if (globalThis.APC && globalThis.APC.__installed) return;

  var CLIENT_VERSION = '0.1.0';
  var DEDUP_WINDOW_MS = 2000;
  var ACCOUNT_TTL_MS = 10 * 60 * 1000;
  var MUTATION_THROTTLE_MS = 500;

  var SYNC_DEFAULTS = {
    serverUrl: 'http://127.0.0.1:47821',
    allowedAccounts: [],
    sources: {
      claude_web: true,
      claude_code_web: true,
      chatgpt_web: true,
      codex_cloud: true,
    },
  };

  var EMAIL_RE =
    /[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}/;

  var UUID_LIKE_RE = /^[0-9a-zA-Z][0-9a-zA-Z_-]{7,}$/;

  /* ------------------------------------------------------------ utilities */

  function log() {
    // Intentionally quiet by default. Flip to console.debug when hacking on
    // selectors: this runs on third-party pages, so we keep the console clean.
  }

  function extensionAlive() {
    try {
      return typeof chrome !== 'undefined' && !!chrome.runtime && !!chrome.runtime.id;
    } catch (err) {
      return false;
    }
  }

  /**
   * Source id for a location, per ARCHITECTURE.md "Source mapping by URL".
   * @param {Location|URL} [loc]
   * @returns {string|null}
   */
  function detectSource(loc) {
    var l = loc || globalThis.location;
    var host = String(l.hostname || '').toLowerCase();
    var path = String(l.pathname || '');
    if (host === 'claude.ai' || host.endsWith('.claude.ai')) {
      return path === '/code' || path.indexOf('/code/') === 0 ? 'claude_code_web' : 'claude_web';
    }
    if (host === 'chatgpt.com' || host.endsWith('.chatgpt.com')) {
      return path === '/codex' || path.indexOf('/codex') === 0 ? 'codex_cloud' : 'chatgpt_web';
    }
    return null;
  }

  function normaliseEmail(value) {
    if (typeof value !== 'string') return null;
    var trimmed = value.trim().toLowerCase();
    if (!trimmed) return null;
    var match = trimmed.match(EMAIL_RE);
    return match ? match[0] : null;
  }

  /*
   * There is deliberately no "find any email in this JSON" helper here.
   * Account detection must read a field that describes the *signed-in user*
   * (see content/claude.js#currentUserEmail and content/chatgpt.js): the first
   * email found anywhere in an API payload is just as likely to be a colleague,
   * an org contact or a shared-project member, and stamping a capture with the
   * wrong allowlisted identity is worse than capturing nothing.
   */

  /**
   * Same-origin JSON GET that never throws. Any of these endpoints may 404,
   * 403 or return HTML; all of that means "unknown account".
   */
  function fetchJson(path) {
    return Promise.resolve()
      .then(function () {
        return fetch(path, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
          cache: 'no-store',
        });
      })
      .then(function (res) {
        if (!res || !res.ok) return null;
        var ctype = res.headers.get('content-type') || '';
        if (ctype.indexOf('json') === -1) return null;
        return res.json();
      })
      .catch(function () {
        return null;
      });
  }

  /* -------------------------------------------------------- account cache */

  /*
   * The detected account is cached in this content script's own memory and
   * nowhere else. It is deliberately NOT kept in chrome.storage.session:
   * that area is shared by every tab of the extension, so a second tab signed
   * in as someone else would read the first tab's identity and stamp its
   * prompts with it. Memory here is per document, i.e. per tab, and it dies
   * with the page; a reload costs one extra same-origin fetch.
   *
   * Two further rules keep a stale identity out of a capture:
   *   - only a *successful* detection is cached (a null would otherwise pin
   *     "unknown" for the whole TTL, e.g. until after the user opens the
   *     profile popover the DOM fallback reads)
   *   - every URL change invalidates it (sign-out and account switches both
   *     navigate), on top of the TTL
   */
  var memoryCache = Object.create(null);

  function cacheGet(key) {
    var entry = memoryCache[key];
    if (entry && entry.expires > Date.now()) return Promise.resolve(entry.value);
    if (entry) delete memoryCache[key];
    return Promise.resolve(undefined);
  }

  function cacheSet(key, value) {
    // Never cache a miss: see above.
    if (!value) return Promise.resolve();
    memoryCache[key] = { value: value, expires: Date.now() + ACCOUNT_TTL_MS };
    return Promise.resolve();
  }

  function cacheClear(key) {
    delete memoryCache[key];
    return Promise.resolve();
  }

  /* -------------------------------------------------------------- options */

  function getOptions() {
    return new Promise(function (resolve) {
      if (!extensionAlive()) {
        resolve({ allowedAccounts: [], sources: SYNC_DEFAULTS.sources, serverUrl: SYNC_DEFAULTS.serverUrl });
        return;
      }
      try {
        chrome.storage.sync.get(SYNC_DEFAULTS, function (stored) {
          if (chrome.runtime.lastError || !stored) {
            resolve({ allowedAccounts: [], sources: SYNC_DEFAULTS.sources, serverUrl: SYNC_DEFAULTS.serverUrl });
            return;
          }
          var accounts = Array.isArray(stored.allowedAccounts) ? stored.allowedAccounts : [];
          resolve({
            serverUrl: stored.serverUrl || SYNC_DEFAULTS.serverUrl,
            allowedAccounts: accounts
              .map(function (a) {
                return String(a).trim().toLowerCase();
              })
              .filter(Boolean),
            sources: Object.assign({}, SYNC_DEFAULTS.sources, stored.sources || {}),
          });
        });
      } catch (err) {
        resolve({ allowedAccounts: [], sources: SYNC_DEFAULTS.sources, serverUrl: SYNC_DEFAULTS.serverUrl });
      }
    });
  }

  /**
   * Allowlist semantics mirror config.toml: an empty allowlist captures
   * nothing from the browser.
   */
  function isAllowlisted(account, options) {
    if (!account) return false;
    if (!options || !Array.isArray(options.allowedAccounts)) return false;
    if (options.allowedAccounts.length === 0) return false;
    return options.allowedAccounts.indexOf(String(account).trim().toLowerCase()) !== -1;
  }

  /* -------------------------------------------------------------- composer */

  function isVisible(el) {
    if (!el) return false;
    if (el.getClientRects && el.getClientRects().length > 0) return true;
    // Fall back for detached-ish layouts / jsdom-like environments.
    return !!(el.offsetParent || (el.offsetWidth | el.offsetHeight));
  }

  /** First visible element matching any selector, else first match at all. */
  function findComposer(selectors) {
    var fallback = null;
    for (var i = 0; i < selectors.length; i += 1) {
      var nodes;
      try {
        nodes = document.querySelectorAll(selectors[i]);
      } catch (err) {
        continue;
      }
      for (var j = 0; j < nodes.length; j += 1) {
        if (isVisible(nodes[j])) return nodes[j];
        if (!fallback) fallback = nodes[j];
      }
    }
    return fallback;
  }

  /** Nearest ancestor-or-self of `node` that is a composer. */
  function closestComposer(node, selectors) {
    var el = node;
    if (el && el.nodeType === 3) el = el.parentElement;
    if (!el || !el.closest) return null;
    for (var i = 0; i < selectors.length; i += 1) {
      var hit;
      try {
        hit = el.closest(selectors[i]);
      } catch (err) {
        continue;
      }
      if (hit) return hit;
    }
    return null;
  }

  /**
   * Read the composer text, preserving line breaks.
   * textarea/input -> .value; contenteditable -> .innerText (which already
   * renders block boundaries as newlines and skips ::before placeholders).
   */
  function extractText(el) {
    if (!el) return '';
    var tag = (el.tagName || '').toLowerCase();
    var raw;
    if (tag === 'textarea' || tag === 'input') {
      raw = el.value || '';
    } else {
      raw = typeof el.innerText === 'string' ? el.innerText : el.textContent || '';
    }
    return String(raw).replace(/\r\n/g, '\n').replace(/ /g, ' ').trim();
  }

  function matchesSend(node, selectors) {
    var el = node;
    if (el && el.nodeType === 3) el = el.parentElement;
    if (!el || !el.closest) return null;
    for (var i = 0; i < selectors.length; i += 1) {
      var hit;
      try {
        hit = el.closest(selectors[i]);
      } catch (err) {
        continue;
      }
      if (hit) return hit;
    }
    return null;
  }

  /* --------------------------------------------------------------- ids */

  /** Last path segment that looks like an id, else null. */
  function lastIdSegment(pathname) {
    var parts = String(pathname || '')
      .split('/')
      .filter(Boolean);
    for (var i = parts.length - 1; i >= 0; i -= 1) {
      var seg = decodeURIComponent(parts[i]);
      if (UUID_LIKE_RE.test(seg) && /[0-9-_]/.test(seg)) return seg;
    }
    return null;
  }

  function stripTitleSuffix(title, suffixes) {
    var out = String(title || '').trim();
    for (var i = 0; i < suffixes.length; i += 1) {
      var suffix = suffixes[i];
      if (out.length > suffix.length && out.slice(-suffix.length) === suffix) {
        out = out.slice(0, out.length - suffix.length).trim();
        break;
      }
    }
    return out || null;
  }

  /* ------------------------------------------------------------- capture */

  function sendCapture(payload) {
    if (!extensionAlive()) return;
    try {
      chrome.runtime.sendMessage({ type: 'capture', payload: payload }, function () {
        // Swallow "Receiving end does not exist" etc. The background worker
        // owns retries; the page must never see an error from us.
        void chrome.runtime.lastError;
      });
    } catch (err) {
      log(err);
    }
  }

  /* ------------------------------------------------------------- install */

  function install(site) {
    if (globalThis.APC && globalThis.APC.__installed) return;

    var composerSelectors = site.composerSelectors || [];
    var sendSelectors = site.sendSelectors || [];
    var accountCacheKey = 'apc_account_' + (site.key || location.hostname);

    var lastText = '';
    var lastTs = 0;
    var cachedComposer = null;
    var accountInFlight = null;
    // Bumped by every invalidation (navigation, sign-out, bfcache restore, an
    // explicit refresh). A detection that started under an older generation
    // describes a document we have already left, so its answer is dropped
    // instead of being cached: otherwise a result that lands just after
    // forgetAccount() would repopulate the cache and stamp the next prompt with
    // the previous identity.
    var accountGeneration = 0;

    function isDuplicate(text) {
      return text === lastText && Date.now() - lastTs < DEDUP_WINDOW_MS;
    }

    function noteText(text) {
      lastText = text;
      lastTs = Date.now();
    }

    function forgetAccount() {
      accountGeneration += 1;
      accountInFlight = null;
      cacheClear(accountCacheKey);
    }

    /**
     * Drop the cached account if the page has navigated since we last looked.
     * Called from the mutation observer and again immediately before a capture,
     * so a sign-out or an account switch can never stamp the next prompt with
     * the previous identity.
     */
    function noteNavigation() {
      if (location.href === lastHref) return;
      lastHref = location.href;
      forgetAccount();
    }

    function resolveAccount(forceRefresh) {
      if (forceRefresh) {
        // A refresh invalidates the identity exactly like a navigation does.
        accountGeneration += 1;
        accountInFlight = null;
        return cacheClear(accountCacheKey).then(function () {
          return resolveAccount(false);
        });
      }
      if (accountInFlight) return accountInFlight;
      var generation = accountGeneration;
      var pending = cacheGet(accountCacheKey)
        .then(function (cached) {
          if (typeof cached !== 'undefined') return cached;
          return Promise.resolve()
            .then(function () {
              return site.detectAccount();
            })
            .catch(function () {
              return null;
            })
            .then(function (found) {
              // The page navigated (or signed out) while this detection was in
              // flight: the answer belongs to the document we just left, so it
              // must neither be cached nor returned.
              if (generation !== accountGeneration) return null;
              var email = normaliseEmail(found);
              return cacheSet(accountCacheKey, email).then(function () {
                return email;
              });
            });
        })
        .then(function (value) {
          if (accountInFlight === pending) accountInFlight = null;
          // Also covers a cache hit that was invalidated mid-read.
          return generation === accountGeneration ? value : null;
        })
        .catch(function () {
          if (accountInFlight === pending) accountInFlight = null;
          return null;
        });
      accountInFlight = pending;
      return pending;
    }

    function currentComposer(eventTarget) {
      var fromEvent = closestComposer(eventTarget, composerSelectors);
      if (fromEvent) {
        cachedComposer = fromEvent;
        return fromEvent;
      }
      var active = document.activeElement;
      var fromActive = closestComposer(active, composerSelectors);
      if (fromActive) {
        cachedComposer = fromActive;
        return fromActive;
      }
      if (cachedComposer && cachedComposer.isConnected) return cachedComposer;
      cachedComposer = findComposer(composerSelectors);
      return cachedComposer;
    }

    function finalize(text) {
      noteNavigation();
      var source = site.detectSource ? site.detectSource() : detectSource();
      if (!source) return Promise.resolve({ captured: false, reason: 'unknown_source' });
      return getOptions().then(function (options) {
        if (options.sources[source] === false) {
          return { captured: false, reason: 'source_disabled' };
        }
        if (options.allowedAccounts.length === 0) {
          return { captured: false, reason: 'empty_allowlist' };
        }
        return resolveAccount(false).then(function (account) {
          if (!account) return { captured: false, reason: 'unknown_account' };
          if (!isAllowlisted(account, options)) {
            return { captured: false, reason: 'account_not_allowed' };
          }
          sendCapture({
            source: source,
            prompt: text,
            account: account,
            conversation_id: site.conversationId ? site.conversationId() : null,
            url: location.href,
            title: site.title ? site.title() : null,
            ts: new Date().toISOString(),
            client_version: CLIENT_VERSION,
          });
          return { captured: true };
        });
      });
    }

    /**
     * Read the composer synchronously (before the app clears it), then do the
     * allowlist work asynchronously.
     */
    function maybeCapture(eventTarget) {
      var el = currentComposer(eventTarget);
      if (!el) return;
      var text = extractText(el);
      if (!text) return;
      if (isDuplicate(text)) return;
      noteText(text);
      finalize(text).catch(function () {});
    }

    function onKeydown(event) {
      if (!event || event.key !== 'Enter') return;
      if (event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
      // IME composition: Enter is committing a candidate, not submitting.
      if (event.isComposing || event.keyCode === 229) return;
      if (event.repeat) return;
      var target = event.target;
      var inComposer =
        !!closestComposer(target, composerSelectors) ||
        !!closestComposer(document.activeElement, composerSelectors);
      if (!inComposer) return;
      maybeCapture(target);
    }

    function onClick(event) {
      if (!event) return;
      if (typeof event.button === 'number' && event.button !== 0) return;
      var btn = matchesSend(event.target, sendSelectors);
      if (!btn) return;
      if (btn.disabled) return;
      maybeCapture(event.target);
    }

    function onSubmit(event) {
      if (!event) return;
      maybeCapture(event.target);
    }

    // Document-level capture-phase listeners: an SPA re-render of the composer
    // cannot detach them, so there is nothing to re-bind.
    document.addEventListener('keydown', onKeydown, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('submit', onSubmit, true);

    // The observer only refreshes our cached composer reference (and drops a
    // stale account cache after an SPA navigation to a different account view).
    var lastHref = location.href;
    var throttled = 0;
    var observer = new MutationObserver(function () {
      var now = Date.now();
      if (now - throttled < MUTATION_THROTTLE_MS) return;
      throttled = now;
      if (!cachedComposer || !cachedComposer.isConnected) {
        cachedComposer = findComposer(composerSelectors);
      }
      // A sign-out or an account switch always changes the URL, and a stale
      // identity would stamp the *next* user's prompt, so drop the cached
      // account rather than waiting out the TTL.
      noteNavigation();
    });
    try {
      observer.observe(document.documentElement || document, {
        childList: true,
        subtree: true,
      });
    } catch (err) {
      log(err);
    }

    if (extensionAlive()) {
      try {
        chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
          if (!message || message.type !== 'whoami') return undefined;
          var source = site.detectSource ? site.detectSource() : detectSource();
          Promise.all([getOptions(), resolveAccount(!!message.refresh)])
            .then(function (results) {
              var options = results[0];
              var account = results[1];
              sendResponse({
                ok: true,
                source: source,
                account: account || null,
                allowlisted: isAllowlisted(account, options),
                allowlistSize: options.allowedAccounts.length,
                sourceEnabled: options.sources[source] !== false,
                composerFound: !!findComposer(composerSelectors),
                conversationId: site.conversationId ? site.conversationId() : null,
                title: site.title ? site.title() : null,
                url: location.href,
                clientVersion: CLIENT_VERSION,
              });
            })
            .catch(function (err) {
              sendResponse({ ok: false, error: String(err) });
            });
          return true;
        });
      } catch (err) {
        log(err);
      }
    }

    // Warm the account cache so the first Enter is not racing a fetch.
    resolveAccount(false).catch(function () {});
    // A bfcache restore can bring back a document that was signed in as
    // somebody else; treat it like any other navigation.
    globalThis.addEventListener('pageshow', function (event) {
      if (!event || !event.persisted) return;
      forgetAccount();
      resolveAccount(false).catch(function () {});
    });

    globalThis.APC.__installed = true;
    globalThis.APC.__site = site;
  }

  globalThis.APC = {
    __installed: false,
    CLIENT_VERSION: CLIENT_VERSION,
    EMAIL_RE: EMAIL_RE,
    DEDUP_WINDOW_MS: DEDUP_WINDOW_MS,
    ACCOUNT_TTL_MS: ACCOUNT_TTL_MS,
    detectSource: detectSource,
    normaliseEmail: normaliseEmail,
    fetchJson: fetchJson,
    getOptions: getOptions,
    isAllowlisted: isAllowlisted,
    findComposer: findComposer,
    closestComposer: closestComposer,
    extractText: extractText,
    matchesSend: matchesSend,
    lastIdSegment: lastIdSegment,
    stripTitleSuffix: stripTitleSuffix,
    sendCapture: sendCapture,
    cacheGet: cacheGet,
    cacheSet: cacheSet,
    cacheClear: cacheClear,
    install: install,
  };
})();
