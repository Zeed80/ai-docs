#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const ts = require("../frontend/node_modules/typescript");

const root = path.resolve(__dirname, "..");
const sourcePath = path.join(root, "frontend/lib/agent-ws.ts");
const source = fs.readFileSync(sourcePath, "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  },
  fileName: sourcePath,
}).outputText;

function loadAdapter() {
  const module = { exports: {} };
  const sandbox = {
    module,
    exports: module.exports,
    process: {
      env: {
      },
    },
    window: undefined,
    require(request) {
      if (request === "@/lib/ws-url") {
        return {
          getWsUrl: () => "ws://api.local",
        };
      }
      return require(request);
    },
  };
  vm.runInNewContext(compiled, sandbox, { filename: sourcePath });
  return module.exports;
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

{
  const adapter = loadAdapter();
  assert.equal(adapter.getAgentWsEndpoint(), "ws://api.local/ws/chat");
  assert.deepEqual(plain(adapter.getAgentWsHealthCheckEndpoints()), [
    "ws://api.local/ws/chat",
  ]);
  assert.deepEqual(plain(adapter.buildAgentUserMessage("ping")), {
    type: "message",
    content: "ping",
    reasoning_mode: "normal",
  });
  assert.deepEqual(plain(adapter.buildAgentApprovalMessage(true)), { type: "approve" });
  assert.deepEqual(plain(adapter.buildAgentApprovalMessage(false)), { type: "reject" });
}

{
  const adapter = loadAdapter();
  assert.deepEqual(plain(adapter.normalizeAgentMessages({ type: "text", content: "a" })), [
    { type: "text", content: "a" },
  ]);
  assert.deepEqual(
    plain(adapter.normalizeAgentMessages({ type: "chat.delta", payload: { text: "b" } })),
    [{ type: "text", content: "b" }],
  );
  assert.deepEqual(
    plain(adapter.normalizeAgentMessages({
      type: "assistant_message",
      payload: { text: "done text" },
    })),
    [
      { type: "text", content: "done text" },
      { type: "done" },
    ],
  );
  assert.deepEqual(
    plain(adapter.normalizeAgentMessages({
      type: "tool.call",
      payload: { name: "memory.search", args: { query: "steel" } },
    })),
    [{ type: "tool_call", tool: "memory.search", args: { query: "steel" } }],
  );
  assert.deepEqual(
    plain(adapter.normalizeAgentMessages({
      type: "approval.request",
      payload: {
        tool: "tech.process_plan_approve",
        args: { process_plan_id: "p1" },
        preview: "approve plan",
      },
    })),
    [
      {
        type: "approval_request",
        tool: "tech.process_plan_approve",
        args: { process_plan_id: "p1" },
        preview: "approve plan",
      },
    ],
  );
  assert.deepEqual(plain(adapter.normalizeAgentMessages({ type: "chat.done" })), [
    { type: "done" },
  ]);
}

console.log("OK agent WebSocket adapter smoke");
