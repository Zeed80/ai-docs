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
  attachments?: { name: string; docId: string }[];
  source?: "telegram";
}

type AttachedFileStatus = "uploading" | "uploaded" | "error";

interface AttachedFile {
  id: string;
  file: File;
  name: string;
  size: number;
  status: AttachedFileStatus;
  docId?: string;
  error?: string;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / 1048576).toFixed(1)} МБ`;
}

function FileChip({
  af,
  onRemove,
}: {
  af: AttachedFile;
  onRemove: () => void;
}) {
  return (
    <div
      className={`flex items-center gap-1.5 px-2 py-1 rounded text-[10px] max-w-[140px] shrink-0 ${
        af.status === "error"
          ? "bg-red-900/40 border border-red-700/50 text-red-300"
          : af.status === "uploaded"
            ? "bg-slate-600 border border-slate-500 text-slate-200"
            : "bg-slate-700 border border-slate-600 text-slate-400 animate-pulse"
      }`}
    >
      {af.status === "uploading" && (
        <svg
          className="w-3 h-3 shrink-0 animate-spin text-blue-400"
          fill="none"
          viewBox="0 0 24 24"
        >
          <circle
            className="opacity-25"
            cx="12"
            cy="12"
            r="10"
            stroke="currentColor"
            strokeWidth="4"
          />
          <path
            className="opacity-75"
            fill="currentColor"
            d="M4 12a8 8 0 018-8v8z"
          />
        </svg>
      )}
      {af.status === "uploaded" && (
        <svg
          className="w-3 h-3 shrink-0 text-green-400"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M5 13l4 4L19 7"
          />
        </svg>
      )}
      {af.status === "error" && (
        <svg
          className="w-3 h-3 shrink-0 text-red-400"
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
      )}
      <span className="truncate flex-1" title={af.name}>
        {af.name}
      </span>
      <span className="text-slate-500 shrink-0">{formatBytes(af.size)}</span>
      <button
        onClick={onRemove}
        className="shrink-0 text-slate-500 hover:text-slate-300 transition-colors"
        title="Убрать файл"
      >
        ×
      </button>
    </div>
  );
}

export function SvetaPanel() {
  const { isDegraded } = useDegradedMode();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [isConnected, setIsConnected] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const streamingIdRef = useRef<string | null>(null);
  const tgStreamingIdRef = useRef<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const agentWsModeRef = useRef<AgentWsMode>("legacy");
  const dragCounterRef = useRef(0);

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

  useEffect(() => {
    if (!isStreaming && !isDegraded) {
      inputRef.current?.focus();
    }
  }, [isStreaming, isDegraded]);

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
    const isTelegram = data.source === "telegram";

    // ── Telegram user message (mirror) ──────────────────────────────────────
    if (type === "tg_user") {
      tgStreamingIdRef.current = null;
      setMessages((prev) => [
        ...prev,
        {
          id: genId(),
          role: "user",
          content: data.content as string,
          source: "telegram",
        },
      ]);
      return;
    }

    // ── Text / streaming ─────────────────────────────────────────────────────
    if (type === "text") {
      const content = (data.content as string) ?? "";
      if (isTelegram) {
        const sid = tgStreamingIdRef.current;
        if (sid) {
          setMessages((prev) =>
            prev.map((m) =>
              m.id === sid ? { ...m, content: (m.content ?? "") + content } : m,
            ),
          );
        } else {
          const id = genId();
          tgStreamingIdRef.current = id;
          setMessages((prev) => [
            ...prev,
            { id, role: "assistant", content, source: "telegram" },
          ]);
        }
        return;
      }
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
      return;
    }

    // ── Done ─────────────────────────────────────────────────────────────────
    if (type === "done") {
      if (isTelegram) {
        tgStreamingIdRef.current = null;
        return;
      }
      streamingIdRef.current = null;
      setIsStreaming(false);
      return;
    }

    // ── Tool call / result (skip Telegram mirror — too noisy) ─────────────────
    if (type === "tool_call") {
      if (isTelegram) return;
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
      return;
    }
    if (type === "tool_result") {
      if (isTelegram) return;
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
      return;
    }

    // ── Approval (skip Telegram — handled by inline buttons) ─────────────────
    if (type === "approval_request") {
      if (isTelegram) return;
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
      return;
    }

    // ── Error ─────────────────────────────────────────────────────────────────
    if (type === "error") {
      if (isTelegram) {
        tgStreamingIdRef.current = null;
        return;
      }
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

  async function uploadFile(af: AttachedFile): Promise<string | null> {
    const form = new FormData();
    form.append("file", af.file);
    const res = await fetch("/api/documents/ingest?source_channel=chat", {
      method: "POST",
      body: form,
    });
    if (!res.ok) throw new Error(`${res.status}`);
    const body = (await res.json()) as Record<string, unknown>;
    return (body.document_id ?? body.id ?? null) as string | null;
  }

  function handleFiles(files: FileList | File[]) {
    const newFiles: AttachedFile[] = Array.from(files).map((f) => ({
      id: genId(),
      file: f,
      name: f.name,
      size: f.size,
      status: "uploading" as AttachedFileStatus,
    }));
    setAttachedFiles((prev) => [...prev, ...newFiles]);

    for (const af of newFiles) {
      uploadFile(af)
        .then((docId) => {
          setAttachedFiles((prev) =>
            prev.map((x) =>
              x.id === af.id
                ? { ...x, status: "uploaded", docId: docId ?? undefined }
                : x,
            ),
          );
        })
        .catch((err: Error) => {
          setAttachedFiles((prev) =>
            prev.map((x) =>
              x.id === af.id
                ? { ...x, status: "error", error: err.message }
                : x,
            ),
          );
        });
    }
  }

  function removeAttachment(id: string) {
    setAttachedFiles((prev) => prev.filter((x) => x.id !== id));
  }

  function sendMessage() {
    const hasText = input.trim().length > 0;
    const uploadedFiles = attachedFiles.filter((f) => f.status === "uploaded");
    const hasFiles = uploadedFiles.length > 0;

    if (
      (!hasText && !hasFiles) ||
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN
    )
      return;

    const isUploading = attachedFiles.some((f) => f.status === "uploading");
    if (isUploading) return;

    let content = input.trim();

    if (uploadedFiles.length > 0) {
      const refs = uploadedFiles
        .map((f) =>
          f.docId
            ? `- ${f.name} (document_id=${f.docId})`
            : `- ${f.name} (загрузка завершена, id недоступен)`,
        )
        .join("\n");
      const suffix = `\n\nПрикреплённые файлы:\n${refs}`;
      content = content ? content + suffix : suffix.trimStart();
    }

    const displayContent = input.trim() || undefined;
    const msgAttachments = uploadedFiles
      .filter((f) => f.docId)
      .map((f) => ({ name: f.name, docId: f.docId! }));

    setMessages((prev) => [
      ...prev,
      {
        id: genId(),
        role: "user",
        content: displayContent,
        attachments: msgAttachments.length > 0 ? msgAttachments : undefined,
      },
    ]);

    wsRef.current.send(
      JSON.stringify(buildAgentUserMessage(content, agentWsModeRef.current)),
    );
    setInput("");
    setAttachedFiles([]);
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

  const isUploading = attachedFiles.some((f) => f.status === "uploading");
  const canSend =
    !isDegraded &&
    !isStreaming &&
    !isUploading &&
    (input.trim().length > 0 ||
      attachedFiles.some((f) => f.status === "uploaded"));

  return (
    <aside
      className={`relative w-full h-full bg-slate-800 border-l flex flex-col overflow-hidden transition-colors ${isDragging ? "border-blue-500 bg-slate-700" : "border-slate-700"}`}
      onDragEnter={(e) => {
        e.preventDefault();
        dragCounterRef.current++;
        setIsDragging(true);
      }}
      onDragLeave={() => {
        dragCounterRef.current--;
        if (dragCounterRef.current === 0) setIsDragging(false);
      }}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        dragCounterRef.current = 0;
        setIsDragging(false);
        if (e.dataTransfer.files.length > 0) {
          handleFiles(e.dataTransfer.files);
        }
      }}
    >
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
        {isUploading && (
          <span className="text-[10px] text-amber-400 animate-pulse ml-1">
            загружаю...
          </span>
        )}
        {!isConnected && (
          <span className="ml-auto text-[10px] text-amber-400">офлайн</span>
        )}
      </div>

      {/* Drag overlay */}
      {isDragging && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-blue-900/30 border-2 border-dashed border-blue-500 rounded pointer-events-none">
          <div className="text-blue-300 text-sm font-medium">
            Отпустите файлы для прикрепления
          </div>
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-3 space-y-2">
        {messages.length === 0 && (
          <div className="text-center text-slate-500 text-xs mt-10 space-y-1">
            <p className="text-2xl">👋</p>
            <p className="font-medium text-slate-400">Привет! Я Света.</p>
            <p>Спросите меня о счётах, аномалиях или поручите задачу.</p>
            <p>Можно прикрепить файл — перетащите или нажмите скрепку.</p>
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
            const isTg = msg.source === "telegram";
            return (
              <div key={msg.id} className="flex justify-end">
                <div className="max-w-[85%] space-y-1">
                  {isTg && (
                    <div className="flex items-center justify-end gap-1 mb-0.5">
                      <svg
                        viewBox="0 0 24 24"
                        className="w-3 h-3 text-sky-400 fill-current shrink-0"
                      >
                        <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12L7.19 13.65l-2.96-.924c-.643-.204-.657-.643.136-.953l11.57-4.461c.537-.194 1.006.131.958.909z" />
                      </svg>
                      <span className="text-[9px] text-sky-500">Telegram</span>
                    </div>
                  )}
                  {msg.content && (
                    <div
                      className={`px-3 py-2 rounded-lg text-sm text-white ${isTg ? "bg-sky-700" : "bg-blue-600"}`}
                    >
                      {msg.content}
                    </div>
                  )}
                  {msg.attachments && msg.attachments.length > 0 && (
                    <div className="flex flex-wrap gap-1 justify-end">
                      {msg.attachments.map((a) => (
                        <div
                          key={a.docId}
                          className="flex items-center gap-1 px-2 py-1 rounded text-[10px] bg-blue-800/60 text-blue-200 border border-blue-700/50"
                          title={`document_id=${a.docId}`}
                        >
                          <svg
                            className="w-3 h-3 shrink-0"
                            fill="none"
                            stroke="currentColor"
                            viewBox="0 0 24 24"
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              strokeWidth={2}
                              d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"
                            />
                          </svg>
                          <span className="truncate max-w-[100px]">
                            {a.name}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          }
          if (msg.role === "assistant") {
            const isTg = msg.source === "telegram";
            return (
              <div key={msg.id} className="flex justify-start">
                <div
                  className={`max-w-[90%] rounded-lg text-sm text-slate-100 whitespace-pre-wrap overflow-hidden ${isTg ? "bg-slate-600" : "bg-slate-700"}`}
                >
                  {isTg && (
                    <div className="flex items-center gap-1 px-3 pt-2 pb-1 border-b border-slate-500/40">
                      <svg
                        viewBox="0 0 24 24"
                        className="w-3 h-3 text-sky-400 fill-current shrink-0"
                      >
                        <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.894 8.221-1.97 9.28c-.145.658-.537.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12L7.19 13.65l-2.96-.924c-.643-.204-.657-.643.136-.953l11.57-4.461c.537-.194 1.006.131.958.909z" />
                      </svg>
                      <span className="text-[9px] text-sky-400">
                        Ответ через Telegram
                      </span>
                    </div>
                  )}
                  <div className="px-3 py-2">{msg.content}</div>
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

      {/* Input area */}
      <div className="p-3 border-t border-slate-700 space-y-2">
        {/* File chips */}
        {attachedFiles.length > 0 && (
          <div className="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto">
            {attachedFiles.map((af) => (
              <FileChip
                key={af.id}
                af={af}
                onRemove={() => removeAttachment(af.id)}
              />
            ))}
          </div>
        )}

        <div className="flex gap-2">
          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => {
              if (e.target.files && e.target.files.length > 0) {
                handleFiles(e.target.files);
                e.target.value = "";
              }
            }}
          />

          {/* Paperclip button */}
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={isDegraded || isStreaming}
            title="Прикрепить файл"
            className="px-2 py-2 text-slate-400 hover:text-slate-200 hover:bg-slate-700 rounded-lg transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
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
                d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"
              />
            </svg>
          </button>

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
            placeholder={
              isDegraded
                ? "Света офлайн"
                : isUploading
                  ? "Загружаю файлы…"
                  : "Спросите Свету…"
            }
            disabled={isDegraded || isStreaming}
            className="flex-1 px-3 py-2 text-sm bg-slate-700 border border-slate-600 rounded-lg text-slate-100 placeholder-slate-500 focus:outline-none focus:border-blue-500 disabled:opacity-50"
          />
          <button
            onClick={sendMessage}
            disabled={!canSend}
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
