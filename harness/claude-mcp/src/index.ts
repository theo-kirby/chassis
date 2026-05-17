/**
 * chassis-mcp — stdio MCP server that wires chassis's tool surface and agent
 * identity into a Claude Code session. Launched via `claude --mcp-config ...`,
 * which spawns `node /usr/local/lib/chassis-mcp/index.js` and sets
 * CHASSIS_AGENT in the child's env. Reads /home/agent/<agent>/agent.json and
 * /etc/chassis/tools-public.json, registers an MCP tool per declared chassis
 * tool, and dispatches each call through `sudo /usr/local/bin/run-tool`.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const AGENT_HOME = "/home/agent";
const TOOLS_PUBLIC = "/etc/chassis/tools-public.json";
const LOG_DIR = "/var/log/chassis";

interface AgentJson {
  tools?: string[];
  claude_defaults?: string[];
  model?: string;
}

interface PublicTool {
  name: string;
  description: string;
  args_schema: Record<string, unknown>;
}

function readJson<T>(p: string): T {
  return JSON.parse(fs.readFileSync(p, "utf8")) as T;
}

function loadAgentContext() {
  const agent = process.env.CHASSIS_AGENT;
  if (!agent) {
    throw new Error(
      "CHASSIS_AGENT not set; run-claude should have exported it via --mcp-config env."
    );
  }
  const task = process.env.CHASSIS_TASK || "";
  const dir = path.join(AGENT_HOME, agent);

  const agentJson = readJson<AgentJson>(path.join(dir, "agent.json"));
  const tools = readJson<{ tools: PublicTool[] }>(TOOLS_PUBLIC).tools || [];
  const toolsByName: Record<string, PublicTool> = {};
  for (const t of tools) toolsByName[t.name] = t;

  return { agent, task, agentJson, toolsByName };
}

function writeRunLog(agent: string, task: string): void {
  // One JSON line per session, mirroring the pi-extension shape so the
  // dashboard's agent drill-in works unchanged. Claude Code doesn't expose a
  // session-file path the way Pi did, so session_file is null.
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
      session_file: null,
    };
    fs.writeFileSync(file, JSON.stringify(record) + "\n");
  } catch (err) {
    process.stderr.write(
      `chassis-mcp: log write failed: ${(err as Error).message}\n`
    );
  }
}

async function main(): Promise<void> {
  const { agent, task, agentJson, toolsByName } = loadAgentContext();

  // Resolve which tools to expose. Agents declare names in agent.json:tools;
  // we look each up in tools-public.json. Unknown names are skipped with a
  // warning to stderr (Claude Code surfaces stderr in `claude --debug`).
  const exposed: PublicTool[] = [];
  for (const name of agentJson.tools ?? []) {
    const meta = toolsByName[name];
    if (!meta) {
      process.stderr.write(
        `chassis-mcp: agent ${agent} lists tool ${name} but it is not in tools-public.json\n`
      );
      continue;
    }
    exposed.push(meta);
  }

  const server = new Server(
    { name: "chassis", version: "0.1.0" },
    { capabilities: { tools: {} } }
  );

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: exposed.map((t) => ({
      name: t.name,
      description: t.description,
      inputSchema: (t.args_schema as Record<string, unknown>) || {
        type: "object",
      },
    })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const name = req.params.name;
    const meta = toolsByName[name];
    if (!meta) {
      return {
        content: [{ type: "text" as const, text: `unknown tool: ${name}` }],
        isError: true,
      };
    }
    const args = req.params.arguments ?? {};
    const json = JSON.stringify(args);
    const result = spawnSync(
      "sudo",
      ["/usr/local/bin/run-tool", name, json],
      { encoding: "utf8" }
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
  });

  writeRunLog(agent, task);

  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  process.stderr.write(`chassis-mcp: ${(err as Error).message}\n`);
  process.exit(1);
});
