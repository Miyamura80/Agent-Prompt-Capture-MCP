/**
 * Drive the REAL installed OpenCode plugin.
 *
 *   node scripts/opencode_driver.mjs <path-to-installed-plugin.js> \
 *        --session ses_1 --created <epoch-ms> --text "..." --synthetic "..." \
 *        --idle-delay-ms 1500
 *
 * Imports the copy that `apc install opencode` wrote into
 * ~/.config/opencode/plugin/, calls the exported factory with the PluginInput
 * shape OpenCode passes (docs/research/hook-specs.md §3.2), then fires
 * `chat.message` with a realistic input/output pair and `event` with a
 * `session.idle` event.
 *
 * The plugin spawns `apc capture opencode` detached, so `apc` must be on PATH
 * (scripts/e2e.py prepends .venv/bin). Nothing here talks to the database; the
 * caller polls it.
 */

import { pathToFileURL } from 'node:url';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 && i + 1 < process.argv.length ? process.argv[i + 1] : fallback;
}

const pluginPath = process.argv[2];
if (!pluginPath) {
  console.error(
    'usage: opencode_driver.mjs <plugin.js> [--session S] [--created MS] [--text T]' +
      ' [--synthetic T] [--idle-delay-ms N]',
  );
  process.exit(2);
}

const SESSION = arg('session', 'ses_1');
// `chat.message` and `event` each spawn a DETACHED `apc capture opencode`. Fired
// back to back, the turn-end child can reach the database before the prompt row
// exists, and `mark_turn_end` then has nothing to stamp. Real OpenCode puts a
// whole agent turn between the two, so wait here instead of pretending otherwise.
const IDLE_DELAY_MS = Number(arg('idle-delay-ms', '1500'));
const CREATED = Number(arg('created', String(Date.now())));
const TEXT = arg('text', 'refactor the tokenizer');
const SYNTHETIC = arg('synthetic', 'Use the above message and context to generate a prompt');
const MESSAGE_ID = 'msg_e2e_1';

const mod = await import(pathToFileURL(pluginPath).href);
const factory = mod.default ?? mod.AgentPromptCapture;
if (typeof factory !== 'function') {
  console.error(`the plugin at ${pluginPath} exports no factory (default / AgentPromptCapture)`);
  process.exit(1);
}

const hooks = await factory({
  project: { id: 'proj' },
  client: {},
  $: undefined,
  directory: '/Users/alice/dev/proj',
  worktree: '/Users/alice/dev/proj',
});

for (const name of ['chat.message', 'event']) {
  if (typeof hooks[name] !== 'function') {
    console.error(`plugin does not implement the ${name} hook`);
    process.exit(1);
  }
}
console.log(`loaded plugin from ${pluginPath}; hooks: ${Object.keys(hooks).join(', ')}`);

const model = { providerID: 'anthropic', modelID: 'claude-sonnet-4' };
const part = (id, extra) => ({ id, sessionID: SESSION, messageID: MESSAGE_ID, ...extra });

const input = {
  sessionID: SESSION,
  agent: 'build',
  model,
  messageID: MESSAGE_ID,
};
const output = {
  message: {
    id: MESSAGE_ID,
    sessionID: SESSION,
    role: 'user',
    time: { created: CREATED },
    agent: 'build',
    model,
  },
  parts: [
    part('prt_text', { type: 'text', text: TEXT, time: { start: CREATED } }),
    // The resolver appends this for an @agent mention; the user never typed it.
    part('prt_synth', { type: 'text', text: SYNTHETIC, synthetic: true }),
    part('prt_file', {
      type: 'file',
      mime: 'image/png',
      filename: 'architecture.png',
      url: 'file:///Users/alice/dev/proj/architecture.png',
    }),
  ],
};

await hooks['chat.message'](input, output);
console.log('fired chat.message (1 real text part, 1 synthetic text part, 1 file part)');

if (IDLE_DELAY_MS > 0) await new Promise((r) => setTimeout(r, IDLE_DELAY_MS));

await hooks.event({ event: { type: 'session.idle', properties: { sessionID: SESSION } } });
console.log(`fired event session.idle for ${SESSION} (after ${IDLE_DELAY_MS}ms)`);

// The plugin spawns detached children and never awaits them; give the dynamic
// import + spawn + stdin write a moment to complete before this process exits.
await new Promise((r) => setTimeout(r, 1000));
