// agent-prompt-capture: OpenCode plugin.
//
// Pipes every submitted user message to `apc capture opencode` as JSON on stdin, and
// every `session.idle` transition as a turn-end event. The child is spawned detached and
// never awaited from `chat.message`: that hook is awaited by the session loop, so any
// work done there is latency the user feels. Everything is wrapped in try/catch —
// capture is best effort and must never slow down or break OpenCode.
//
// Per session the children are *ordered*, though: `session.idle` can arrive while the
// prompt's child is still starting up, and a turn_end that runs first finds no row to
// stamp and is lost. Each session gets a promise chain, so its turn_end child is only
// spawned once the prompt child has exited.
//
// Install with `apc install opencode` (copies this file to
// ~/.config/opencode/plugin/agent-prompt-capture.js).

const COMMAND = "apc";
const ARGS = ["capture", "opencode"];
// A capture writes one row; if the child has not exited by then it never will, and the
// session's next event must not wait on it forever.
const SPAWN_TIMEOUT_MS = 10000;

/**
 * Spawn `apc capture opencode`, feed it `json`, and resolve when the child has exited
 * (or could not be started at all). The promise never rejects.
 */
function spawnDetached(json) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    let kill = null; // set once a child exists; called if it outlives the timer

    function done() {
      if (settled) return;
      settled = true;
      if (timer !== null) clearTimeout(timer);
      resolve();
    }

    function expire() {
      // A capture writes one row; a child still alive after SPAWN_TIMEOUT_MS never
      // finishes, so terminate it rather than rely on unref() alone (older Bun
      // builds have no unref, and a hung child must not keep OpenCode alive).
      if (kill !== null) {
        try {
          kill();
        } catch (_) {
          // already gone
        }
      }
      done();
    }

    // The child is unref'd below (a hung `apc capture` must never keep OpenCode
    // alive), so this timer is what holds the event loop open instead: a caller that
    // awaits this promise (the per-session chain, or a short-lived host such as the
    // e2e driver) must not drain before the child exits. Events on an unref'd child
    // still fire while the timer runs, and done() clears the timer, so a healthy
    // child releases the host as soon as it exits and a hung one after
    // SPAWN_TIMEOUT_MS. The timer itself is deliberately NOT unref'd.
    timer = setTimeout(expire, SPAWN_TIMEOUT_MS);

    // Bun first (OpenCode's server runs on Bun), node:child_process otherwise.
    let spawned = null;
    try {
      if (globalThis.Bun && typeof globalThis.Bun.spawn === "function") {
        // Bun.spawn throws synchronously when `apc` is not on PATH; that is the only
        // reason to fall through to node. A failure AFTER the spawn must not retry,
        // or one prompt is captured twice.
        spawned = globalThis.Bun.spawn([COMMAND, ...ARGS], {
          stdin: "pipe",
          stdout: "ignore",
          stderr: "ignore",
        });
      }
    } catch (_) {
      spawned = null; // fall through to node
    }

    if (spawned) {
      kill = () => spawned.kill();
      try {
        // Detach from the host's event loop: a hung child must not keep it alive.
        if (typeof spawned.unref === "function") spawned.unref();
      } catch (_) {
        // older Bun without unref(): expire() kills the child on timeout instead
      }
      try {
        spawned.stdin.write(json);
        spawned.stdin.end();
      } catch (_) {
        // the child died before it read us; nothing to salvage, never retry
        done();
        return;
      }
      if (spawned.exited && typeof spawned.exited.then === "function") {
        spawned.exited.then(done, done);
      } else {
        done();
      }
      return;
    }

    import("node:child_process")
      .then(({ spawn }) => {
        const child = spawn(COMMAND, ARGS, {
          stdio: ["pipe", "ignore", "ignore"],
          detached: true,
        });
        kill = () => child.kill();
        // Detach from the host's event loop: a hung child must not keep it alive.
        // The SPAWN_TIMEOUT_MS timer above, not the child, is what keeps a
        // short-lived host running long enough to see "close". The stdin pipe is
        // unref'd too, or a large prompt the child never drains would keep the
        // pending write (and the host) referenced.
        child.unref();
        // A missing `apc` surfaces as an async "error" event, never a throw.
        child.on("error", () => done());
        child.on("close", () => done());
        if (child.stdin) {
          child.stdin.on("error", () => {});
          if (typeof child.stdin.unref === "function") child.stdin.unref();
          // end() both writes and closes the pipe; "close" fires once the child exits.
          child.stdin.end(json);
        }
      })
      .catch(() => done());
  });
}

