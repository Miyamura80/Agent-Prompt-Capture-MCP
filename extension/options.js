/* Options page. ES module (no inline handlers: MV3 CSP). It imports the same
   loopback rule the service worker enforces, so what the UI accepts and what
   the worker is willing to POST to cannot drift apart. */
import { serverUrlProblem } from './lib/limits.js';

(function () {
  'use strict';

  var SOURCES = ['claude_web', 'claude_code_web', 'chatgpt_web', 'codex_cloud'];

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

  var $ = function (id) {
    return document.getElementById(id);
  };

  function setStatus(text, cls) {
    var el = $('status');
    el.textContent = text || '';
    el.className = 'status' + (cls ? ' ' + cls : '');
  }

  function parseAccounts(raw) {
    var seen = Object.create(null);
    return String(raw || '')
      .split(/[\n,;]+/)
      .map(function (line) {
        return line.trim().toLowerCase();
      })
      .filter(function (line) {
        if (!line) return false;
        if (seen[line]) return false;
        seen[line] = true;
        return true;
      });
  }

  function load() {
    chrome.storage.sync.get(SYNC_DEFAULTS, function (stored) {
      $('serverUrl').value = stored.serverUrl || SYNC_DEFAULTS.serverUrl;
      $('allowedAccounts').value = (stored.allowedAccounts || []).join('\n');
      var sources = Object.assign({}, SYNC_DEFAULTS.sources, stored.sources || {});
      SOURCES.forEach(function (src) {
        $('src-' + src).checked = sources[src] !== false;
      });
    });
    chrome.storage.local.get({ token: '' }, function (stored) {
      $('token').value = stored.token || '';
    });
  }

  function save() {
    var serverUrl = $('serverUrl').value.trim().replace(/\/+$/, '') || SYNC_DEFAULTS.serverUrl;
    // `apc serve` binds loopback and the manifest only grants http://127.0.0.1
    // and http://localhost: anything else saves a configuration the worker will
    // refuse to use.
    var problem = serverUrlProblem(serverUrl);
    if (problem) {
      setStatus(problem, 'err');
      return;
    }

    var sources = {};
    SOURCES.forEach(function (src) {
      sources[src] = $('src-' + src).checked;
    });
    var accounts = parseAccounts($('allowedAccounts').value);

    chrome.storage.sync.set(
      { serverUrl: serverUrl, allowedAccounts: accounts, sources: sources },
      function () {
        if (chrome.runtime.lastError) {
          setStatus('Could not save: ' + chrome.runtime.lastError.message, 'err');
          return;
        }
        chrome.storage.local.set({ token: $('token').value.trim() }, function () {
          $('serverUrl').value = serverUrl;
          $('allowedAccounts').value = accounts.join('\n');
          if (accounts.length === 0) {
            setStatus('Saved. The allowlist is empty, so nothing will be captured.', 'warn');
          } else {
            setStatus('Saved.', 'ok');
          }
        });
      },
    );
  }

  function testConnection() {
    setStatus('Testing...', '');
    chrome.runtime.sendMessage({ type: 'testConnection' }, function (res) {
      if (chrome.runtime.lastError) {
        setStatus('Background worker unreachable: ' + chrome.runtime.lastError.message, 'err');
        return;
      }
      if (!res) {
        setStatus('No response from the background worker.', 'err');
        return;
      }
      if (!res.reachable) {
        setStatus(
          'Listener not reachable at ' + res.serverUrl + '\n' + (res.error || '') +
            '\nIs `apc serve` running?',
          'err',
        );
        return;
      }
      if (!res.ok) {
        setStatus(
          'Listener is up at ' + res.serverUrl + ' but /v1/config failed: ' + (res.error || '') +
            '\nCheck the token.',
          'warn',
        );
        return;
      }
      var version = res.health && res.health.version ? ' v' + res.health.version : '';
      var cfg = res.config || {};
      setStatus(
        'Connected to ' + res.serverUrl + version +
          '\nallowed accounts on the server: ' + (cfg.allowed_accounts_count != null ? cfg.allowed_accounts_count : '?') +
          '\nenabled sources: ' + (Array.isArray(cfg.sources) ? cfg.sources.join(', ') : '?'),
        'ok',
      );
    });
  }

  function clearQueue() {
    chrome.runtime.sendMessage({ type: 'clearQueue' }, function (res) {
      if (chrome.runtime.lastError || !res || !res.ok) {
        setStatus('Could not clear the queue.', 'err');
        return;
      }
      setStatus('Queue cleared.', 'ok');
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    load();
    $('save').addEventListener('click', save);
    $('test').addEventListener('click', testConnection);
    $('clearQueue').addEventListener('click', clearQueue);
  });
})();
