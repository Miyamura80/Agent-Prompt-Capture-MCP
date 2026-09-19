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
const LINE_ONE = 'summarise the changelog';
const LINE_TWO = `and cc ${SECRET_EMAIL}`;

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

process.env.PLAYWRIGHT_BROWSERS_PATH ||= '/opt/pw-browsers';

async function loadPlaywright() {
  try {
    return await import('playwright');
  } catch {
    /* not resolvable from here; fall through to the global install */
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
  throw new Error(`Could not resolve the playwright package (looked in: ${candidates.join(', ')})`);
}

/* ------------------------------------------------------------ local server */

const received = { prompts: [], health: 0, config: 0, session: 0, organizations: 0, account: 0, unauthorised: 0 };
// The fixture the catch-all serves; flipped when the claude phase starts.
let currentFixture = FIXTURE_CHATGPT;

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
    json(res, 200, { user: { id: 'user-1', email: ACCOUNT, name: 'Test User' }, expires: '2030-01-01T00:00:00Z' });
    return;
  }

  // claude.ai account endpoints: both 404 so the smoke test exercises the
  // DOM fallback (and proves two dead endpoints are handled gracefully).
  if (url.pathname === '/api/organizations') {
    received.organizations += 1;
    json(res, 404, { error: 'not found' });
    return;
  }
  if (url.pathname === '/api/account' || url.pathname === '/api/bootstrap') {
    received.account += 1;
    json(res, 404, { error: 'not found' });
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
    { serverUrl, token: TOKEN, account: ACCOUNT },
  );
  check('options page loaded and seeded', true);

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

  const got = await waitFor('POST /v1/prompts', async () => received.prompts.length > 0, 20000);
  check('listener received a POST /v1/prompts', got, `prompts: ${received.prompts.length}`);

  // the page's own handler must still have run (we never preventDefault)
  const pageSubmitted = await page.evaluate(() => window.__lastSubmitted || null);
  check('page submit handler still ran (no preventDefault from us)', !!pageSubmitted, String(pageSubmitted));

  if (got) {
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
  check('testConnection hit /v1/health and /v1/config', test && test.ok === true && received.config > 0, JSON.stringify(test));
  check('no request was rejected for a bad token', received.unauthorised === 0, `401s: ${received.unauthorised}`);

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
  check('the queued prompt never reached the real listener', received.prompts.length === 1, `prompts: ${received.prompts.length}`);

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
  await optionsPage.evaluate((url) => chrome.storage.sync.set({ serverUrl: url }), serverUrl);
  currentFixture = FIXTURE_CLAUDE;
  const claudePage = await context.newPage();
  await claudePage.goto(CLAUDE_URL, { waitUntil: 'domcontentloaded' });
  await claudePage.waitForSelector('fieldset div[contenteditable="true"]');
  await claudePage.waitForTimeout(1500);
  check('claude account endpoints were tried and 404d', received.organizations > 0 && received.account > 0,
    `orgs: ${received.organizations}, account: ${received.account}`);

  const before = received.prompts.length;
  await claudePage.click('fieldset div[contenteditable="true"]');
  await claudePage.keyboard.type('this must NOT be captured, the account is unknown');
  await claudePage.keyboard.press('Enter');
  await claudePage.waitForTimeout(2500);
  check('nothing is captured while the account is unknown', received.prompts.length === before,
    `prompts: ${received.prompts.length}`);

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
  check('claude conversation id is the last id-like segment', claudeWhoami && claudeWhoami.conversationId === CLAUDE_SESSION_ID, JSON.stringify(claudeWhoami && claudeWhoami.conversationId));
  check('claude title drops the " - Claude" suffix', claudeWhoami && claudeWhoami.title === 'Refactor the parser', JSON.stringify(claudeWhoami && claudeWhoami.title));

  // Now capture via the send button instead of Enter.
  await claudePage.click('fieldset div[contenteditable="true"]');
  await claudePage.keyboard.type('explain the tokenizer');
  await claudePage.click('button[aria-label="Send Message"]');
  const claudeGot = await waitFor('claude capture', async () => received.prompts.length > before, 15000);
  check('send-button click captures on claude.ai', claudeGot, `prompts: ${received.prompts.length}`);
  if (claudeGot) {
    const body = received.prompts[received.prompts.length - 1];
    check('claude source is claude_code_web', body.source === 'claude_code_web', JSON.stringify(body.source));
    check('claude prompt text is right', body.prompt === 'explain the tokenizer', JSON.stringify(body.prompt));
    check('claude session id is right', body.conversation_id === CLAUDE_SESSION_ID, JSON.stringify(body.conversation_id));
    check('claude title is right', body.title === 'Refactor the parser', JSON.stringify(body.title));
  }
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
