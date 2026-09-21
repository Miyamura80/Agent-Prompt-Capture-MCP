# Agent Prompt Capture - Chrome extension

Captures the prompts you submit on **claude.ai** and **chatgpt.com** and forwards
them to the local `apc serve` listener, which scrubs them before anything is
written to disk.

Manifest V3, plain JavaScript, **no build step**. The directory you check out is
the directory you load into Chrome.

## What it does

```
  BROWSER (this extension)                        YOUR MACHINE ONLY
  +--------------------------------------+
  |  content/common.js                   |
  |    capture-phase listeners on         |
  |    document: keydown Enter /          |
  |    click send / form submit           |
  |             |                         |
  |             v                         |
  |    read composer text BEFORE the      |
  |    app clears it  (innerText)         |
  |             |                         |
  |             v                         |
  |    account known?  -----no---->  drop |
  |    allowlisted?    -----no---->  drop |
  |    source enabled? -----no---->  drop |
  |             | yes                     |
  |             v                         |
  |    chrome.runtime.sendMessage         |
  |      {type:"capture", payload}        |
  +-------------|------------------------+
                v
  +--------------------------------------+
  |  background.js (service worker)      |
  |    lib/prescrub.js                   |
  |      emails -> [EMAIL]               |
  |      sk-/ghp_/AKIA/xox/JWT ->        |
  |                      [API_KEY]       |
  |             |                        |         +-------------------------+
  |             v                        |  POST   |  apc serve              |
  |    fetch(serverUrl + /v1/prompts)    |-------->|  127.0.0.1:47821        |
  |      X-APC-Token: <token>            |         |    full pii.scrub()     |
  |             |                        |         |    SQLite + FTS5        |
  |          on failure                  |         +-------------------------+
  |             v                        |
  |    chrome.storage.local queue        |
  |      cap 500 items / 4 MiB,          |
  |      drop oldest                     |
  |      chrome.alarms every 1 min       |
  |      backoff 1,2,4,...,60 min        |
  +--------------------------------------+
```

`serverUrl` is the **only** origin this extension ever sends anything to, and
the worker refuses to use it at all unless it is `http://127.0.0.1` or
`http://localhost` (`lib/limits.js#isLoopbackServerUrl`, enforced before every
`fetch`; the options page rejects the same URLs at save time).

Source mapping (matches `ARCHITECTURE.md`):

| URL                  | source id         |
|----------------------|-------------------|
| `claude.ai/code/*`   | `claude_code_web` |
| `claude.ai/*`        | `claude_web`      |
| `chatgpt.com/codex*` | `codex_cloud`     |
| `chatgpt.com/*`      | `chatgpt_web`     |

## Install (load unpacked)

1. Start the listener and grab the token:

   ```sh
   apc serve            # binds 127.0.0.1:47821
   apc token            # prints the shared secret
   ```

2. Open `chrome://extensions`, turn on **Developer mode** (top right).
3. Click **Load unpacked** and pick this `extension/` directory.
4. Open the extension's **Options** (from the puzzle-piece menu, or the
   *Options* link in the popup) and fill in:
   * **Server URL** - leave as `http://127.0.0.1:47821` unless you changed
     `[server]` in `config.toml`.
   * **Token** - paste the output of `apc token`.
   * **Allowed accounts** - one email per line. **An empty list captures
     nothing.** These must also appear in `[capture] allowed_accounts` in
     `config.toml`, because the listener enforces the allowlist again.
   * **Sources** - untick any of the four you do not want captured.
5. Click **Save**, then **Test connection**. You should see
   `Connected to http://127.0.0.1:47821 v<version>`.
6. Reload any claude.ai / chatgpt.com tabs that were already open. Content
   scripts are only injected on load.

## Verify it works

1. Open `https://chatgpt.com/` and click the extension icon. The popup should show:

   ```
   Listener      yes - http://127.0.0.1:47821
   Account       me@work.com
   Allowlisted   yes
   Source        chatgpt_web
   ```

   If **Account** says `unknown`, nothing will be captured. Open your profile
   menu in the page once and re-open the popup (on claude.ai the DOM fallback
   can only read the address while that popover is open).

2. Send a prompt, then check it landed:

   ```sh
   apc list --source chatgpt_web --limit 1
   ```

