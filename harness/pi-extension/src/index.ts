/**
 * chassis-pi-extension — wires chassis's tool surface and agent identity into
 * a Pi session. Loaded via `pi -e /usr/local/lib/chassis-ext/index.js`. Reads
 * CHASSIS_AGENT / CHASSIS_TASK (set by run-agent) plus files under
 * $AGENT_HOME/<agent>/ and /etc/chassis/tools-public.json.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { Type } from "@earendil-works/pi-ai";
import type {
  ExtensionAPI,
  ExtensionContext,
} from "@earendil-works/pi-coding-agent";

const AGENT_HOME = process.env.AGENT_HOME || "/home/agent";
const TOOLS_PUBLIC = "/etc/chassis/tools-public.json";
const LOG_DIR = "/var/log/chassis";

interface AgentJson {
  tools?: string[];
  pi_defaults?: string[];
  model?: string;
}

interface PublicTool {
  name: string;
  description: string;
  args_schema: object;
}

function readJson<T>(p: string): T {
  return JSON.parse(fs.readFileSync(p, "utf8")) as T;
}

function readMaybe(p: string): string | null {
  try {
    return fs.readFileSync(p, "utf8");
  } catch {
    return null;
  }
}

function loadAgentContext() {
  const agent = process.env.CHASSIS_AGENT;
  if (!agent) {
    throw new Error("CHASSIS_AGENT not set; run-agent should have exported it.");
  }
  const task = process.env.CHASSIS_TASK || "";
  const dir = path.join(AGENT_HOME, agent);

  const agentJson = readJson<AgentJson>(path.join(dir, "agent.json"));
  const systemMd = readMaybe(path.join(dir, "SYSTEM.md")) || "";
  const taskMd = task
    ? readMaybe(path.join(dir, task, "INSTRUCTIONS.md"))
    : null;

  const tools = readJson<{ tools: PublicTool[] }>(TOOLS_PUBLIC).tools || [];
  const toolsByName: Record<string, PublicTool> = {};
  for (const t of tools) toolsByName[t.name] = t;

  return { agent, task, agentJson, systemMd, taskMd, toolsByName };
}

function makeTool(meta: PublicTool) {
  // Pi wants a TypeBox TSchema for `parameters`. Our public tool registry
  // ships plain JSON Schema, which TypeBox accepts via Type.Unsafe — it wraps
  // the raw schema as a TSchema without altering it, so the validator sees
  // the same JSON Schema we already enforce in the dispatcher.
  const parameters = Type.Unsafe<Record<string, unknown>>(
    meta.args_schema as never,
  );
  return {
    name: meta.name,
    label: meta.name,
    description: meta.description,
    parameters,
    async execute(
      _toolCallId: string,
      params: Record<string, unknown>,
      _signal: AbortSignal | undefined,
      _onUpdate: unknown,
      _ctx: ExtensionContext,
    ) {
      const json = JSON.stringify(params ?? {});
      const result = spawnSync(
        "sudo",
        ["/usr/local/bin/run-tool", meta.name, json],
        { encoding: "utf8" },
      );
      const stdout = result.stdout || "";
      const stderr = result.stderr || "";
      const ok = result.status === 0;
      const text =
        (ok ? stdout : `${stdout}${stderr ? "\n" + stderr : ""}`).trim() ||
        `run-tool exited ${result.status}`;
      return {
        content: [{ type: "text" as const, text }],
        isError: !ok,
      };
    },
  };
}

function writeRunLog(
  agent: string,
  task: string,
  sessionFile: string | undefined,
): void {
  try {
    const sub = task ? task : "interactive";
    const dir = path.join(LOG_DIR, "agents", agent, sub);
    fs.mkdirSync(dir, { recursive: true });
    const ts = new Date().toISOString().replace(/[:.]/g, "-");
    const file = path.join(dir, `${ts}.jsonl`);
    const record = {
      ts: new Date().toISOString(),
      agent,
      task: task || null,
      session_file: sessionFile ?? null,
    };
    fs.writeFileSync(file, JSON.stringify(record) + "\n");
  } catch (err) {
    process.stderr.write(
      `chassis-ext: log write failed: ${(err as Error).message}\n`,
    );
  }
}

export default function (pi: ExtensionAPI): void {
  const { agent, task, agentJson, systemMd, taskMd, toolsByName } =
    loadAgentContext();

  // Registration phase: only registration calls are allowed here.
  for (const name of agentJson.tools ?? []) {
    const meta = toolsByName[name];
    if (!meta) {
      process.stderr.write(
        `chassis-ext: agent ${agent} lists tool ${name} but it is not in tools-public.json\n`,
      );
      continue;
    }
    pi.registerTool(makeTool(meta) as never);
  }

  // Prepend SYSTEM.md to the assembled system prompt every turn.
  pi.on("before_agent_start", async (event) => {
    if (!systemMd.trim()) return;
    return { systemPrompt: systemMd.trim() + "\n\n" + event.systemPrompt };
  });

  // Action methods: must be called from a handler, not the factory body.
  // Pi was launched with --no-builtin-tools, so nothing is active by default;
  // we expose pi_defaults (built-ins this agent is allowed to use) plus this
  // agent's chassis tools. Task instructions are passed via argv by run-agent,
  // so we don't inject a user message here.
  pi.on("session_start", async () => {
    const active = [
      ...(agentJson.pi_defaults ?? []),
      ...(agentJson.tools ?? []),
    ];
    pi.setActiveTools(active);
  });

  // Once-per-session log. agent_end fires per-prompt; session_shutdown is the
  // right hook for a single end-of-session record.
  pi.on("session_shutdown", async (_event, ctx) => {
    const mgr = ctx.sessionManager as unknown as {
      getSessionFile?: () => string | undefined;
    };
    const sessionFile = mgr.getSessionFile?.();
    writeRunLog(agent, task, sessionFile);
  });
}
