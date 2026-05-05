"use client";

import { getWsUrl } from "@/lib/ws-url";
import { getAiAgentWebSocketUrl } from "@/lib/api-base";

export type AgentWsMode = "legacy" | "aiagent";

export interface AgentWsMessage extends Record<string, unknown> {
  type: string;
  content?: string;
  session_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  preview?: string;
}

export interface AgentWsResolvedConfig {
  mode: AgentWsMode;
  endpoint: string;
  healthCheckEndpoints: string[];
}

function getLegacyWsEndpoint(): string {
  return `${getWsUrl()}/ws/chat`;
}

function getConfiguredAgentWsMode(): AgentWsMode {
  return process.env.NEXT_PUBLIC_AGENT_WS_MODE === "aiagent"
    ? "aiagent"
    : "legacy";
}

function getFallbackMode(): AgentWsMode | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage?.getItem("agent_ws_fallback_mode") === "legacy"
      ? "legacy"
      : null;
  } catch {
    return null;
  }
}

export function getAgentWsMode(): AgentWsMode {
  return getFallbackMode() ?? getConfiguredAgentWsMode();
}

export function setLegacyAgentWsFallback(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage?.setItem("agent_ws_fallback_mode", "legacy");
  } catch {
    // sessionStorage can be unavailable in privacy-restricted browsers.
  }
}

export function clearAgentWsFallback(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage?.removeItem("agent_ws_fallback_mode");
  } catch {
    // sessionStorage can be unavailable in privacy-restricted browsers.
  }
}

export function getAgentWsEndpoint(): string {
  return getAgentWsMode() === "aiagent"
    ? getAiAgentWebSocketUrl()
    : getLegacyWsEndpoint();
}

export function getAgentWsHealthCheckEndpoints(): string[] {
  if (getConfiguredAgentWsMode() !== "aiagent") {
    return [getLegacyWsEndpoint()];
  }
  return [getAiAgentWebSocketUrl(), getLegacyWsEndpoint()];
}

export async function resolveAgentWsConfig(): Promise<AgentWsResolvedConfig> {
  const mode = getAgentWsMode();
  const endpoint = getAgentWsEndpoint();
  return {
    mode,
    endpoint,
    healthCheckEndpoints: getAgentWsHealthCheckEndpoints(),
  };
}

export function buildAgentUserMessage(
  content: string,
  sessionId?: string | null,
  attachments?: Array<{
    document_id: string;
    file_name: string;
    mime_type?: string;
    size_bytes?: number;
  }>,
  mode: AgentWsMode = getAgentWsMode(),
): Record<string, unknown> {
  if (mode === "aiagent") {
    return {
      type: "chat",
      payload: {
        text: content,
        session_id: sessionId ?? undefined,
        attachments:
          attachments && attachments.length > 0 ? attachments : undefined,
      },
    };
  }
  return {
    type: "message",
    content,
    session_id: sessionId ?? undefined,
    attachments:
      attachments && attachments.length > 0 ? attachments : undefined,
  };
}

export function buildAgentApprovalMessage(
  approved: boolean,
  mode: AgentWsMode = getAgentWsMode(),
): Record<string, unknown> {
  if (mode === "aiagent") {
    return {
      type: "approval",
      payload: { approved },
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
      "session",
    ].includes(type)
  ) {
    return [data as unknown as AgentWsMessage];
  }

  if (type === "canvas.publish" || type === "canvas.block") {
    return [
      {
        type: "canvas",
        canvas_id: String(payload.canvas_id ?? data.canvas_id ?? ""),
        block: (payload.block ?? data.block) as Record<string, unknown>,
        append: payload.append ?? data.append ?? true,
      },
    ];
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
