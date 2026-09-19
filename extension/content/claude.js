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

  function detectAccount() {
    // 1. organizations: the app calls this on boot; members often carry emails.
    return APC.fetchJson('/api/organizations')
      .then(function (data) {
        var email = APC.findEmailInObject(data);
        if (email) return email;
        // 2. account / bootstrap payloads.
        return APC.fetchJson('/api/account').then(function (account) {
          var fromAccount = APC.findEmailInObject(account);
          if (fromAccount) return fromAccount;
          return APC.fetchJson('/api/bootstrap').then(function (bootstrap) {
            return APC.findEmailInObject(bootstrap);
          });
        });
      })
      .then(function (email) {
        // 3. DOM fallback (only sees anything when a profile popover is open).
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
