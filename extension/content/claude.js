/**
 * Agent Prompt Capture - claude.ai content script.
 *
 * claude.ai/code/*  -> claude_code_web
 * everything else   -> claude_web
 *
 * Every account-detection step here is best effort: these are private
 * endpoints that can 404, change shape, or require a session we do not have.
 * Any failure means "unknown account", which means nothing is captured.
 */
(function () {
  'use strict';

  var APC = globalThis.APC;
  if (!APC || APC.__installed) return;

  var COMPOSER_SELECTORS = [
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"][data-placeholder]',
    'fieldset div[contenteditable="true"]',
    'textarea',
  ];

  var SEND_SELECTORS = [
    'button[aria-label="Send Message"]',
    'button[aria-label="Send message"]',
    'button[aria-label*="Send"]',
    'button[type="submit"]',
  ];

  // Containers that only hold an email address once the user has opened them.
  var ACCOUNT_DOM_SELECTORS = [
    '[data-testid*="user"]',
    '[aria-label*="Account"]',
    '[aria-label*="account"]',
    '[role="menu"]',
    '[role="dialog"]',
    '[data-radix-popper-content-wrapper]',
  ];

  function detectSource() {
    return APC.detectSource(location);
  }

  function conversationId() {
    // /chat/<uuid>, /code/session/<id>, /project/<id>/chat/<uuid>, ...
    return APC.lastIdSegment(location.pathname);
  }

  function title() {
    return APC.stripTitleSuffix(document.title, [' - Claude', ' | Claude', ' – Claude']);
  }

  function accountFromDom() {
    for (var i = 0; i < ACCOUNT_DOM_SELECTORS.length; i += 1) {
      var nodes;
      try {
        nodes = document.querySelectorAll(ACCOUNT_DOM_SELECTORS[i]);
      } catch (err) {
        continue;
      }
      for (var j = 0; j < nodes.length; j += 1) {
        var node = nodes[j];
        // Only read popovers that are actually open / rendered.
        if (!node.getClientRects || node.getClientRects().length === 0) continue;
        var text = node.innerText || node.textContent || '';
        if (!text || text.length > 4000) continue;
        var match = text.match(APC.EMAIL_RE);
        if (match) return APC.normaliseEmail(match[0]);
      }
    }
    return null;
  }

  /**
   * Field names claude.ai uses for the *signed-in* user's address.
   * `email_address` is what /api/auth/current_account returns.
   */
  var CURRENT_USER_FIELDS = ['email_address', 'email', 'emailAddress', 'primary_email'];

  /**
   * Read a current-user email out of a payload, and only that.
   *
   * We look at the document root and at the handful of keys that describe the
   * viewer (`account`, `current_account`, `user`, `profile`) - never at a
   * members/users list. /api/organizations answers with the organisations the
   * session belongs to, and those objects can carry other members: a deep
   * search there returns whichever member happens to come first, which is how
   * one person's prompts end up filed under a colleague's allowlisted address.
   */
  function currentUserEmail(data) {
    if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
    var containers = [data, data.account, data.current_account, data.user, data.profile];
    for (var i = 0; i < containers.length; i += 1) {
      var container = containers[i];
      if (!container || typeof container !== 'object' || Array.isArray(container)) continue;
      for (var j = 0; j < CURRENT_USER_FIELDS.length; j += 1) {
        var email = APC.normaliseEmail(container[CURRENT_USER_FIELDS[j]]);
        if (email) return email;
      }
    }
    return null;
  }

  /**
   * The signed-in account, or null. Every step is best effort and every
   * failure means "unknown account", which means nothing is captured.
   */
  function detectAccount() {
    // 1. the current-account endpoint: one user, no roster to confuse it with.
    return APC.fetchJson('/api/auth/current_account')
      .then(function (data) {
        var email = currentUserEmail(data);
        if (email) return email;
        // 2. /api/account, same shape rules.
        return APC.fetchJson('/api/account').then(function (account) {
          return currentUserEmail(account);
        });
      })
      .then(function (email) {
        // 3. DOM fallback: the profile popover shows the viewer's own address
        //    (only readable once the user has opened it).
        return email || accountFromDom();
      })
      .catch(function () {
        return null;
      });
  }

  APC.install({
    key: 'claude',
    composerSelectors: COMPOSER_SELECTORS,
    sendSelectors: SEND_SELECTORS,
    detectSource: detectSource,
    detectAccount: detectAccount,
    conversationId: conversationId,
    title: title,
  });
})();
