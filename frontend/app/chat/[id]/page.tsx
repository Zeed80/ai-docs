"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { getApiBaseUrl, getWebSocketBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { useCurrentUser } from "@/lib/auth-context";

const API = getApiBaseUrl();
const WS_BASE = getWebSocketBaseUrl();

interface Attachment {
  id: string;
  file_name: string;
  file_size: number;
  mime_type: string;
  storage_key: string;
  document_id: string | null;
}

interface Message {
  id: string;
  room_id: string;
  sender_sub: string;
  sender_name?: string;
  content: string;
  content_type: "text" | "file" | "action" | "system";
  reply_to_id: string | null;
  is_edited: boolean;
  created_at: string;
  attachments: Attachment[];
}

interface Room {
  id: string;
  name: string;
  type: "direct" | "group" | "system";
  description: string | null;
}

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} КБ`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} МБ`;
}

function AttachmentCard({
  attachment,
  messageId,
  roomId,
  onAction,
}: {
  attachment: Attachment;
  messageId: string;
  roomId: string;
  onAction: (label: string) => void;
}) {
  const isImage = attachment.mime_type.startsWith("image/");
  const [menuOpen, setMenuOpen] = useState(false);

  async function recognize() {
    setMenuOpen(false);
    onAction("Распознавание...");
    await fetch(`${API}/api/rooms/${roomId}/messages/${messageId}/recognize`, {
      method: "POST",
      credentials: "include",
      headers: csrfHeaders(),
    });
  }

  async function sendToApproval() {
    setMenuOpen(false);
    onAction("Отправлено на согласование");
    await fetch(`${API}/api/rooms/${roomId}/messages/${messageId}/approve`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json", ...csrfHeaders() },
      body: JSON.stringify({ comment: attachment.file_name }),
    });
  }

  return (
    <div className="relative mt-1 rounded border border-border bg-muted/40 p-2 flex items-center gap-2 max-w-xs">
      <div className="text-2xl shrink-0">{isImage ? "🖼" : "📄"}</div>
      <div className="flex-1 min-w-0">
        <p className="text-xs font-medium truncate">{attachment.file_name}</p>
        <p className="text-[10px] text-muted-foreground">
          {formatSize(attachment.file_size)}
        </p>
      </div>
      <div className="relative">
        <button
          onClick={() => setMenuOpen((v) => !v)}
          className="p-1 rounded hover:bg-muted text-muted-foreground text-xs"
          title="Действия"
        >
          ⋯
        </button>
        {menuOpen && (
          <div className="absolute right-0 bottom-full mb-1 w-44 bg-popover border border-border rounded shadow-lg z-10 text-xs">
            <button
              onClick={recognize}
              className="w-full text-left px-3 py-2 hover:bg-muted transition-colors"
            >
              Распознать (OCR)
            </button>
            <button
              onClick={sendToApproval}
              className="w-full text-left px-3 py-2 hover:bg-muted transition-colors"
            >
              На согласование
            </button>
            {attachment.document_id && (
              <Link
                href={`/documents/${attachment.document_id}`}
                className="block px-3 py-2 hover:bg-muted transition-colors"
              >
                Открыть документ
              </Link>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function MessageBubble({
  msg,
  isMine,
  roomId,
}: {
  msg: Message;
  isMine: boolean;
  roomId: string;
}) {
  const [actionFeedback, setActionFeedback] = useState<string | null>(null);

  function handleAction(label: string) {
    setActionFeedback(label);
    setTimeout(() => setActionFeedback(null), 3000);
  }

  return (
    <div
      className={`flex gap-2 mb-3 ${isMine ? "flex-row-reverse" : "flex-row"}`}
    >
      <div className="w-6 h-6 rounded-full bg-slate-500 flex items-center justify-center text-[10px] font-bold text-white shrink-0 mt-1">
        {(msg.sender_name?.[0] ?? msg.sender_sub[0] ?? "?").toUpperCase()}
      </div>
      <div
        className={`max-w-[70%] ${isMine ? "items-end" : "items-start"} flex flex-col`}
      >
        {!isMine && (
          <span className="text-[10px] text-muted-foreground mb-0.5">
            {msg.sender_name ?? msg.sender_sub}
          </span>
        )}
        {msg.content_type === "system" ? (
          <div className="text-xs text-muted-foreground italic px-3 py-1.5 bg-muted rounded-full">
            {msg.content}
          </div>
        ) : (
          <div
            className={`rounded-2xl px-3 py-2 text-sm break-words ${
              isMine
                ? "bg-primary text-primary-foreground rounded-tr-sm"
                : "bg-muted rounded-tl-sm"
            }`}
          >
            {msg.content}
            {msg.is_edited && (
              <span className="text-[10px] opacity-60 ml-1">(ред.)</span>
            )}
          </div>
        )}
        {msg.attachments.map((att) => (
          <AttachmentCard
            key={att.id}
            attachment={att}
            messageId={msg.id}
            roomId={roomId}
            onAction={handleAction}
          />
        ))}
        {actionFeedback && (
          <p className="text-[10px] text-green-600 mt-0.5">{actionFeedback}</p>
        )}
        <span className="text-[10px] text-muted-foreground mt-0.5">
          {new Date(msg.created_at).toLocaleTimeString("ru", {
            hour: "2-digit",
            minute: "2-digit",
          })}
        </span>
      </div>
    </div>
  );
}

export default function ChatRoomPage() {
  const { id: roomId } = useParams<{ id: string }>();
  const router = useRouter();
  const user = useCurrentUser();

  const [room, setRoom] = useState<Room | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  function scrollToBottom() {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  // Load room info
  useEffect(() => {
    fetch(`${API}/api/rooms/${roomId}`, { credentials: "include" })
      .then((r) => r.json())
      .then(setRoom)
      .catch(() => {});
  }, [roomId]);

  // Load message history
  useEffect(() => {
    fetch(`${API}/api/rooms/${roomId}/messages?limit=50`, {
      credentials: "include",
    })
      .then((r) => r.json())
      .then((d) => {
        const items: Message[] = (d.items ?? []).reverse();
        setMessages(items);
      })
      .catch(() => {})
      .finally(() => setLoadingHistory(false));
  }, [roomId]);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // WebSocket connection
  useEffect(() => {
    const ws = new WebSocket(`${WS_BASE}/api/rooms/ws/${roomId}`);
    wsRef.current = ws;

    ws.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data);
        if (event.type === "message") {
          setMessages((prev) => [...prev, event.data as Message]);
        }
      } catch {
        /* ignore */
      }
    };

    ws.onclose = () => {
      wsRef.current = null;
    };

    return () => {
      ws.close();
    };
  }, [roomId]);

  const sendMessage = useCallback(async () => {
    if (!text.trim() || sending) return;
    const content = text.trim();
    setText("");
    setSending(true);
    try {
      await fetch(`${API}/api/rooms/${roomId}/messages`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ content }),
      });
    } catch {
      setText(content);
    } finally {
      setSending(false);
    }
  }, [text, sending, roomId]);

  async function uploadFile(file: File) {
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(`${API}/api/rooms/${roomId}/upload`, {
        method: "POST",
        credentials: "include",
        headers: csrfHeaders(),
        body: form,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      /* ignore */
    } finally {
      setUploading(false);
    }
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) uploadFile(file);
    e.target.value = "";
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  return (
    <div
      className="flex flex-col h-screen max-h-screen"
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
    >
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-border bg-card shrink-0">
        <button
          onClick={() => router.push("/chat")}
          className="text-muted-foreground hover:text-foreground transition-colors"
          title="Назад"
        >
          ←
        </button>
        <div className="w-7 h-7 rounded-full bg-slate-600 flex items-center justify-center text-xs font-bold text-white">
          {room?.type === "direct"
            ? "DM"
            : (room?.name?.[0]?.toUpperCase() ?? "?")}
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-sm font-semibold truncate">
            {room?.name ?? "Загрузка..."}
          </p>
          {room?.description && (
            <p className="text-[10px] text-muted-foreground truncate">
              {room.description}
            </p>
          )}
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-3">
        {loadingHistory && (
          <p className="text-xs text-muted-foreground text-center py-4">
            Загрузка...
          </p>
        )}
        {messages.map((msg) => (
          <MessageBubble
            key={msg.id}
            msg={msg}
            isMine={msg.sender_sub === user?.sub}
            roomId={roomId}
          />
        ))}
        <div ref={messagesEndRef} />
      </div>

      {/* Drag overlay */}
      {dragOver && (
        <div className="absolute inset-0 bg-primary/10 border-2 border-dashed border-primary rounded-lg flex items-center justify-center z-20 pointer-events-none">
          <p className="text-primary font-medium">
            Отпустите файл для загрузки
          </p>
        </div>
      )}

      {/* Input */}
      <div className="shrink-0 border-t border-border bg-card px-4 py-3">
        {uploading && (
          <p className="text-xs text-muted-foreground mb-2">
            Загрузка файла...
          </p>
        )}
        <div className="flex items-end gap-2">
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            onChange={handleFileChange}
          />
          <button
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            title="Прикрепить файл"
            className="p-2 rounded hover:bg-muted text-muted-foreground transition-colors shrink-0 disabled:opacity-50"
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
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Сообщение... (Enter — отправить, Shift+Enter — перенос строки)"
            rows={1}
            className="flex-1 resize-none border border-border rounded-lg px-3 py-2 text-sm bg-background focus:outline-none focus:ring-1 focus:ring-primary max-h-32 overflow-y-auto"
            style={{ minHeight: "38px" }}
          />
          <button
            onClick={sendMessage}
            disabled={!text.trim() || sending}
            className="p-2 rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors shrink-0"
            title="Отправить"
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
