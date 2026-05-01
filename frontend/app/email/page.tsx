"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = getApiBaseUrl();

interface EmailThread {
  id: string;
  subject: string | null;
  mailbox: string;
  sender: string | null;
  status: string;
  message_count: number;
  has_attachment: boolean;
  last_message_at: string | null;
  created_at: string;
}

interface ThreadListResponse {
  items: EmailThread[];
  total: number;
}

interface EmailDraft {
  id: string;
  to_address: string;
  subject: string;
  body: string;
  status: string;
  risk_score: number | null;
  risk_flags: string[];
}

type PanelMode = "threads" | "draft";

export default function EmailPage() {
  const router = useRouter();
  const [threads, setThreads] = useState<EmailThread[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [mailboxFilter, setMailboxFilter] = useState("");
  const [panelMode, setPanelMode] = useState<PanelMode>("threads");
  const [drafts, setDrafts] = useState<EmailDraft[]>([]);
  const [composing, setComposing] = useState(false);
  const [draftForm, setDraftForm] = useState({
    to_address: "",
    subject: "",
    body: "",
  });
  const [savingDraft, setSavingDraft] = useState(false);
  const [riskCheck, setRiskCheck] = useState<{
    risk_score: number;
    risk_flags: string[];
    approved: boolean;
  } | null>(null);

  const fetchThreads = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: "50" });
      if (mailboxFilter) params.set("mailbox", mailboxFilter);
      const res = await fetch(`${API_BASE}/api/email/threads?${params}`);
      const data: EmailThread[] | ThreadListResponse = await res.json();
      const items = Array.isArray(data)
        ? data
        : ((data as ThreadListResponse).items ?? []);
      setThreads(items);
      setTotal(
        Array.isArray(data)
          ? data.length
          : ((data as ThreadListResponse).total ?? 0),
      );
    } catch {
      setThreads([]);
    } finally {
      setLoading(false);
    }
  }, [mailboxFilter]);

  const fetchDrafts = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/email/drafts`);
      const data = await res.json();
      setDrafts(Array.isArray(data) ? data : (data.items ?? []));
    } catch {
      setDrafts([]);
    }
  }, []);

  useEffect(() => {
    fetchThreads();
  }, [fetchThreads]);

  useEffect(() => {
    if (panelMode === "draft") fetchDrafts();
  }, [panelMode, fetchDrafts]);

  // Keyboard navigation
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;
      if (e.key === "j")
        setSelectedIndex((i) => Math.min(i + 1, threads.length - 1));
      if (e.key === "k") setSelectedIndex((i) => Math.max(i - 1, 0));
      if (e.key === "n" && !e.ctrlKey) setComposing(true);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [threads.length]);

  async function handleSaveDraft() {
    if (!draftForm.to_address || !draftForm.subject) return;
    setSavingDraft(true);
    try {
      const res = await fetch(`${API_BASE}/api/email/drafts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(draftForm),
      });
      if (res.ok) {
        await fetchDrafts();
        setComposing(false);
        setDraftForm({ to_address: "", subject: "", body: "" });
        setPanelMode("draft");
      }
    } finally {
      setSavingDraft(false);
    }
  }

  async function handleRiskCheck(draftId: string) {
    const res = await fetch(
      `${API_BASE}/api/email/drafts/${draftId}/risk-check`,
      {
        method: "POST",
      },
    );
    if (res.ok) {
      const data = await res.json();
      setRiskCheck(data);
    }
  }

  async function handleSend(draftId: string) {
    if (!window.confirm("Отправить письмо?")) return;
    await fetch(`${API_BASE}/api/email/drafts/${draftId}/send`, {
      method: "POST",
    });
    await fetchDrafts();
  }

  const mailboxes = ["", "procurement", "accounting", "general"];
  const mailboxLabels: Record<string, string> = {
    "": "Все",
    procurement: "Закупки",
    accounting: "Бухгалтерия",
    general: "Общий",
  };

  const statusColors: Record<string, string> = {
    new: "bg-blue-900/40 text-blue-400",
    processing: "bg-amber-900/40 text-amber-400",
    processed: "bg-green-900/40 text-green-400",
    archived: "bg-slate-700 text-slate-400",
  };

  return (
    <div className="flex h-full">
      {/* Left panel: thread list */}
      <div className="w-80 shrink-0 border-r border-slate-700 flex flex-col bg-slate-800">
        {/* Header */}
        <div className="px-4 py-3 border-b border-slate-700">
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-sm font-bold">Почта</h2>
            <span className="text-xs text-slate-400">{total} писем</span>
          </div>
          <div className="flex gap-1 flex-wrap">
            {mailboxes.map((mb) => (
              <button
                key={mb}
                onClick={() => setMailboxFilter(mb)}
                className={`px-2 py-0.5 text-xs rounded-full border ${
                  mailboxFilter === mb
                    ? "bg-slate-600 text-white border-slate-600"
                    : "bg-slate-700 text-slate-300 border-slate-600 hover:bg-slate-600"
                }`}
              >
                {mailboxLabels[mb]}
              </button>
            ))}
          </div>
        </div>

        {/* Mode tabs */}
        <div className="flex border-b border-slate-700">
          <button
            onClick={() => setPanelMode("threads")}
            className={`flex-1 py-2 text-xs font-medium ${panelMode === "threads" ? "border-b-2 border-blue-500 text-blue-400" : "text-slate-500"}`}
          >
            Входящие
          </button>
          <button
            onClick={() => {
              setPanelMode("draft");
            }}
            className={`flex-1 py-2 text-xs font-medium ${panelMode === "draft" ? "border-b-2 border-blue-500 text-blue-400" : "text-slate-500"}`}
          >
            Черновики {drafts.length > 0 && `(${drafts.length})`}
          </button>
        </div>

        {/* Thread list */}
        <div className="flex-1 overflow-auto">
          {panelMode === "threads" && (
            <>
              {loading ? (
                <div className="py-8 text-center text-sm text-slate-400">
                  Загрузка...
                </div>
              ) : threads.length === 0 ? (
                <div className="py-8 text-center text-sm text-slate-400">
                  Нет писем
                  <p className="text-xs mt-1">IMAP не настроен или ящик пуст</p>
                </div>
              ) : (
                <div className="divide-y divide-slate-700">
                  {threads.map((thread, i) => (
                    <div
                      key={thread.id}
                      className={`px-4 py-3 cursor-pointer transition-colors ${
                        i === selectedIndex
                          ? "bg-blue-900/30 border-l-2 border-l-blue-500"
                          : "hover:bg-slate-700/50"
                      }`}
                      onClick={() => {
                        setSelectedIndex(i);
                        router.push(`/email/${thread.id}`);
                      }}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <p className="text-sm font-medium truncate">
                          {thread.subject ?? "(без темы)"}
                        </p>
                        <span
                          className={`shrink-0 text-[10px] px-1.5 py-0.5 rounded-full ${statusColors[thread.status] ?? "bg-slate-100 text-slate-600"}`}
                        >
                          {thread.status}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 mt-1">
                        <span className="text-xs text-slate-400 truncate">
                          {thread.sender ?? "—"}
                        </span>
                        {thread.has_attachment && (
                          <span className="text-[10px] text-slate-400">📎</span>
                        )}
                      </div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-[10px] px-1 bg-slate-700 text-slate-400 rounded">
                          {mailboxLabels[thread.mailbox] ?? thread.mailbox}
                        </span>
                        {thread.last_message_at && (
                          <span className="text-[10px] text-slate-400">
                            {new Date(
                              thread.last_message_at,
                            ).toLocaleDateString("ru-RU")}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {panelMode === "draft" && (
            <div className="divide-y divide-slate-700">
              {drafts.length === 0 ? (
                <div className="py-8 text-center text-sm text-slate-400">
                  Нет черновиков
                </div>
              ) : (
                drafts.map((draft) => (
                  <div key={draft.id} className="px-4 py-3 space-y-2">
                    <div>
                      <p className="text-sm font-medium truncate">
                        {draft.subject}
                      </p>
                      <p className="text-xs text-slate-400">
                        {draft.to_address}
                      </p>
                    </div>
                    <div className="flex gap-1.5">
                      <button
                        onClick={() => handleRiskCheck(draft.id)}
                        className="px-2 py-1 text-xs border border-slate-600 text-slate-300 rounded hover:bg-slate-700"
                      >
                        Проверить риски
                      </button>
                      <button
                        onClick={() => handleSend(draft.id)}
                        className="px-2 py-1 text-xs bg-blue-500 text-white rounded hover:bg-blue-600"
                      >
                        Отправить
                      </button>
                    </div>
                    {draft.risk_flags && draft.risk_flags.length > 0 && (
                      <div className="text-xs text-red-600">
                        {draft.risk_flags.join(", ")}
                      </div>
                    )}
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        {/* Compose button */}
        <div className="p-3 border-t border-slate-700">
          <button
            onClick={() => setComposing(true)}
            className="w-full py-2 text-sm bg-blue-500 text-white rounded-md hover:bg-blue-600 font-medium"
          >
            + Написать <kbd className="ml-1 text-xs opacity-70">n</kbd>
          </button>
        </div>
      </div>

      {/* Right panel: compose or placeholder */}
      <div className="flex-1 bg-slate-900 flex items-center justify-center">
        {composing ? (
          <div className="bg-slate-800 border border-slate-700 rounded-xl shadow-lg w-full max-w-lg p-6">
            <h3 className="text-sm font-bold mb-4">Новое письмо</h3>
            <div className="space-y-3">
              <input
                type="email"
                placeholder="Кому..."
                value={draftForm.to_address}
                onChange={(e) =>
                  setDraftForm((f) => ({ ...f, to_address: e.target.value }))
                }
                className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
              <input
                type="text"
                placeholder="Тема..."
                value={draftForm.subject}
                onChange={(e) =>
                  setDraftForm((f) => ({ ...f, subject: e.target.value }))
                }
                className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded focus:outline-none focus:ring-1 focus:ring-blue-400"
              />
              <textarea
                placeholder="Текст письма..."
                rows={8}
                value={draftForm.body}
                onChange={(e) =>
                  setDraftForm((f) => ({ ...f, body: e.target.value }))
                }
                className="w-full px-3 py-2 text-sm bg-slate-700 border border-slate-600 text-slate-200 placeholder-slate-500 rounded focus:outline-none focus:ring-1 focus:ring-blue-400 resize-none"
              />
              <div className="flex gap-2 justify-end">
                <button
                  onClick={() => {
                    setComposing(false);
                    setDraftForm({ to_address: "", subject: "", body: "" });
                  }}
                  className="px-4 py-2 text-sm text-slate-500 hover:text-slate-700"
                >
                  Отмена
                </button>
                <button
                  onClick={handleSaveDraft}
                  disabled={
                    savingDraft || !draftForm.to_address || !draftForm.subject
                  }
                  className="px-4 py-2 text-sm bg-slate-600 text-white rounded hover:bg-slate-700 disabled:opacity-50"
                >
                  Сохранить черновик
                </button>
              </div>
            </div>
          </div>
        ) : riskCheck ? (
          <div className="bg-slate-800 border border-slate-700 rounded-xl p-6 max-w-sm text-slate-200">
            <h3 className="font-bold mb-3">Проверка рисков</h3>
            <div
              className={`text-2xl font-bold mb-2 ${riskCheck.risk_score > 0.5 ? "text-red-600" : "text-green-600"}`}
            >
              {riskCheck.approved ? "Одобрено" : "Риски обнаружены"}
            </div>
            {riskCheck.risk_flags.length > 0 && (
              <ul className="text-sm text-red-700 list-disc pl-4 space-y-1 mb-4">
                {riskCheck.risk_flags.map((f, i) => (
                  <li key={i}>{f}</li>
                ))}
              </ul>
            )}
            <button
              onClick={() => setRiskCheck(null)}
              className="text-sm text-slate-400 hover:text-slate-600"
            >
              Закрыть
            </button>
          </div>
        ) : (
          <div className="text-center text-slate-400">
            <div className="text-4xl mb-3">✉</div>
            <p className="text-sm">Выберите письмо или напишите новое</p>
            <p className="text-xs mt-1 text-slate-500">
              <kbd className="px-1 border border-slate-600 rounded">j</kbd>/
              <kbd className="px-1 border border-slate-600 rounded">k</kbd>{" "}
              навигация &nbsp;
              <kbd className="px-1 border border-slate-600 rounded">n</kbd>{" "}
              написать
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
