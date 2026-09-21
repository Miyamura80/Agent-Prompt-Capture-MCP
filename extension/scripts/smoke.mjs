/**
 * End-to-end smoke test for the extension.
 *
 *   node extension/scripts/smoke.mjs
 *
 * What it does:
 *   1. starts a local http server that plays two roles
 *        - the *site*: serves fixtures/fake-chatgpt.html and stubs
 *          GET /api/auth/session so account detection has something to read
 *        - the *listener*: GET /v1/health, GET /v1/config, POST /v1/prompts
 *   2. launches Chromium with the unpacked extension loaded
 *   3. seeds the extension's options (serverUrl / token / allowlist) through an
 *      extension page
 *   4. routes https://chatgpt.com/** at the local fixture so the real
 *      content_scripts matches fire, types into #prompt-textarea, hits Enter
 *   5. asserts the listener received a POST /v1/prompts with the right
 *      source / account / prompt (and that the pre-scrub ran)
 *
 * Chromium refuses to load extensions without a browser UI, so if there is no
 * DISPLAY this script re-execs itself under xvfb-run.
 *
 * With APC_E2E_LISTENER_URL + APC_E2E_TOKEN set (scripts/e2e.py), the extension
 * is pointed at a REAL `apc serve` instead of the mock listener above. The site
 * fixtures are unchanged; only the listener half of the local server goes
 * unused, so the assertions that inspect the mock's captured requests are
 * skipped and delivery is proven through the worker's own `status.last24h`.
 */

import http from 'node:http';
import { readFileSync, mkdtempSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync, spawnSync } from 'node:child_process';

const HERE = dirname(fileURLToPath(import.meta.url));
const EXT_DIR = resolve(HERE, '..');
const FIXTURE_CHATGPT = readFileSync(join(HERE, 'fixtures', 'fake-chatgpt.html'), 'utf8');
const FIXTURE_CLAUDE = readFileSync(join(HERE, 'fixtures', 'fake-claude.html'), 'utf8');

const TOKEN = 'smoke-token-abc123';
const ACCOUNT = 'me@work.com';
const CONVERSATION_ID = '11111111-2222-3333-4444-555555555555';
const SITE_URL = `https://chatgpt.com/c/${CONVERSATION_ID}`;
const CLAUDE_SESSION_ID = 'sess-abc123def';
const CLAUDE_URL = `https://claude.ai/code/session/${CLAUDE_SESSION_ID}`;
const SECRET_EMAIL = 'leaked.person@example.com';
// Another member of the same organisation. It must never be mistaken for the
// signed-in user: the allowlist contract says a wrong identity is worse than none.
const DECOY_EMAIL = 'colleague@example.com';
// The identity a *slow* account lookup answers with, after the page has already
// navigated away from the document that asked for it.
const STALE_EMAIL = 'previous.user@example.com';
const RACE_BEFORE_ID = 'race-before-1111';
const RACE_AFTER_ID = 'race-after-2222';
const LINE_ONE = 'summarise the changelog';
const LINE_TWO = `and cc ${SECRET_EMAIL}`;

/* ------------------------------------------- real-listener (e2e) overrides */

const E2E_LISTENER_URL = (process.env.APC_E2E_LISTENER_URL || '').replace(/\/+$/, '');
const E2E_TOKEN = process.env.APC_E2E_TOKEN || '';
const E2E = E2E_LISTENER_URL !== '' && E2E_TOKEN !== '';

/* --------------------------------------------------- headed re-exec helper */

function hasXvfb() {
  const r = spawnSync('which', ['xvfb-run'], { encoding: 'utf8' });
  return r.status === 0 && r.stdout.trim().length > 0;
}

if (!process.env.DISPLAY && !process.env.APC_SMOKE_NO_XVFB) {
  if (hasXvfb()) {
    console.log('[smoke] no DISPLAY; re-running under xvfb-run');
    const r = spawnSync(
      'xvfb-run',
      ['-a', '--server-args=-screen 0 1280x900x24', process.execPath, fileURLToPath(import.meta.url)],
      { stdio: 'inherit', env: { ...process.env, APC_SMOKE_NO_XVFB: '1' } },
    );
    process.exit(r.status === null ? 1 : r.status);
  }
  console.log('[smoke] no DISPLAY and no xvfb-run; trying headless anyway');
}

