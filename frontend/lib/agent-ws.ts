"use client";

import { getWsUrl } from "@/lib/ws-url";
import { getActiveWorkspaceContext } from "@/lib/workspace-context";

export interface AgentWsMessage extends Record<string, unknown> {
  type: string;
  content?: string;
  session_id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  preview?: string;
  approval_id?: string;
  db_id?: string;
}

export interface AgentWsResolvedConfig {
  endpoint: string;
  healthCheckEndpoints: string[];
}

function getBuiltinWsEndpoint(): string {
  return `${getWsUrl()}/ws/chat`;
}

export function getAgentWsEndpoint(): string {
  return getBuiltinWsEndpoint();
}

export function getAgentWsHealthCheckEndpoints(): string[] {
  return [getBuiltinWsEndpoint()];
}

export async function resolveAgentWsConfig(): Promise<AgentWsResolvedConfig> {
  const endpoint = getAgentWsEndpoint();
  return {
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
  reasoningMode?: "normal" | "strict",
): Record<string, unknown> {
  return {
    type: "message",
    content,
    session_id: sessionId ?? undefined,
    workspace_context: getActiveWorkspaceContext(),
    attachments:
      attachments && attachments.length > 0 ? attachments : undefined,
    reasoning_mode: reasoningMode ?? "normal",
  };
}

export function buildAgentApprovalMessage(
  approved: boolean,
  approvalId?: string,
  dbId?: string,
): Record<string, unknown> {
  return {
    type: approved ? "approve" : "reject",
    approval_id: approvalId,
    db_id: dbId,
  };
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
      "status",
      "orchestrator.status",
      "worker.assigned",
      "workspace.publish_started",
      "workspace.publish_verified",
      "audit.passed",
      "audit.failed",
      "capability_gap.detected",
      "session",
      "chat.session_updated",
      "workspace.updated",
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
        approval_id: String(payload.approval_id ?? data.approval_id ?? ""),
        db_id: String(payload.db_id ?? data.db_id ?? ""),
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
