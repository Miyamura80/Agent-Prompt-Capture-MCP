/* Popup. Classic script; no inline handlers (MV3 CSP). */
(function () {
  'use strict';

  var $ = function (id) {
    return document.getElementById(id);
  };

  function set(id, text, cls) {
    var el = $(id);
    if (!el) return;
    el.textContent = text;
    el.className = cls || '';
  }

  function renderStatus(res) {
    if (!res || !res.ok) {
      set('listener', 'background worker unavailable', 'err');
      return;
    }
    if (res.reachable) {
      set('listener', 'yes - ' + res.serverUrl, 'ok');
    } else {
      set('listener', 'no - ' + res.serverUrl + ' (' + (res.healthError || 'unreachable') + ')', 'err');
    }
    set('last24h', String(res.last24h));
    set('queued', String(res.queued), res.queued > 0 ? 'warn' : '');
    if (res.lastError && res.lastError.message) {
      set('lastError', res.lastError.message, 'err');
      $('lastError').title = res.lastError.ts || '';
    } else {
      set('lastError', 'none');
    }
    $('version').textContent = 'v' + (res.clientVersion || '');
    if (!res.hasToken) {
      set('lastError', 'no token configured - open Options', 'warn');
    }
  }

  function renderWhoami(res, fallbackSource) {
    if (!res || !res.ok) {
      set('account', 'unknown (not a supported page)', 'warn');
      set('allowlisted', '-');
      set('source', fallbackSource || '-');
      return;
    }
    set('source', res.source || '-');
    if (res.account) {
      set('account', res.account);
    } else {
      set('account', 'unknown - nothing will be captured', 'warn');
    }
    if (!res.account) {
      set('allowlisted', 'n/a', 'warn');
    } else if (res.allowlisted) {
      set('allowlisted', 'yes', 'ok');
    } else if (res.allowlistSize === 0) {
      set('allowlisted', 'no - allowlist is empty', 'err');
    } else {
      set('allowlisted', 'no - add it in Options', 'err');
    }
    if (res.sourceEnabled === false) {
      set('source', (res.source || '?') + ' (disabled in Options)', 'warn');
    }
    if (res.composerFound === false) {
      set('source', (res.source || '?') + ' (composer not found)', 'warn');
    }
  }

  function askContentScript() {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      var tab = tabs && tabs[0];
      if (!tab || !tab.id || !tab.url) {
        renderWhoami(null, null);
        return;
      }
      var supported = /^https:\/\/(claude\.ai|chatgpt\.com)\//.test(tab.url);
      if (!supported) {
        set('account', 'not a captured site', '');
        set('allowlisted', '-');
        set('source', '-');
        return;
      }
      chrome.tabs.sendMessage(tab.id, { type: 'whoami' }, function (res) {
        if (chrome.runtime.lastError) {
          set('account', 'content script not loaded (reload the tab)', 'warn');
          set('allowlisted', '-');
          set('source', '-');
          return;
        }
        renderWhoami(res, null);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('openOptions').addEventListener('click', function (event) {
      event.preventDefault();
      if (chrome.runtime.openOptionsPage) chrome.runtime.openOptionsPage();
      else chrome.tabs.create({ url: chrome.runtime.getURL('options.html') });
    });

    chrome.runtime.sendMessage({ type: 'status' }, function (res) {
      if (chrome.runtime.lastError) {
        renderStatus(null);
        return;
      }
      renderStatus(res);
    });

    askContentScript();
  });
})();