/* ------------------------------------------------------- playwright lookup */

// Only override playwright's own browser cache when this machine actually keeps
// the browsers in /opt (CI images do). Forcing the override on a clean checkout
// points chromium lookup at a directory that does not exist and the launch fails.
if (!process.env.PLAYWRIGHT_BROWSERS_PATH && existsSync('/opt/pw-browsers')) {
  process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
}

async function loadPlaywright() {
  try {
    // extension/package.json declares playwright as a devDependency:
    // `npm install` in extension/ makes this the resolvable copy.
    return await import('playwright');
  } catch {
    /* not installed here; fall back to a global install before giving up */
  }
  let globalRoot = '';
  try {
    globalRoot = execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim();
  } catch {
    globalRoot = '';
  }
  const candidates = [globalRoot, '/opt/node22/lib/node_modules', '/usr/lib/node_modules'].filter(Boolean);
  for (const root of candidates) {
    const entry = join(root, 'playwright');
    if (!existsSync(entry)) continue;
    const req = createRequire(join(root, 'noop.js'));
    return req('playwright');
  }
  throw new Error(
    'Could not resolve the playwright package. Run `npm install` in extension/ ' +
      `(looked in: ${candidates.join(', ')})`,
  );
}

/* ------------------------------------------------------------ local server */

const received = { prompts: [], health: 0, config: 0, session: 0, currentAccount: 0, account: 0, unauthorised: 0 };
// The fixture the catch-all serves; flipped when the claude phase starts.
let currentFixture = FIXTURE_CHATGPT;
// What GET /api/auth/session answers with, and how long it sits on the answer.
// Phase 10 uses both to hold one lookup open across a navigation.
let sessionEmail = ACCOUNT;
let sessionDelayMs = 0;

function cors(req, res) {
  res.setHeader('Access-Control-Allow-Origin', req.headers.origin || '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-APC-Token');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Max-Age', '600');
}

function json(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text) });
  res.end(text);
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  cors(req, res);

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  // --- site stubs
  if (url.pathname === '/api/auth/session') {
    received.session += 1;
    const body = { user: { id: 'user-1', email: sessionEmail, name: 'Test User' }, expires: '2030-01-01T00:00:00Z' };
    if (sessionDelayMs > 0) {
      const wait = sessionDelayMs;
      setTimeout(() => json(res, 200, body), wait);
      return;
    }
    json(res, 200, body);
    return;
  }

  // claude.ai account endpoints.
  //   /api/auth/current_account 404s (a dead endpoint must degrade to "unknown")
  //   /api/account answers with an org roster and NO current-user field, which is
  //   the shape that used to make the old deep search return a colleague's address
  // so the run ends up on the DOM fallback, which reads the viewer's own popover.
  if (url.pathname === '/api/auth/current_account') {
    received.currentAccount += 1;
    json(res, 404, { error: 'not found' });
    return;
  }
  if (url.pathname === '/api/account') {
    received.account += 1;
    json(res, 200, {
      organization: { name: 'Acme', members: [{ email: DECOY_EMAIL }, { email: ACCOUNT }] },
    });
    return;
  }

  // --- listener stubs
  if (url.pathname === '/v1/health') {
    received.health += 1;
    json(res, 200, { ok: true, version: '0.1.0' });
    return;
  }

  if (url.pathname === '/v1/config') {
    received.config += 1;
    if (req.headers['x-apc-token'] !== TOKEN) {
      received.unauthorised += 1;
      json(res, 401, { error: 'bad token' });
      return;
    }
    json(res, 200, { allowed_accounts_count: 1, sources: ['claude_web', 'chatgpt_web'] });
    return;
  }

  if (url.pathname === '/v1/prompts' && req.method === 'POST') {
    let body = '';
    req.on('data', (c) => {
      body += c;
    });
    req.on('end', () => {
      if (req.headers['x-apc-token'] !== TOKEN) {
        received.unauthorised += 1;
        json(res, 401, { error: 'bad token' });
        return;
      }
      try {
        received.prompts.push(JSON.parse(body));
      } catch {
        json(res, 400, { error: 'bad json' });
        return;
      }
      json(res, 202, { stored: true, id: `id-${received.prompts.length}` });
    });
    return;
  }

  // --- the fixture page for everything else
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  res.end(currentFixture);
});