3. Re-open the popup: **Last 24h** should have gone up and **Queued** should be `0`.

### Troubleshooting

| Symptom | Cause |
|---|---|
| `Account: content script not loaded` | The tab predates the install. Reload it. |
| `Account: unknown` | The private account endpoints changed shape or 404'd. Open the profile popover and re-open the popup. |
| `Allowlisted: no` | The address is missing from Options (and probably from `config.toml`). |
| `Listener: no` | `apc serve` is not running, or the port differs. |
| `Queued: 12` and rising | Captures are buffered because POSTs are failing. Fix the listener; the queue drains on the next 1-minute alarm tick. **Clear queue** in Options throws them away. Queued captures are re-checked against the current settings when they are retried: turning a source off, or removing an account from the allowlist, drops the ones that no longer pass instead of sending them. |
| `Last 24h` did not move although the POST succeeded | The listener answered `202 {"stored": false}` - a disabled source, an account missing from `config.toml`, or a duplicate within its 5 s dedup window. Only stored records are counted. |
| Nothing captured and everything looks green | The site changed its composer markup. The composer/send selectors live at the top of `content/claude.js` and `content/chatgpt.js`. |

## Privacy

**What leaves the browser, and where it goes**

* A capture is one `POST` to the configured `serverUrl` (default
  `http://127.0.0.1:47821`, i.e. your own machine) with this body, and nothing
  else ever leaves:

  ```json
  {
    "source": "chatgpt_web",
    "prompt": "your prompt text, lightly pre-scrubbed",
    "account": "me@work.com",
    "conversation_id": "uuid-or-null",
    "url": "https://chatgpt.com/c/uuid",
    "title": "conversation title or null",
    "ts": "2026-09-19T20:11:03.123Z",
    "client_version": "0.1.0"
  }
  ```

* There is **no telemetry, no analytics, and no remote code**. `host_permissions`
  covers `claude.ai`, `chatgpt.com`, `127.0.0.1` and `localhost` only, and the
  worker's only `fetch` target is `serverUrl`.
* Model *responses* are never read. Only the composer text you submit.

**Allowlist semantics**

* Nothing is captured unless the detected account is on the allowlist. Unknown
  account means no capture; an empty allowlist means no capture.
* Checked three times: in the content script (before anything is sent to the
  worker), in the worker (before the POST), and in the listener (`ingest.py`,
  which is the authority).
* `account` is sent as the raw address because the listener needs it for that
  check. It never reaches the database in that form: `config.toml`'s
  `[accounts]` aliases it, and un-aliased addresses are stored as
  `sha256:<12 hex>`.

**Scrubbing**

* The extension applies a deliberately small pre-scrub in `lib/prescrub.js`
  (emails, `sk-`/`sk-ant-`/`ghp_`/`github_pat_`/`AKIA`/`xox*` tokens, JWTs) as
  defence in depth for the hop to localhost.
* The **real** scrub is server-side in `pii.py` (phones, IPs, cards, IBAN, SSN,
  private keys, home paths, your own extra terms, optional NER). Raw prompt text
  never touches disk.
* Only `prompt` is pre-scrubbed. `url` and `title` are scrubbed server-side.
* **The pre-scrub's markers are flat (`[EMAIL]`, `[API_KEY]`), not numbered like
  the listener's `[EMAIL_1]` / `[API_KEY_2]`.** The listener cannot renumber or
  count what the browser already replaced - the raw value is gone before the
  POST - so anything scrubbed here contributes nothing to the record's
  `pii_findings`, and two different addresses in one prompt both read `[EMAIL]`.
  Writing `[EMAIL_1]` here instead would be worse than useless: the server
  numbers from 1 per record over the text it is given, and its `api_key`
  patterns are wider than ours (`Bearer ...`, `password = ...`), so one
  placeholder could end up standing for two different secrets. A flat marker
  can never collide with a server placeholder, so it is always clear which side
  scrubbed what.

**Local storage**

* `chrome.storage.sync`: `serverUrl`, `allowedAccounts`, per-source toggles.
* `chrome.storage.local`: the **token** (deliberately not synced), the retry
  queue (500 items *and* 4 MiB of serialized JSON, oldest dropped first, so it
  cannot run into the 10 MiB quota), capture timestamps for the 24-hour counter,
  and the last error. Every read-modify-write of the queue is serialized behind
  one promise chain so a capture and an alarm flush cannot overwrite each other.