function send(payload) {
  try {
    return spawnDetached(JSON.stringify(payload));
  } catch (_) {
    return Promise.resolve(); // never propagate
  }
}

function basename(p) {
  if (!p) return null;
  const parts = String(p).replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : null;
}

function promptText(parts) {
  if (!Array.isArray(parts)) return "";
  return parts
    .filter((p) => p && p.type === "text" && !p.synthetic && !p.ignored)
    .map((p) => p.text || "")
    .join("\n");
}

function attachmentCount(parts) {
  if (!Array.isArray(parts)) return 0;
  return parts.filter((p) => p && p.type === "file").length;
}

function sessionIdOf(event) {
  // session.idle carries `properties.sessionID`, but session.created/updated/deleted
  // put the id at `properties.info.id` instead. Getting this wrong is the easiest
  // mistake in the whole plugin (hook-specs.md 6.c).
  const props = (event && event.properties) || {};
  if (props.sessionID) return props.sessionID;
  if (props.info && props.info.id) return props.info.id;
  if (props.part && props.part.sessionID) return props.part.sessionID;
  return null;
}

// session.idle also fires for sub-sessions and for sessions that were already running
// when this plugin loaded. We have no open turn for those, so emitting a turn end
// would spawn a process for nothing. Remember the sessions we actually sent a prompt
// for, bounded so a long-lived server cannot grow this without limit.
const OPEN_SESSIONS = new Set();
const MAX_OPEN_SESSIONS = 512;

function rememberSession(sessionID) {
  if (!sessionID) return;
  if (OPEN_SESSIONS.size >= MAX_OPEN_SESSIONS) {
    // Sets iterate in insertion order, so this drops the oldest.
    OPEN_SESSIONS.delete(OPEN_SESSIONS.values().next().value);
  }
  OPEN_SESSIONS.add(sessionID);
}

// One promise chain per session: the prompt's child must have exited before the
// turn_end child starts, or `apc capture` finds no open turn to stamp. Different
// sessions never wait on each other.
const SESSION_CHAINS = new Map();

function enqueueForSession(sessionID, payload) {
  const key = sessionID || "";
  const previous = SESSION_CHAINS.get(key);
  const next = (previous || Promise.resolve()).then(
    () => send(payload),
    () => send(payload),
  );
  if (!SESSION_CHAINS.has(key) && SESSION_CHAINS.size >= MAX_OPEN_SESSIONS) {
    // Maps iterate in insertion order, so this drops the oldest.
    SESSION_CHAINS.delete(SESSION_CHAINS.keys().next().value);
  }
  SESSION_CHAINS.set(key, next);
  const forget = () => {
    if (SESSION_CHAINS.get(key) === next) SESSION_CHAINS.delete(key);
  };
  next.then(forget, forget);
  return next;
}

export const AgentPromptCapture = async ({ project, client, $, directory, worktree }) => ({
  "chat.message": async (input, output) => {
    try {
      const parts = (output && output.parts) || [];
      const prompt = promptText(parts);
      if (!prompt || !prompt.trim()) return; // image-only turn or a bare @agent mention

      const message = (output && output.message) || {};
      const created = message.time && message.time.created;
      const model = input && input.model
        ? `${input.model.providerID}/${input.model.modelID}`
        : null;

      const sessionID = (input && input.sessionID) || message.sessionID || null;
      rememberSession(sessionID);

      // Deliberately not awaited: the chain only orders this session's children.
      enqueueForSession(sessionID, {
        session_id: sessionID,
        cwd: directory || null,
        project: (project && project.id) || basename(worktree) || null,
        model,
        agent: (input && input.agent) || message.agent || null,
        prompt,
        ts: new Date(typeof created === "number" ? created : Date.now()).toISOString(),
        messageID: message.id || (input && input.messageID) || null,
        attachments: attachmentCount(parts),
      });
    } catch (_) {
      // capture is best effort
    }
  },

  event: async ({ event }) => {
    try {
      if (!event || event.type !== "session.idle") return;
      const sessionID = sessionIdOf(event);
      if (!sessionID) return;
      if (!OPEN_SESSIONS.has(sessionID)) return; // no open turn of ours: nothing to end
      OPEN_SESSIONS.delete(sessionID);
      // `ts` is stamped now, not when the child finally runs, so a queued turn end
      // still records when the session actually went idle.
      await enqueueForSession(sessionID, {
        event: "turn_end",
        session_id: sessionID,
        ts: new Date().toISOString(),
      });
    } catch (_) {
      // capture is best effort
    }
  },
});

export default AgentPromptCapture;
