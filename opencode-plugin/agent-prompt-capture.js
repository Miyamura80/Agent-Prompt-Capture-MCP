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
  try {
    if (globalThis.Bun && typeof globalThis.Bun.spawn === "function") {
      const child = globalThis.Bun.spawn([COMMAND, ...ARGS], {
        stdin: "pipe",
        stdout: "ignore",
        stderr: "ignore",
      });
      child.stdin.write(json);
      child.stdin.end();
      if (typeof child.unref === "function") child.unref();
      return;
    }
  } catch (_) {
    // fall through to node
  }

  import("node:child_process")
    .then(({ spawn }) => {
      const child = spawn(COMMAND, ARGS, {
        stdio: ["pipe", "ignore", "ignore"],
        detached: true,
      });
      child.on("error", () => {});
      child.stdin.on("error", () => {});
      child.stdin.end(json);
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
  const props = (event && event.properties) || {};
  if (props.sessionID) return props.sessionID;
  if (props.info && props.info.id) return props.info.id;
  if (props.part && props.part.sessionID) return props.part.sessionID;
  return null;
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

      send({
        session_id: (input && input.sessionID) || message.sessionID || null,
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