* The detected account is cached **in the content script's own memory only**,
  for 10 minutes, per tab. It is deliberately not kept in
  `chrome.storage.session` (which every tab shares, so a second tab signed in
  as somebody else would read the first one's identity), only successful
  detections are cached (a cached `null` would otherwise block capture until it
  expired), and any URL change - sign-out and account switches both navigate -
  drops it.
* Captures waiting in the retry queue hold pre-scrubbed prompt text in
  `chrome.storage.local` until they are delivered. Use **Clear queue** in
  Options to discard them.

## Layout

```
extension/
  manifest.json          MV3 manifest
  background.js          service worker (ES module): pre-scrub, POST, queue, retry
  lib/prescrub.js        the pre-scrub, shared with the node test
  lib/limits.js          byte budgets + the loopback rule (worker + options page)
  content/common.js      capture plumbing shared by both sites
  content/claude.js      claude.ai selectors + account detection
  content/chatgpt.js     chatgpt.com selectors + account detection
  options.html/.js       settings
  popup.html/.js         status
  styles.css             shared, dark-mode aware
  icons/                 generated PNGs (16/32/48/128)
  scripts/
    make-icons.js        regenerates icons/ (zlib PNG encoder, no image tooling)
    test-prescrub.js     unit tests for lib/prescrub.js
    test-limits.js       unit tests for lib/limits.js
    smoke.mjs            Playwright end-to-end test against local fixtures
    fixtures/            offline stand-ins for the two composers
```

## Development

```sh
# syntax check everything
find extension -name '*.js' -o -name '*.mjs' | xargs -n1 node --check
node -e "JSON.parse(require('fs').readFileSync('extension/manifest.json','utf8'))"

# unit tests (no dependencies)
node extension/scripts/test-prescrub.js
node extension/scripts/test-limits.js
# or: cd extension && npm test

# end-to-end: needs Playwright and its Chromium, declared as a devDependency:
#   cd extension && npm install && npx playwright install chromium
# (smoke.mjs also accepts a global playwright install, and only falls back to
# PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers when that directory exists and the
# variable is unset.)
# It loads the unpacked extension into Chromium against local
# fixtures served as https://chatgpt.com/** and https://claude.ai/**, and
# asserts the POST the listener would receive. Re-execs itself under
# xvfb-run when there is no DISPLAY (Chromium will not load extensions
# without a browser UI).
node extension/scripts/smoke.mjs

# regenerate icons after changing the design in make-icons.js
node extension/scripts/make-icons.js
```

`package.json` marks these `.js` files as ES modules and declares the one test
dependency (Playwright, for `scripts/smoke.mjs`). Chrome ignores it: there is
still no build step, and nothing has to be installed to load the extension or to
run the unit tests.

### Notes for whoever maintains the selectors

Both sites are unversioned SPAs. The parts most likely to rot:

* `COMPOSER_SELECTORS` / `SEND_SELECTORS` in `content/claude.js` and
  `content/chatgpt.js`.
* Account detection. Both sites read a **current-user** field and nothing else,
  because the whole contract is "allowlisted accounts only": guessing the wrong
  identity would file your prompts under a colleague's allowlisted address,
  which is worse than capturing nothing.
  * chatgpt.com: `user.email` from `/api/auth/session`. No fallback - the rest
    of that payload may carry unrelated addresses.
  * claude.ai: `/api/auth/current_account`, then `/api/account`, reading only
    `email_address`/`email` on the document root or on its `account`,
    `current_account`, `user` or `profile` object; then the DOM fallback, which
    reads the profile popover while it is open. `/api/organizations` is
    deliberately **not** used: it answers with the organisations the session
    belongs to, and those can list other members.
  * Anything else - a changed shape, a 404, a roster with no current-user
    field - means `unknown account`, which means no capture.

Listeners are attached **once, at `document` level, in the capture phase**, so an
SPA re-render of the composer cannot detach them. Nothing here ever calls
`preventDefault()` or `stopPropagation()`: if the extension breaks, the page
still works.
