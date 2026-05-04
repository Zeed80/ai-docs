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
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const agentWsModeRef = useRef<AgentWsMode>("legacy");
  // Buffer accumulates text chunks from the current assistant response
  const streamingIdRef = useRef<string | null>(null);

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
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // ── Message handlers ───────────────────────────────────────────────────

  function handleServerMessage(data: Record<string, unknown>) {
    const type = data.type as string;

    if (type === "text") {
      const content = (data.content as string) ?? "";
      const sid = streamingIdRef.current;
      if (sid) {
        // Append to existing streaming message
        setMessages((prev) =>
          prev.map((m) =>
            m.id === sid ? { ...m, content: (m.content ?? "") + content } : m,
          ),
        );
      } else {
        // Start new assistant message
        const id = genId();
        streamingIdRef.current = id;
        setIsStreaming(true);
        setMessages((prev) => [...prev, { id, role: "assistant", content }]);
      }
    } else if (type === "done") {
      streamingIdRef.current = null;
      setIsStreaming(false);
    } else if (type === "tool_call") {
      setMessages((prev) => [
        ...prev,
        {
          id: genId(),
          role: "tool",
          tool: data.tool as string,
          args: data.args as Record<string, unknown>,
          status: "calling",
        },
      ]);
    } else if (type === "tool_result") {
      const toolName = data.tool as string;
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

  function handleApproval(msgId: string, approved: boolean) {
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
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200 bg-slate-50 rounded-t-xl">
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${isConnected ? "bg-green-500" : "bg-slate-300"}`}
          />
          <span className="font-semibold text-sm">{t("sveta")}</span>
          {isStreaming && (
            <span className="text-[10px] text-blue-500 animate-pulse">
              думает...
            </span>
          )}
        </div>
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
            return (
              <div key={msg.id} className="flex justify-start">
                <div className="max-w-[85%] px-3 py-2 rounded-lg text-sm bg-slate-100 text-slate-800 whitespace-pre-wrap">
                  {msg.content}
                </div>
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
            return (
              <div
                key={msg.id}
                className="border border-amber-200 rounded-lg p-3 bg-amber-50 text-sm"
              >
                <p className="font-medium text-amber-800 mb-1">
                  Требует подтверждения:{" "}
                  <code className="text-xs">{msg.tool}</code>
                </p>
                <pre className="text-[11px] text-slate-600 bg-white rounded p-2 overflow-x-auto mb-2 max-h-24">
                  {msg.preview}
                </pre>
                {isPending ? (
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleApproval(msg.id, true)}
                      className="px-3 py-1 bg-green-600 text-white rounded text-xs hover:bg-green-700"
                    >
                      Утвердить
                    </button>
                    <button
                      onClick={() => handleApproval(msg.id, false)}
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
                    {msg.status === "approved" ? "Утверждено" : "Отклонено"}
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