/* ------------------------------------------------------------------ assert */

const failures = [];
function check(name, condition, detail) {
  if (condition) {
    console.log(`ok   ${name}`);
  } else {
    failures.push(`${name}${detail ? ` -- ${detail}` : ''}`);
    console.error(`FAIL ${name}${detail ? `\n     ${detail}` : ''}`);
  }
}

// Assertions that read `received` (what the MOCK listener saw). Meaningless when
// the extension is posting to a real `apc serve`, so they are skipped there.
function mockCheck(name, condition, detail) {
  if (E2E) {
    console.log(`skip ${name} -- real listener; not observable from here`);
    return;
  }
  check(name, condition, detail);
}

async function waitFor(label, predicate, timeoutMs = 15000, intervalMs = 200) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return true;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  console.error(`[smoke] timed out waiting for ${label}`);
  return false;
}

/* -------------------------------------------------------------------- main */

let context = null;
let userDataDir = null;

async function main() {
  const { chromium } = await loadPlaywright();

  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const serverUrl = `http://127.0.0.1:${port}`;
  console.log(`[smoke] local site + listener on ${serverUrl}`);

  const listenerUrl = E2E ? E2E_LISTENER_URL : serverUrl;
  const listenerToken = E2E ? E2E_TOKEN : TOKEN;
  if (E2E) console.log(`[smoke] APC_E2E: extension will target the real listener ${listenerUrl}`);

  userDataDir = mkdtempSync(join(tmpdir(), 'apc-smoke-'));
  context = await chromium.launchPersistentContext(userDataDir, {
    headless: false,
    channel: 'chromium',
    ignoreHTTPSErrors: true,
    args: [
      `--disable-extensions-except=${EXT_DIR}`,
      `--load-extension=${EXT_DIR}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      '--disable-dev-shm-usage',
    ],
  });

  // 1. find the extension's service worker -> extension id
  let [worker] = context.serviceWorkers();
  if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 20000 });
  const extensionId = new URL(worker.url()).host;
  check('extension service worker started', !!extensionId, worker.url());

  // 2. seed options through an extension page (same storage the options UI writes)
  const optionsPage = await context.newPage();
  await optionsPage.goto(`chrome-extension://${extensionId}/options.html`);
  await optionsPage.evaluate(
    async ({ serverUrl, token, account }) => {
      await chrome.storage.sync.set({
        serverUrl,
        allowedAccounts: [account],
        sources: { claude_web: true, claude_code_web: true, chatgpt_web: true, codex_cloud: true },
      });
      await chrome.storage.local.set({ token, apc_queue: [], apc_captures: [], apc_last_error: null });
    },
    { serverUrl: listenerUrl, token: listenerToken, account: ACCOUNT },
  );
  check('options page loaded and seeded', true);

  // How many captures the worker has actually delivered. Against the mock we can
  // count the POSTs it received; against a real listener the worker's own
  // last-24h counter (incremented only on a successful POST) is the evidence.
  const delivered = async () => {
    if (!E2E) return received.prompts.length;
    const s = await optionsPage.evaluate(
      () => new Promise((r) => chrome.runtime.sendMessage({ type: 'status' }, r)),
    );
    return s && typeof s.last24h === 'number' ? s.last24h : 0;
  };

  // 3. serve the fixture at https://chatgpt.com/** so content_scripts matches fire
  await context.route(/^https:\/\/(chatgpt\.com|claude\.ai)\//, async (route) => {
    const url = new URL(route.request().url());
    const target = `${serverUrl}${url.pathname}${url.search}`;
    try {
      const res = await fetch(target, {
        method: route.request().method(),
        headers: { Accept: 'application/json' },
      });
      const body = Buffer.from(await res.arrayBuffer());
      await route.fulfill({
        status: res.status,
        headers: { 'content-type': res.headers.get('content-type') || 'text/html; charset=utf-8' },
        body,
      });
    } catch (err) {
      await route.abort();
    }
  });

  const page = await context.newPage();
  await page.goto(SITE_URL, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#prompt-textarea');
  check('fixture page served at chatgpt.com', (await page.title()).includes('ChatGPT'));

  // give the content script (document_idle) time to install and warm the account cache
  await page.waitForTimeout(1500);
  check('content script reached /api/auth/session', received.session > 0, `session hits: ${received.session}`);

  // 4. type a two-line prompt and press Enter
  await page.click('#prompt-textarea');
  await page.keyboard.type(LINE_ONE);
  await page.keyboard.down('Shift');
  await page.keyboard.press('Enter');
  await page.keyboard.up('Shift');
  await page.keyboard.type(LINE_TWO);
  await page.keyboard.press('Enter');

  const got = await waitFor('POST /v1/prompts', async () => (await delivered()) > 0, 20000);
  check('listener received a POST /v1/prompts', got, `delivered: ${await delivered()}`);

  // the page's own handler must still have run (we never preventDefault)
  const pageSubmitted = await page.evaluate(() => window.__lastSubmitted || null);
  check('page submit handler still ran (no preventDefault from us)', !!pageSubmitted, String(pageSubmitted));

  if (got && !E2E) {
    const body = received.prompts[0];
    check('source is chatgpt_web', body.source === 'chatgpt_web', JSON.stringify(body.source));
    check('account is the allowlisted address', body.account === ACCOUNT, JSON.stringify(body.account));
    check('conversation_id came from the URL', body.conversation_id === CONVERSATION_ID, JSON.stringify(body.conversation_id));
    check('url is the page url', body.url === SITE_URL, JSON.stringify(body.url));
    check('client_version is set', body.client_version === '0.1.0', JSON.stringify(body.client_version));
    check('ts is ISO 8601 UTC', /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(String(body.ts)), String(body.ts));

    const lines = String(body.prompt).split('\n').map((s) => s.trim()).filter(Boolean);
    check('prompt kept both lines', lines.length === 2 && lines[0] === LINE_ONE, JSON.stringify(body.prompt));
    check(
      'pre-scrub replaced the email in the prompt',
      body.prompt.includes('[EMAIL]') && !body.prompt.includes(SECRET_EMAIL),
      JSON.stringify(body.prompt),
    );
    check('only one capture was sent (dedup held)', received.prompts.length === 1, `count: ${received.prompts.length}`);
  }

  // 5. popup-facing plumbing: whoami (exactly the call popup.js makes) + status
  const whoami = await optionsPage.evaluate(async () => {
    const tabs = await chrome.tabs.query({ url: 'https://chatgpt.com/*' });
    if (!tabs.length) return { error: 'no chatgpt tab' };
    try {
      return await chrome.tabs.sendMessage(tabs[0].id, { type: 'whoami' });
    } catch (err) {
      return { error: String(err) };
    }
  });
  check('whoami reports the account', whoami && whoami.account === ACCOUNT, JSON.stringify(whoami));
  check('whoami reports allowlisted', whoami && whoami.allowlisted === true, JSON.stringify(whoami && whoami.allowlisted));
  check('whoami reports the source', whoami && whoami.source === 'chatgpt_web', JSON.stringify(whoami && whoami.source));
  check('whoami found the composer', whoami && whoami.composerFound === true, JSON.stringify(whoami && whoami.composerFound));

  const status = await optionsPage.evaluate(
    () => new Promise((resolveP) => chrome.runtime.sendMessage({ type: 'status' }, resolveP)),
  );
  check('status reports the listener reachable', status && status.reachable === true, JSON.stringify(status && status.healthError));
  check('status counts the capture in the last 24h', status && status.last24h === 1, JSON.stringify(status && status.last24h));
  check('status shows an empty queue', status && status.queued === 0, JSON.stringify(status && status.queued));

  const test = await optionsPage.evaluate(
    () => new Promise((resolveP) => chrome.runtime.sendMessage({ type: 'testConnection' }, resolveP)),
  );
  check(
    'testConnection hit /v1/health and /v1/config',
    test && test.ok === true && (E2E || received.config > 0),
    JSON.stringify(test),
  );
  mockCheck('no request was rejected for a bad token', received.unauthorised === 0, `401s: ${received.unauthorised}`);

  // 6. failure path: point the worker at a dead port and confirm the capture
  //    lands in the retry queue instead of being lost.
  const deadUrl = 'http://127.0.0.1:1';
  await optionsPage.evaluate((url) => chrome.storage.sync.set({ serverUrl: url }), deadUrl);
  await page.bringToFront();
  await page.click('#prompt-textarea');
  await page.keyboard.type('this one should be queued');
  await page.keyboard.press('Enter');

  const queued = await waitFor(
    'queued capture',
    async () => {
      const s = await optionsPage.evaluate(
        () => new Promise((r) => chrome.runtime.sendMessage({ type: 'status' }, r)),
      );
      return s && s.queued === 1;
    },
    20000,
  );
  check('a failed POST is queued in chrome.storage.local', queued);

  const failedStatus = await optionsPage.evaluate(
    () => new Promise((r) => chrome.runtime.sendMessage({ type: 'status' }, r)),
  );
  check('last error is recorded for the popup', !!(failedStatus && failedStatus.lastError && failedStatus.lastError.message), JSON.stringify(failedStatus && failedStatus.lastError));
  check('listener is reported unreachable', failedStatus && failedStatus.reachable === false, JSON.stringify(failedStatus && failedStatus.reachable));
  mockCheck('the queued prompt never reached the real listener', received.prompts.length === 1, `prompts: ${received.prompts.length}`);

  const cleared = await optionsPage.evaluate(
    () => new Promise((r) => chrome.runtime.sendMessage({ type: 'clearQueue' }, r)),
  );
  check('clearQueue empties the queue', cleared && cleared.ok === true, JSON.stringify(cleared));
  const afterClear = await optionsPage.evaluate(
    () => new Promise((r) => chrome.runtime.sendMessage({ type: 'status' }, r)),
  );
  check('queue is empty after clearQueue', afterClear && afterClear.queued === 0, JSON.stringify(afterClear && afterClear.queued));

  // 7. claude.ai: source mapping, the DOM account fallback, and the single
  //    most important safety rule - an unknown account captures nothing.
  await optionsPage.evaluate((url) => chrome.storage.sync.set({ serverUrl: url }), listenerUrl);
  currentFixture = FIXTURE_CLAUDE;
  const claudePage = await context.newPage();
  await claudePage.goto(CLAUDE_URL, { waitUntil: 'domcontentloaded' });
  await claudePage.waitForSelector('fieldset div[contenteditable="true"]');
  await claudePage.waitForTimeout(1500);
  check('claude current-account endpoints were tried', received.currentAccount > 0 && received.account > 0,
    `current_account: ${received.currentAccount}, account: ${received.account}`);

  const before = await delivered();
  await claudePage.click('fieldset div[contenteditable="true"]');
  await claudePage.keyboard.type('this must NOT be captured, the account is unknown');
  await claudePage.keyboard.press('Enter');
  await claudePage.waitForTimeout(2500);
  check('nothing is captured while the account is unknown', (await delivered()) === before,
    `delivered: ${await delivered()}`);

  // Open the profile popover so the DOM fallback has something to read, then
  // bust the 10-minute account cache the way the popup's refresh does.
  await claudePage.evaluate(() => window.__openUserMenu());
  const claudeWhoami = await optionsPage.evaluate(async () => {
    const tabs = await chrome.tabs.query({ url: 'https://claude.ai/*' });
    if (!tabs.length) return { error: 'no claude tab' };
    return await chrome.tabs.sendMessage(tabs[0].id, { type: 'whoami', refresh: true });
  });
  check('claude whoami maps /code/* to claude_code_web', claudeWhoami && claudeWhoami.source === 'claude_code_web', JSON.stringify(claudeWhoami && claudeWhoami.source));
  check('claude DOM fallback found the account', claudeWhoami && claudeWhoami.account === ACCOUNT, JSON.stringify(claudeWhoami && claudeWhoami.account));
  check(
    'no org-roster address was mistaken for the signed-in user',
    !!claudeWhoami && claudeWhoami.account !== DECOY_EMAIL,
    JSON.stringify(claudeWhoami && claudeWhoami.account),
  );
  check('claude conversation id is the last id-like segment', claudeWhoami && claudeWhoami.conversationId === CLAUDE_SESSION_ID, JSON.stringify(claudeWhoami && claudeWhoami.conversationId));
  check('claude title drops the " - Claude" suffix', claudeWhoami && claudeWhoami.title === 'Refactor the parser', JSON.stringify(claudeWhoami && claudeWhoami.title));

  // Now capture via the send button instead of Enter.
  await claudePage.click('fieldset div[contenteditable="true"]');
  await claudePage.keyboard.type('explain the tokenizer');
  await claudePage.click('button[aria-label="Send Message"]');
  const claudeGot = await waitFor('claude capture', async () => (await delivered()) > before, 15000);
  check('send-button click captures on claude.ai', claudeGot, `delivered: ${await delivered()}`);
  if (claudeGot && !E2E) {
    const body = received.prompts[received.prompts.length - 1];
    check('claude source is claude_code_web', body.source === 'claude_code_web', JSON.stringify(body.source));
    check('claude prompt text is right', body.prompt === 'explain the tokenizer', JSON.stringify(body.prompt));
    check('claude session id is right', body.conversation_id === CLAUDE_SESSION_ID, JSON.stringify(body.conversation_id));
    check('claude title is right', body.title === 'Refactor the parser', JSON.stringify(body.title));
  }

  // 8. the worker talks to loopback or to nothing: a non-local serverUrl must be
  //    refused before any fetch, and must not sit in the retry queue either.
  const beforeRemote = await delivered();
  const remote = await optionsPage.evaluate(async () => {
    await chrome.storage.sync.set({ serverUrl: 'https://not-local.example.com' });
    const res = await new Promise((r) =>
      chrome.runtime.sendMessage(
        {
          type: 'capture',
          payload: {
            source: 'chatgpt_web',
            prompt: 'this must never leave the machine',
            account: 'me@work.com',
            url: 'https://chatgpt.com/c/x',
            ts: new Date().toISOString(),
          },
        },
        r,
      ),
    );
    const status = await new Promise((r) => chrome.runtime.sendMessage({ type: 'status' }, r));
    return { res, queued: status && status.queued };
  });
  check(
    'a non-loopback serverUrl is refused by the worker',
    remote && remote.res && remote.res.ok === false && remote.res.reason === 'server_not_loopback',
    JSON.stringify(remote && remote.res),
  );
  check('a refused non-loopback capture is not queued', remote && remote.queued === 0, JSON.stringify(remote && remote.queued));
  mockCheck('nothing extra reached the listener', (await delivered()) === beforeRemote, `delivered: ${await delivered()}`);
  await optionsPage.evaluate((url) => chrome.storage.sync.set({ serverUrl: url }), listenerUrl);

  // 9. the options UI enforces the same rule (and loading it at all proves the
  //    page's module script resolved lib/limits.js).
  const uiPage = await context.newPage();
  await uiPage.goto(`chrome-extension://${extensionId}/options.html`);
  const uiLoaded = await uiPage
    .waitForFunction(() => document.getElementById('serverUrl').value.length > 0, { timeout: 10000 })
    .then(() => true, () => false);
  check('options page script ran and loaded the saved settings', uiLoaded);

  const saveUrl = async (value) => {
    await uiPage.evaluate(() => {
      document.getElementById('status').textContent = '';
    });
    await uiPage.fill('#serverUrl', value);
    await uiPage.click('#save');
    await waitFor('options status', async () => ((await uiPage.textContent('#status')) || '').length > 0, 5000);
    return (await uiPage.textContent('#status')) || '';
  };

  check(
    'options page refuses a non-loopback host',
    /127\.0\.0\.1 or localhost/.test(await saveUrl('http://not-local.example.com')),
    'no loopback complaint',
  );
  check(
    'options page refuses https, even on loopback',
    /http:\/\//.test(await saveUrl('https://127.0.0.1:47821')),
    'no scheme complaint',
  );
  const storedUrl = await uiPage.evaluate(
    () => new Promise((r) => chrome.storage.sync.get({ serverUrl: '' }, (v) => r(v.serverUrl))),
  );
  check('a rejected URL never reaches chrome.storage.sync', storedUrl === listenerUrl, JSON.stringify(storedUrl));
  check('options page accepts the loopback listener', /Saved/.test(await saveUrl(listenerUrl)), 'not saved');

  // 10. an account lookup that is still in flight when the page navigates must
  //     not repopulate the cache afterwards: the identity it describes belongs
  //     to the document we just left, and the next prompt would be stamped with
  //     it. The site answers slowly and with a *different* address, and the
  //     navigation happens while that answer is still on the wire.
  currentFixture = FIXTURE_CHATGPT;
  sessionEmail = STALE_EMAIL;
  sessionDelayMs = 3000;
  const sessionsBefore = received.session;
  const racePage = await context.newPage();
  await racePage.goto(`https://chatgpt.com/c/${RACE_BEFORE_ID}`, { waitUntil: 'domcontentloaded' });
  await racePage.waitForSelector('#prompt-textarea');
  // The content script's warm-up lookup has reached the site and is now waiting
  // on the slow answer: everything below happens inside that window.
  const raceInFlight = await waitFor('the slow account lookup to start', async () => received.session > sessionsBefore, 10000, 100);
  check('the account lookup is in flight', raceInFlight, `session hits: ${received.session}`);

  // From here on the site is a different user: whatever the content script ends
  // up reporting must be *this* identity, never the one still on the wire.
  sessionEmail = ACCOUNT;
  sessionDelayMs = 0;
  // An SPA navigation plus a DOM mutation, which is what drives the content
  // script's own navigation check (it is throttled, hence the second nudge).
  await racePage.evaluate((id) => {
    history.pushState({}, '', `/c/${id}`);
    document.body.appendChild(document.createElement('div'));
  }, RACE_AFTER_ID);
  await racePage.waitForTimeout(800);
  await racePage.evaluate(() => document.body.appendChild(document.createElement('div')));
  // Let the slow answer land (and, with the bug, re-cache the old identity).
  await racePage.waitForTimeout(3000);

  const raceWhoami = await optionsPage.evaluate(async () => {
    const tabs = await chrome.tabs.query({ url: 'https://chatgpt.com/c/race-after-*' });
    if (!tabs.length) return { error: 'no race tab' };
    try {
      return await chrome.tabs.sendMessage(tabs[0].id, { type: 'whoami' });
    } catch (err) {
      return { error: String(err) };
    }
  });
  check(
    'a late account detection cannot outlive the navigation that cleared it',
    raceWhoami && raceWhoami.account === ACCOUNT,
    JSON.stringify(raceWhoami),
  );
}

main()
  .catch((err) => {
    failures.push(`unhandled: ${err && err.stack ? err.stack : err}`);
    console.error(err);
  })
  .finally(async () => {
    try {
      if (context) await context.close();
    } catch {
      /* ignore */
    }
    await new Promise((r) => server.close(r));
    if (userDataDir) {
      try {
        rmSync(userDataDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
    console.log(`\n${failures.length === 0 ? 'SMOKE PASSED' : `SMOKE FAILED (${failures.length})`}`);
    for (const f of failures) console.error(` - ${f}`);
    process.exit(failures.length === 0 ? 0 : 1);
  });
