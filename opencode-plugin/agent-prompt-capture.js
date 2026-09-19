// agent-prompt-capture: OpenCode plugin.
//
// Pipes every submitted user message to `apc capture opencode` as JSON on stdin, and
// every `session.idle` transition as a turn-end event. The child is spawned detached and
// never awaited: `chat.message` is awaited by the session loop, so any work done here is
// latency the user feels. Everything is wrapped in try/catch — capture is best effort and
// must never slow down or break OpenCode.
//
// Install with `apc install opencode` (copies this file to
// ~/.config/opencode/plugin/agent-prompt-capture.js).

const COMMAND = "apc";
const ARGS = ["capture", "opencode"];

function spawnDetached(json) {
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
    try {
      spawned.stdin.write(json);
      spawned.stdin.end();
      if (typeof spawned.unref === "function") spawned.unref();
    } catch (_) {
      // the child died before it read us; nothing to salvage, never retry
    }
    return;
  }

  import("node:child_process")
    .then(({ spawn }) => {
      const child = spawn(COMMAND, ARGS, {
        stdio: ["pipe", "ignore", "ignore"],
        detached: true,
      });
      // A missing `apc` surfaces as an async "error" event, never a throw.
      child.on("error", () => {});
      if (child.stdin) {
        child.stdin.on("error", () => {});
        // end() both writes and closes the pipe: that close, not unref(), is what
        // lets the parent's event loop drain. unref() is belt and braces.
        child.stdin.end(json);
        if (typeof child.stdin.unref === "function") child.stdin.unref();
      }
      child.unref();
    })
    .catch(() => {});
}

function send(payload) {
  try {
    spawnDetached(JSON.stringify(payload));
  } catch (_) {
    // never propagate
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

      send({
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
      send({
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
