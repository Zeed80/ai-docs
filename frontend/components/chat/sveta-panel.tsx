"use client";

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

type MessageRole = "user" | "assistant" | "tool" | "approval" | "error";

interface ChatMessage {
  id: string;
  role: MessageRole;
  content?: string;
  tool?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  status?: "calling" | "done" | "pending" | "approved" | "rejected";
  preview?: string;
}

export function SvetaPanel() {
  const { isDegraded } = useDegradedMode();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const streamingIdRef = useRef<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const agentWsModeRef = useRef<AgentWsMode>("legacy");

  const connect = useCallback(() => {
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
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    connect();
    return () => wsRef.current?.close();
  }, [connect]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Restore focus to input after streaming ends
  useEffect(() => {
    if (!isStreaming && !isDegraded) {
      inputRef.current?.focus();
    }
  }, [isStreaming, isDegraded]);

  // Focus input on Ctrl+K
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

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
      setMessages((prev) => {
        const idx = [...prev]
          .reverse()
          .findIndex(
            (m) =>
              m.role === "tool" &&
              m.tool === toolName &&
              m.status === "calling",
          );
        if (idx === -1) return prev;
        const realIdx = prev.length - 1 - idx;
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
      JSON.stringify(buildAgentUserMessage(content, agentWsModeRef.current)),
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

  return (
    <aside className="w-full h-full bg-slate-800 border-l border-slate-700 flex flex-col overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-700 flex items-center gap-2">
        <span
          className={`w-2 h-2 rounded-full shrink-0 ${isConnected ? "bg-green-400" : "bg-slate-500"}`}
        />
        <span className="font-semibold text-sm text-slate-100">Света</span>
        {isStreaming && (
          <span className="text-[10px] text-blue-400 animate-pulse ml-1">
            думает...
          </span>
        )}
        {!isConnected && (
          <span className="ml-auto text-[10px] text-amber-400">офлайн</span>
        )}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && (
          <div className="text-center text-slate-500 text-xs mt-10 space-y-1">
            <p className="text-2xl">👋</p>
            <p className="font-medium text-slate-400">Привет! Я Света.</p>
            <p>Спросите меня о счётах, аномалиях или поручите задачу.</p>
            <p className="mt-3 text-slate-600">
              <kbd className="px-1 py-0.5 bg-slate-700 rounded text-slate-500">
                Ctrl+K
              </kbd>{" "}
              — фокус
            </p>
          </div>
        )}

        {messages.map((msg) => {
          if (msg.role === "user") {
            return (
              <div key={msg.id} className="flex justify-end">
                <div className="max-w-[85%] px-3 py-2 rounded-lg text-sm bg-blue-600 text-white">
                  {msg.content}
                </div>
              </div>
            );
          }
          if (msg.role === "assistant") {
            return (
              <div key={msg.id} className="flex justify-start">
                <div className="max-w-[90%] px-3 py-2 rounded-lg text-sm bg-slate-700 text-slate-100 whitespace-pre-wrap">
                  {msg.content}
                </div>
              </div>
            );
          }
          if (msg.role === "tool") {
            return (
              <div key={msg.id} className="flex items-center gap-2 px-1">
                <span
                  className={`w-1.5 h-1.5 rounded-full shrink-0 ${msg.status === "calling" ? "bg-amber-400 animate-pulse" : "bg-green-400"}`}
                />
                <span className="text-[10px] text-slate-500 font-mono truncate">
                  {msg.tool?.replace("__", ".")}{" "}
                  {msg.status === "calling" ? "…" : "✓"}
                </span>
              </div>
            );
          }
          if (msg.role === "approval") {
            const isPending = msg.status === "pending";
            return (
              <div
                key={msg.id}
                className="border border-amber-600/50 rounded-lg p-3 bg-amber-950/40 text-sm"
              >
                <p className="font-medium text-amber-300 mb-1 text-xs">
                  Нужно разрешение:{" "}
                  <code className="font-mono">{msg.tool}</code>
                </p>
                <pre className="text-[10px] text-slate-400 bg-slate-900/50 rounded p-2 overflow-x-auto mb-2 max-h-20">
                  {msg.preview}
                </pre>
                {isPending ? (
                  <div className="flex gap-2">
                    <button
                      onClick={() => handleApproval(msg.id, true)}
                      className="flex-1 py-1.5 bg-green-700 hover:bg-green-600 text-white rounded text-xs font-medium transition-colors"
                    >
                      Утвердить
                    </button>
                    <button
                      onClick={() => handleApproval(msg.id, false)}
                      className="flex-1 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-300 rounded text-xs font-medium transition-colors"
                    >
                      Отклонить
                    </button>
                  </div>
                ) : (
                  <span
                    className={`text-xs px-2 py-0.5 rounded-full ${msg.status === "approved" ? "bg-green-900/50 text-green-400" : "bg-red-900/50 text-red-400"}`}
                  >
                    {msg.status === "approved" ? "Утверждено" : "Отклонено"}
                  </span>
                )}
              </div>
            );
          }
          if (msg.role === "error") {
            return (
              <div
                key={msg.id}
                className="px-3 py-2 rounded-lg text-xs bg-red-950/40 text-red-400 border border-red-800/50"
              >
                {msg.content}
              </div>
            );
          }
          return null;
        })}
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <div className="p-3 border-t border-slate-700">
        <div className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            placeholder={isDegraded ? "Света офлайн" : "Спросите Свету…"}
            disabled={isDegraded || isStreaming}
            className="flex-1 px-3 py-2 text-sm bg-slate-700 border border-slate-600 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 disabled:opacity-50"
          />
          <button
            onClick={sendMessage}
            disabled={isDegraded || isStreaming || !input.trim()}
            className="px-3 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-500 disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
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
    </aside>
  );
}
