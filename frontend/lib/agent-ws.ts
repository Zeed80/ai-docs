"use client";

import { getWsUrl } from "@/lib/ws-url";
import { getApiBaseUrl, getOpenClawWebSocketUrl } from "@/lib/api-base";

export type AgentWsMode = "legacy" | "openclaw";

export interface AgentWsSettings {
  agent_ws_mode: AgentWsMode;
  openclaw_ws_url: string;
  legacy_ws_url: string;
  fallback_to_legacy: boolean;
}

export interface AgentWsResolvedConfig {
  mode: AgentWsMode;
  endpoint: string;
  healthCheckEndpoints: string[];
}

export interface AgentWsMessage extends Record<string, unknown> {
  type: string;
  content?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  preview?: string;
}

const FALLBACK_STORAGE_KEY = "agent_ws_fallback_mode";
let settingsCache: AgentWsSettings | null = null;

function getLegacyWsEndpoint(): string {
  return `${getWsUrl()}/ws/chat`;
}

function getOpenClawWsUrl(): string {
  return getOpenClawWebSocketUrl();
}

function getStoredFallbackMode(): AgentWsMode | null {
  if (typeof window === "undefined" || !window.sessionStorage) return null;
  return window.sessionStorage.getItem(FALLBACK_STORAGE_KEY) === "legacy"
    ? "legacy"
    : null;
}

export function setLegacyAgentWsFallback(): void {
  if (typeof window !== "undefined" && window.sessionStorage) {
    window.sessionStorage.setItem(FALLBACK_STORAGE_KEY, "legacy");
  }
}

export function clearAgentWsFallback(): void {
  if (typeof window !== "undefined" && window.sessionStorage) {
    window.sessionStorage.removeItem(FALLBACK_STORAGE_KEY);
  }
}

export function getAgentWsMode(): AgentWsMode {
  if (settingsCache?.agent_ws_mode) return settingsCache.agent_ws_mode;
  return process.env.NEXT_PUBLIC_AGENT_WS_MODE === "openclaw"
    ? "openclaw"
    : "legacy";
}

export function getAgentWsEndpoint(): string {
  const legacy = settingsCache?.legacy_ws_url || getLegacyWsEndpoint();
  const openclaw = settingsCache?.openclaw_ws_url || getOpenClawWsUrl();
  return getAgentWsMode() === "openclaw" && getStoredFallbackMode() !== "legacy"
    ? openclaw
    : legacy;
}

export function getAgentWsHealthCheckEndpoints(): string[] {
  const legacy = settingsCache?.legacy_ws_url || getLegacyWsEndpoint();
  const openclaw = settingsCache?.openclaw_ws_url || getOpenClawWsUrl();
  if (getAgentWsMode() !== "openclaw") return [legacy];
  if (settingsCache?.fallback_to_legacy === false) return [openclaw];
  return [openclaw, legacy];
}

export async function loadAgentWsSettings(): Promise<AgentWsSettings> {
  const response = await fetch(`${getApiBaseUrl()}/api/openclaw/settings`);
  if (!response.ok) throw new Error("OpenClaw settings unavailable");
  const data = (await response.json()) as AgentWsSettings;
  settingsCache = data;
  return data;
}

export async function resolveAgentWsConfig(): Promise<AgentWsResolvedConfig> {
  try {
    await loadAgentWsSettings();
  } catch {
    settingsCache = null;
  }
  return {
    mode: getAgentWsMode(),
    endpoint: getAgentWsEndpoint(),
    healthCheckEndpoints: getAgentWsHealthCheckEndpoints(),
  };
}

export function buildAgentUserMessage(
  content: string,
  mode: AgentWsMode = getAgentWsMode(),
): Record<string, unknown> {
  if (mode === "openclaw") {
    return {
      type: "chat",
      payload: {
        text: content,
      },
    };
  }

  return { type: "message", content };
}

export function buildAgentApprovalMessage(
  approved: boolean,
  mode: AgentWsMode = getAgentWsMode(),
): Record<string, unknown> {
  if (mode === "openclaw") {
    return {
      type: "approval",
      payload: {
        approved,
      },
    };
  }

  return { type: approved ? "approve" : "reject" };
}

export function normalizeAgentMessages(raw: unknown): AgentWsMessage[] {
  if (!raw || typeof raw !== "object") return [];
  const data = raw as Record<string, unknown>;
  const payload =
    data.payload && typeof data.payload === "object"
      ? (data.payload as Record<string, unknown>)
      : {};
  const type = String(data.type ?? "");

  if (
    [
      "text",
      "done",
      "tool_call",
      "tool_result",
      "approval_request",
      "error",
      "tg_user",
      "canvas",
    ].includes(type)
  ) {
    return [data as unknown as AgentWsMessage];
  }

  if (type === "chat.delta" || type === "delta" || type === "assistant.delta") {
    return [
      { type: "text", content: String(payload.text ?? data.content ?? "") },
    ];
  }

  if (type === "chat.done" || type === "assistant.done") {
    return [{ type: "done" }];
  }

  if (type === "assistant_message" || type === "assistant.message") {
    return [
      {
        type: "text",
        content: String(payload.text ?? data.content ?? ""),
      },
      { type: "done" },
    ];
  }

  if (
    type === "message" &&
    (payload.role === "assistant" || data.role === "assistant")
  ) {
    return [
      {
        type: "text",
        content: String(payload.text ?? data.content ?? ""),
      },
      { type: "done" },
    ];
  }

  if (type === "tool.call" || type === "tool_call_request") {
    return [
      {
        type: "tool_call",
        tool: String(
          payload.tool ?? data.tool ?? payload.name ?? data.name ?? "",
        ),
        args: (payload.args ?? data.args ?? {}) as Record<string, unknown>,
      },
    ];
  }

  if (type === "tool.result" || type === "tool_call_result") {
    return [
      {
        type: "tool_result",
        tool: String(
          payload.tool ?? data.tool ?? payload.name ?? data.name ?? "",
        ),
        result: payload.result ?? data.result,
      },
    ];
  }

  if (type === "approval.request" || type === "approval_request") {
    return [
      {
        type: "approval_request",
        tool: String(
          payload.tool ?? data.tool ?? payload.name ?? data.name ?? "",
        ),
        args: (payload.args ?? data.args ?? {}) as Record<string, unknown>,
        preview: String(payload.preview ?? data.preview ?? ""),
      },
    ];
  }

  if (type === "error" || type === "chat.error") {
    return [
      {
        type: "error",
        content: String(payload.message ?? data.message ?? data.content ?? ""),
      },
    ];
  }

  return [];
}
