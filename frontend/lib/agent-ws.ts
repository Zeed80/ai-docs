"use client";

import { getWsUrl } from "@/lib/ws-url";

export type AgentWsMode = "legacy";

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

export function setLegacyAgentWsFallback(): void {
  // no-op: single-mode setup, kept for API compatibility
}

export function clearAgentWsFallback(): void {
  // no-op: single-mode setup, kept for API compatibility
}

export function getAgentWsEndpoint(): string {
  return getLegacyWsEndpoint();
}

export function getAgentWsHealthCheckEndpoints(): string[] {
  return [getLegacyWsEndpoint()];
}

export async function resolveAgentWsConfig(): Promise<AgentWsResolvedConfig> {
  const endpoint = getLegacyWsEndpoint();
  return {
    mode: "legacy",
    endpoint,
    healthCheckEndpoints: [endpoint],
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
  _mode?: AgentWsMode,
): Record<string, unknown> {
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
  _mode?: AgentWsMode,
): Record<string, unknown> {
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
