/**
 * Agent Prompt Capture - chatgpt.com content script.
 *
 * chatgpt.com/codex* -> codex_cloud
 * everything else    -> chatgpt_web
 */
(function () {
  'use strict';

  var APC = globalThis.APC;
  if (!APC || APC.__installed) return;

  var COMPOSER_SELECTORS = [
    '#prompt-textarea',
    'div.ProseMirror[contenteditable="true"]',
    'textarea[data-id]',
    'textarea',
  ];

  var SEND_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label*="Send"]',
    'form button[type="submit"]',
  ];

  function detectSource() {
    return APC.detectSource(location);
  }

  function conversationId() {
    // /c/<uuid> and /codex/tasks/<id> both end in the id we want.
    return APC.lastIdSegment(location.pathname);
  }

  function title() {
    return APC.stripTitleSuffix(document.title, [' | ChatGPT', ' - ChatGPT', ' – ChatGPT']);
  }

  function detectAccount() {
    return APC.fetchJson('/api/auth/session')
      .then(function (data) {
        if (data && data.user && typeof data.user.email === 'string') {
          var direct = APC.normaliseEmail(data.user.email);
          if (direct) return direct;
        }
        return APC.findEmailInObject(data);
      })
      .catch(function () {
        return null;
      });
  }

  APC.install({
    key: 'chatgpt',
    composerSelectors: COMPOSER_SELECTORS,
    sendSelectors: SEND_SELECTORS,
    detectSource: detectSource,
    detectAccount: detectAccount,
    conversationId: conversationId,
    title: title,
  });
})();
