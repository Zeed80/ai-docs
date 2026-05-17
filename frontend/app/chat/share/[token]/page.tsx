"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

interface SharedMessage {
  id: string;
  role: string;
  content: string | null;
  created_at: string;
}

interface SharedSession {
  session_id: string;
  title: string;
  messages: SharedMessage[];
}

export default function SharedChatPage() {
  const { token } = useParams<{ token: string }>();
  const [session, setSession] = useState<SharedSession | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!token) return;
    fetch(`/api/chat/share/${token}`)
      .then((r) => {
        if (!r.ok) throw new Error("not found");
        return r.json() as Promise<SharedSession>;
      })
      .then(setSession)
      .catch(() => setError(true));
  }, [token]);

  if (error) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <div className="text-slate-400 text-center">
          <div className="text-4xl mb-4">🔒</div>
          <div className="text-lg font-medium text-slate-300">
            Чат не найден или ссылка недействительна
          </div>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="min-h-screen bg-slate-900 flex items-center justify-center">
        <div className="text-slate-500 text-sm">Загрузка...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-900 flex flex-col">
      <header className="border-b border-slate-700 px-6 py-4 bg-slate-800">
        <div className="max-w-2xl mx-auto">
          <div className="flex items-center gap-2 text-xs text-slate-500 mb-1">
            <span>💬</span>
            <span>Поделились чатом</span>
          </div>
          <h1 className="text-slate-100 font-semibold">{session.title}</h1>
        </div>
      </header>

      <main className="flex-1 overflow-y-auto">
        <div className="max-w-2xl mx-auto px-6 py-6 space-y-4">
          {session.messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[85%] rounded-lg px-4 py-3 text-sm whitespace-pre-wrap ${
                  msg.role === "user"
                    ? "bg-blue-600 text-white"
                    : "bg-slate-700 text-slate-100"
                }`}
              >
                {msg.content}
              </div>
            </div>
          ))}
        </div>
      </main>

      <footer className="border-t border-slate-700 px-6 py-3 bg-slate-800">
        <div className="max-w-2xl mx-auto text-center text-xs text-slate-500">
          Просмотр общего чата · AI Workspace
        </div>
      </footer>
    </div>
  );
}
