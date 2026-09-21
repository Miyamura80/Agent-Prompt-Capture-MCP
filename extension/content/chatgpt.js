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

  /**
   * The signed-in account, or null.
   *
   * Only `user.email` from /api/auth/session counts. A deep search of the
   * session payload would happily return an unrelated nested address (an
   * organisation contact, a workspace owner, a shared-project member) and the
   * allowlist would then authorise *this* user's prompts under someone else's
   * identity. The contract is allowlisted accounts only, so a wrong guess is
   * worse than returning null and capturing nothing.
   */
  function detectAccount() {
    return APC.fetchJson('/api/auth/session')
      .then(function (data) {
        if (!data || !data.user) return null;
        return APC.normaliseEmail(data.user.email) || null;
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
