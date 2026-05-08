"use client";

import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  buildAgentApprovalMessage,
  buildAgentUserMessage,
  normalizeAgentMessages,
  resolveAgentWsConfig,
  type AgentWsMode,
} from "@/lib/agent-ws";
import { useDegradedMode } from "@/lib/degraded-mode";
import { genId } from "@/lib/ws-url";

// ── Types ─────────────────────────────────────────────────────────────────

type MessageRole = "user" | "assistant" | "tool" | "approval" | "error";

interface ChatMessage {
  id: string;
  role: MessageRole;
  content?: string;
  // tool activity
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  status?: "calling" | "done" | "pending" | "approved" | "rejected";
  // approval gate
  preview?: string;
  // tools used during the turn that produced this assistant message
  toolsUsedInTurn?: string[];
}

// ── Component ─────────────────────────────────────────────────────────────

export function ChatWidget() {
  const t = useTranslations("chat");
  const { isDegraded } = useDegradedMode();
  const [isOpen, setIsOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [currentStatus, setCurrentStatus] = useState<string | null>(null);
  const [activeToolCall, setActiveToolCall] = useState<string | null>(null);
  const [workerModel, setWorkerModel] = useState<string | null>(null);
  const [ratings, setRatings] = useState<Record<string, 1 | -1>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const agentWsModeRef = useRef<AgentWsMode>("legacy");
  const streamingIdRef = useRef<string | null>(null);
  // Tools used in the current streaming turn (reset on each user message)
  const currentTurnToolsRef = useRef<string[]>([]);
  // session_id for rating attribution (stable per widget mount)
  const sessionIdRef = useRef<string>(genId());

  // ── WebSocket ──────────────────────────────────────────────────────────

  const connect = useCallback(() => {
    if (isDegraded) return;
    void (async () => {
      try {
        const config = await resolveAgentWsConfig();
        agentWsModeRef.current = config.mode;
        const ws = new WebSocket(config.endpoint);

        ws.onopen = () => setIsConnected(true);

        ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data) as Record<string, unknown>;
            for (const message of normalizeAgentMessages(data)) {
              handleServerMessage(message);
            }
          } catch {
            // plain text fallback
            appendAssistant(String(event.data));
          }
        };

        ws.onclose = () => {
          setIsConnected(false);
          setTimeout(connect, 5000);
        };

        ws.onerror = () => setIsConnected(false);

        wsRef.current = ws;
      } catch {
        setIsConnected(false);
      }
    })();
  }, [isDegraded]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (isOpen && !isDegraded) connect();
    return () => wsRef.current?.close();
  }, [isOpen, isDegraded, connect]);

  useEffect(() => {
    if (!isOpen) return;
    fetch("/api/ai/agent-config")
      .then((r) => r.json())
      .then((cfg: Record<string, unknown>) => {
        const m = (cfg.worker_model ?? cfg.model ?? "") as string;
        if (m) setWorkerModel(m);
      })
      .catch(() => {});
  }, [isOpen]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // ── Message handlers ───────────────────────────────────────────────────

  function handleServerMessage(data: Record<string, unknown>) {
    const type = data.type as string;

    if (type === "text") {
      const content = (data.content as string) ?? "";
      const sid = streamingIdRef.current;
      if (sid) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === sid ? { ...m, content: (m.content ?? "") + content } : m,
          ),
        );
      } else {
        const id = genId();
        streamingIdRef.current = id;
        setIsStreaming(true);
        setMessages((prev) => [...prev, { id, role: "assistant", content }]);
      }
    } else if (type === "status") {
      setCurrentStatus((data.content as string) ?? null);
    } else if (type === "done") {
      const sid = streamingIdRef.current;
      const toolsSnapshot = [...currentTurnToolsRef.current];
      // Attach tool list to the assistant message so rating can reference it
      if (sid && toolsSnapshot.length > 0) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === sid ? { ...m, toolsUsedInTurn: toolsSnapshot } : m,
          ),
        );
      }
      streamingIdRef.current = null;
      currentTurnToolsRef.current = [];
      setIsStreaming(false);
      setCurrentStatus(null);
      setActiveToolCall(null);
    } else if (type === "tool_call") {
      const toolName = data.tool as string;
      setActiveToolCall(toolName);
      if (!currentTurnToolsRef.current.includes(toolName)) {
        currentTurnToolsRef.current = [
          ...currentTurnToolsRef.current,
          toolName,
        ];
      }
      setMessages((prev) => [
        ...prev,
        {
          id: genId(),
          role: "tool",
          tool: toolName,
          args: data.args as Record<string, unknown>,
          status: "calling",
        },
      ]);
    } else if (type === "tool_result") {
      const toolName = data.tool as string;
      setActiveToolCall(null);
      // Update the matching "calling" tool message
      setMessages((prev) => {
        const lastCallIdx = [...prev]
          .reverse()
          .findIndex(
            (m) =>
              m.role === "tool" &&
              m.tool === toolName &&
              m.status === "calling",
          );
        if (lastCallIdx === -1) return prev;
        const realIdx = prev.length - 1 - lastCallIdx;
        return prev.map((m, i) =>
          i === realIdx ? { ...m, status: "done", result: data.result } : m,
        );
      });
    } else if (type === "approval_request") {
      setMessages((prev) => [
        ...prev,
        {
          id: genId(),
          role: "approval",
          tool: data.tool as string,
          args: data.args as Record<string, unknown>,
          preview: data.preview as string,
          status: "pending",
        },
      ]);
    } else if (type === "error") {
      streamingIdRef.current = null;
      setIsStreaming(false);
      setMessages((prev) => [
        ...prev,
        {
          id: genId(),
          role: "error",
          content: data.content as string,
        },
      ]);
    }
  }

  function appendAssistant(text: string) {
    setMessages((prev) => [
      ...prev,
      { id: genId(), role: "assistant", content: text },
    ]);
  }

  // ── Actions ────────────────────────────────────────────────────────────

  function sendMessage() {
    if (
      !input.trim() ||
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN
    )
      return;
    const content = input.trim();
    currentTurnToolsRef.current = [];
    setMessages((prev) => [...prev, { id: genId(), role: "user", content }]);
    wsRef.current.send(
      JSON.stringify(
        buildAgentUserMessage(
          content,
          undefined,
          undefined,
          agentWsModeRef.current,
        ),
      ),
    );
    setInput("");
    setIsStreaming(true);
  }

  async function rateMessage(msg: ChatMessage, vote: 1 | -1) {
    if (ratings[msg.id]) return;
    setRatings((prev) => ({ ...prev, [msg.id]: vote }));
    try {
      await fetch("/api/memory/rate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionIdRef.current,
          message_id: msg.id,
          rating: vote,
          tools_used: msg.toolsUsedInTurn ?? [],
        }),
      });
    } catch {
      // non-critical — rating failed silently
    }
  }

  async function handleApproval(msgId: string, approved: boolean) {
    const msg = messages.find((m) => m.id === msgId);

    // Capability proposals are decided via REST, not agent WS approval_future
    if (msg?.tool === "capability.proposal") {
      const proposalId = (msg.args as Record<string, string> | undefined)
        ?.proposal_id;
      if (proposalId) {
        try {
          await fetch(`/api/agent/capabilities/${proposalId}/decide`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              approved,
              decided_by: "user_chat",
              comment: approved ? "Одобрено в чате" : "Отклонено в чате",
            }),
          });
        } catch {
          // non-critical
        }
      }
      setMessages((prev) =>
        prev.map((m) =>
          m.id === msgId
            ? { ...m, status: approved ? "approved" : "rejected" }
            : m,
        ),
      );
      return;
    }

    // Standard tool approval via WebSocket
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(
      JSON.stringify(
        buildAgentApprovalMessage(approved, agentWsModeRef.current),
      ),
    );
    setMessages((prev) =>
      prev.map((m) =>
        m.id === msgId
          ? { ...m, status: approved ? "approved" : "rejected" }
          : m,
      ),
    );
  }

  // ── Render ─────────────────────────────────────────────────────────────

  if (!isOpen) {
    return (
      <button
        onClick={() => setIsOpen(true)}
        className="fixed bottom-6 right-6 w-14 h-14 bg-blue-500 text-white rounded-full shadow-lg hover:bg-blue-600 transition-colors flex items-center justify-center z-40"
        title={t("placeholder")}
      >
        <svg
          className="w-6 h-6"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"
          />
        </svg>
        {isDegraded && (
          <span className="absolute -top-1 -right-1 w-3 h-3 bg-amber-400 rounded-full border-2 border-white" />
        )}
      </button>
    );
  }

  return (
    <div className="fixed bottom-6 right-6 w-96 h-[520px] bg-white rounded-xl shadow-2xl border border-slate-200 flex flex-col z-50">
      {/* Header */}
      <div className="border-b border-slate-200 bg-slate-50 rounded-t-xl">
        {/* Top row: name + status + model + close */}
        <div className="flex items-center justify-between px-4 py-3">
          <div className="flex items-center gap-2 min-w-0">
            <span
              className={`w-2 h-2 rounded-full flex-shrink-0 ${isConnected ? "bg-green-500" : "bg-slate-300"}`}
            />
            <span className="font-semibold text-sm">{t("sveta")}</span>
            {isStreaming ? (
              <span className="text-[10px] text-blue-500 animate-pulse flex-shrink-0">
                думает...
              </span>
            ) : (
              <span className="text-[10px] text-slate-400 flex-shrink-0">
                {isConnected ? "онлайн" : "офлайн"}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 flex-shrink-0">
            {workerModel && (
              <span className="text-[10px] text-slate-400 bg-slate-100 rounded px-1.5 py-0.5 font-mono">
                {workerModel}
              </span>
            )}
            <button
              onClick={() => setIsOpen(false)}
              className="text-slate-400 hover:text-slate-600"
            >
              <svg
                className="w-5 h-5"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>
        </div>

        {/* Activity bar — visible while agent is working */}
        {isStreaming && (activeToolCall || currentStatus) && (
          <div className="px-4 pb-2 flex items-center gap-2 flex-wrap border-t border-slate-100 pt-1.5">
            {activeToolCall && (
              <span className="flex items-center gap-1 text-[10px] bg-blue-50 text-blue-600 border border-blue-200 rounded px-1.5 py-0.5 font-mono max-w-[200px] truncate">
                <svg
                  className="w-2.5 h-2.5 flex-shrink-0 animate-spin"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
                  />
                </svg>
                {activeToolCall.replace(/__/g, ".")}
              </span>
            )}
            {currentStatus && (
              <span
                className="text-[10px] text-slate-500 truncate max-w-[220px]"
                title={currentStatus}
              >
                {currentStatus.replace(/^Оркестратор:\s*|^Инструмент:\s*/i, "")}
              </span>
            )}
          </div>
        )}
      </div>

      {/* Degraded banner */}
      {isDegraded && (
        <div className="px-3 py-2 bg-amber-50 border-b border-amber-200 text-xs text-amber-700">
          {t("unavailable")}
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && (
          <div className="text-center text-slate-400 text-sm mt-8">
            <p className="font-medium">{t("sveta")}</p>
            <p className="mt-1 text-xs">{t("placeholder")}</p>
          </div>
        )}

        {messages.map((msg) => {
          if (msg.role === "user") {
            return (
              <div key={msg.id} className="flex justify-end">
                <div className="max-w-[80%] px-3 py-2 rounded-lg text-sm bg-blue-500 text-white">
                  {msg.content}
                </div>
              </div>
            );
          }

          if (msg.role === "assistant") {
            const existingRating = ratings[msg.id];
            const isLastAssistant =
              !isStreaming &&
              [...messages].reverse().find((m) => m.role === "assistant")
                ?.id === msg.id;
            return (
              <div key={msg.id} className="flex flex-col items-start gap-0.5">
                <div className="max-w-[85%] px-3 py-2 rounded-lg text-sm bg-slate-100 text-slate-800 whitespace-pre-wrap">
                  {msg.content}
                </div>
                {isLastAssistant && (
                  <div className="flex items-center gap-1 pl-1">
                    <button
                      onClick={() => void rateMessage(msg, 1)}
                      disabled={!!existingRating}
                      title="Полезно"
                      className={`text-base leading-none transition-colors ${
                        existingRating === 1
                          ? "opacity-100"
                          : existingRating
                            ? "opacity-20 cursor-default"
                            : "opacity-40 hover:opacity-90"
                      }`}
                    >
                      👍
                    </button>
                    <button
                      onClick={() => void rateMessage(msg, -1)}
                      disabled={!!existingRating}
                      title="Не полезно"
                      className={`text-base leading-none transition-colors ${
                        existingRating === -1
                          ? "opacity-100"
                          : existingRating
                            ? "opacity-20 cursor-default"
                            : "opacity-40 hover:opacity-90"
                      }`}
                    >
                      👎
                    </button>
                  </div>
                )}
              </div>
            );
          }

          if (msg.role === "tool") {
            return (
              <div key={msg.id} className="flex items-center gap-2 px-2">
                <span
                  className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                    msg.status === "calling"
                      ? "bg-amber-400 animate-pulse"
                      : "bg-green-400"
                  }`}
                />
                <span className="text-[11px] text-slate-400 font-mono truncate">
                  {msg.tool?.replace("__", ".")}{" "}
                  {msg.status === "calling" ? "..." : "✓"}
                </span>
              </div>
            );
          }

          if (msg.role === "approval") {
            const isPending = msg.status === "pending";
            const isCapability = msg.tool === "capability.proposal";
            const args = (msg.args ?? {}) as Record<string, string>;
            const riskColor: Record<string, string> = {
              low: "text-green-600",
              medium: "text-amber-600",
              high: "text-orange-600",
              critical: "text-red-600",
            };
            return (
              <div
                key={msg.id}
                className={`border rounded-lg p-3 text-sm ${
                  isCapability
                    ? "border-violet-200 bg-violet-50"
                    : "border-amber-200 bg-amber-50"
                }`}
              >
                <p
                  className={`font-medium mb-1 ${isCapability ? "text-violet-800" : "text-amber-800"}`}
                >
                  {isCapability
                    ? "🧩 Новая capability"
                    : "⚡ Требует подтверждения"}
                  {!isCapability && (
                    <>
                      {": "}
                      <code className="text-xs">{msg.tool}</code>
                    </>
                  )}
                </p>
                {isCapability && args.title && (
                  <p className="font-medium text-slate-700 mb-0.5">
                    {args.title}
                  </p>
                )}
                {isCapability && args.risk_level && (
                  <p
                    className={`text-[11px] mb-1 ${riskColor[args.risk_level] ?? "text-slate-500"}`}
                  >
                    Риск:{" "}
                    <span className="font-semibold">{args.risk_level}</span>
                    {args.suggested_artifact && ` · ${args.suggested_artifact}`}
                  </p>
                )}
                {msg.preview && (
                  <pre className="text-[11px] text-slate-600 bg-white rounded p-2 overflow-x-auto mb-2 max-h-24 whitespace-pre-wrap">
                    {isCapability ? args.reason || msg.preview : msg.preview}
                  </pre>
                )}
                {isPending ? (
                  <div className="flex gap-2">
                    <button
                      onClick={() => void handleApproval(msg.id, true)}
                      className={`px-3 py-1 text-white rounded text-xs ${
                        isCapability
                          ? "bg-violet-600 hover:bg-violet-700"
                          : "bg-green-600 hover:bg-green-700"
                      }`}
                    >
                      {isCapability ? "Разрешить" : "Утвердить"}
                    </button>
                    <button
                      onClick={() => void handleApproval(msg.id, false)}
                      className="px-3 py-1 bg-slate-200 text-slate-700 rounded text-xs hover:bg-slate-300"
                    >
                      Отклонить
                    </button>
                  </div>
                ) : (
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full ${
                      msg.status === "approved"
                        ? "bg-green-100 text-green-700"
                        : "bg-red-100 text-red-700"
                    }`}
                  >
                    {msg.status === "approved" ? "Разрешено ✓" : "Отклонено"}
                  </span>
                )}
              </div>
            );
          }

          if (msg.role === "error") {
            return (
              <div key={msg.id} className="flex justify-start">
                <div className="max-w-[85%] px-3 py-2 rounded-lg text-sm bg-red-50 text-red-700 border border-red-200">
                  {msg.content}
                </div>
              </div>
            );
          }

          return null;
        })}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="p-3 border-t border-slate-200">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            placeholder={isDegraded ? t("unavailable") : t("placeholder")}
            disabled={isDegraded || isStreaming}
            className="flex-1 px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-slate-50 disabled:text-slate-400"
          />
          <button
            onClick={sendMessage}
            disabled={isDegraded || isStreaming || !input.trim()}
            className="px-3 py-2 bg-blue-500 text-white rounded-lg text-sm hover:bg-blue-600 disabled:bg-slate-300 disabled:cursor-not-allowed"
          >
            <svg
              className="w-4 h-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8"
              />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
