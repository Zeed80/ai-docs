"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import { useEmailStream } from "./useEmailStream";
import { MailSidebar } from "./MailSidebar";
import { ThreadList } from "./ThreadList";
import { ThreadView } from "./ThreadView";
import { Composer } from "./Composer";
import type { ComposeMode, EmailLabel, EmailThread, MailboxChip } from "./types";

export function EmailClient({ initialThreadId }: { initialThreadId?: string }) {
  const t = useTranslations("email");

  const [mailboxes, setMailboxes] = useState<MailboxChip[]>([]);
  const [labels, setLabels] = useState<EmailLabel[]>([]);
  const [threads, setThreads] = useState<EmailThread[]>([]);
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);

  const [activeMailbox, setActiveMailbox] = useState("");
  const [activeFolder, setActiveFolder] = useState("inbox");
  const [activeLabel, setActiveLabel] = useState<string | null>(null);
  const [starredOnly, setStarredOnly] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");

  const [openThreadId, setOpenThreadId] = useState<string | null>(initialThreadId ?? null);
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [compose, setCompose] = useState<ComposeMode | null>(null);
  const [showKeys, setShowKeys] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const searchRef = useRef<HTMLInputElement>(null);

  const loadMeta = useCallback(() => {
    emailApi.mailboxes().then(setMailboxes).catch(() => {});
    emailApi.labels().then(setLabels).catch(() => {});
  }, []);

  const loadThreads = useCallback(async () => {
    setLoading(true);
    try {
      if (searchQuery.trim()) {
        const res = await emailApi.search({
          query: searchQuery,
          mailbox: activeMailbox || undefined,
          folder: activeFolder !== "inbox" ? activeFolder : undefined,
          label_ids: activeLabel ? [activeLabel] : [],
        });
        // Group search hits into pseudo-threads by thread_id for the list.
        const byThread = new Map<string, EmailThread>();
        for (const m of res.results) {
          const id = m.thread_id ?? m.id;
          if (!byThread.has(id))
            byThread.set(id, {
              id,
              subject: m.subject ?? "",
              mailbox: m.mailbox,
              message_count: 1,
              last_message_at: m.received_at,
              created_at: m.received_at ?? "",
              is_read: m.is_read,
              is_starred: m.is_starred,
              has_attachments: m.has_attachments,
              folder: m.folder,
              last_snippet: m.snippet,
              unread_count: m.is_read ? 0 : 1,
              labels: [],
              sender: m.from_address,
              messages: [],
            });
        }
        setThreads([...byThread.values()]);
      } else {
        const list = await emailApi.threads({
          mailbox: activeMailbox || undefined,
          folder: starredOnly ? undefined : activeFolder,
          label_id: activeLabel || undefined,
          is_starred: starredOnly || undefined,
          is_unread: unreadOnly || undefined,
        });
        setThreads(list);
      }
    } finally {
      setLoading(false);
    }
  }, [activeMailbox, activeFolder, activeLabel, starredOnly, unreadOnly, searchQuery]);

  useEffect(loadMeta, [loadMeta]);
  useEffect(() => {
    loadThreads();
  }, [loadThreads]);

  useEmailStream((e) => {
    if (e.type === "email.new" || e.type === "email.sent" || e.type === "email.thread_updated") {
      loadThreads();
      loadMeta();
    }
  });

  const openThread = useCallback(
    (th: EmailThread) => {
      setOpenThreadId(th.id);
      setCompose(null);
      window.history.replaceState(null, "", `/email/${th.id}`);
    },
    [],
  );

  const closeThread = useCallback(() => {
    setOpenThreadId(null);
    window.history.replaceState(null, "", "/email");
  }, []);

  const bulk = useCallback(
    async (action: string, ids?: string[], extra?: { folder?: string; label_id?: string }) => {
      const target = ids ?? [...selected];
      if (!target.length) return;
      await emailApi.bulkAction(target, action, extra ?? {});
      setSelected(new Set());
      loadThreads();
      loadMeta();
    },
    [selected, loadThreads, loadMeta],
  );

  async function handleSync() {
    setSyncing(true);
    try {
      await emailApi.syncMailbox(activeMailbox || null);
      setTimeout(() => {
        loadThreads();
        loadMeta();
        setSyncing(false);
      }, 2500);
    } catch {
      setSyncing(false);
    }
  }

  // Keyboard shortcuts (Gmail-style).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || (e.target as HTMLElement)?.isContentEditable) {
        if (e.key === "Escape") (e.target as HTMLElement).blur();
        return;
      }
      if (compose) {
        if (e.key === "Escape") setCompose(null);
        return;
      }
      const cur = threads[cursor];
      switch (e.key) {
        case "j":
          setCursor((c) => Math.min(c + 1, threads.length - 1));
          break;
        case "k":
          setCursor((c) => Math.max(c - 1, 0));
          break;
        case "Enter":
          if (cur) openThread(cur);
          break;
        case "c":
          setCompose({ kind: "new" });
          break;
        case "e":
          if (openThreadId) bulk("archive", [openThreadId]);
          else if (cur) bulk("archive", [cur.id]);
          break;
        case "#":
          if (openThreadId) bulk("trash", [openThreadId]);
          else if (cur) bulk("trash", [cur.id]);
          break;
        case "s":
          if (cur) bulk(cur.is_starred ? "unstar" : "star", [cur.id]);
          break;
        case "x":
          if (cur)
            setSelected((prev) => {
              const n = new Set(prev);
              if (n.has(cur.id)) n.delete(cur.id);
              else n.add(cur.id);
              return n;
            });
          break;
        case "u":
          closeThread();
          break;
        case "/":
          e.preventDefault();
          searchRef.current?.focus();
          break;
        case "?":
          setShowKeys((s) => !s);
          break;
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [threads, cursor, compose, openThreadId, openThread, closeThread, bulk]);

  const emptyState =
    mailboxes.length === 0 ? (
      <>
        {t("noMailboxes")}
        <br />
        <a href="/settings?tab=email" className="text-blue-400 underline">
          {t("configure")}
        </a>
      </>
    ) : mailboxes.find((m) => m.name === activeMailbox)?.sync_error ? (
      <>
        <span className="text-red-400">
          {t("syncError", {
            error: mailboxes.find((m) => m.name === activeMailbox)!.sync_error!,
          })}
        </span>
        <br />
        <button onClick={handleSync} className="mt-2 text-blue-400 underline">
          {t("retry")}
        </button>
      </>
    ) : (
      <>
        {t("emptyInbox")}
        <br />
        <span className="text-xs">{t("emptyAfterSync")}</span>
      </>
    );

  return (
    <div className="flex h-full">
      {sidebarOpen && (
      <MailSidebar
        onCollapse={() => setSidebarOpen(false)}
        mailboxes={mailboxes}
        labels={labels}
        activeMailbox={activeMailbox}
        activeFolder={activeFolder}
        activeLabel={activeLabel}
        starredOnly={starredOnly}
        onSelectMailbox={(n) => {
          setActiveMailbox(n);
          setActiveFolder("inbox");
          setActiveLabel(null);
          setStarredOnly(false);
        }}
        onSelectFolder={(f) => {
          setActiveFolder(f);
          setActiveLabel(null);
          setStarredOnly(false);
        }}
        onSelectLabel={(id) => {
          setActiveLabel(id);
          setStarredOnly(false);
        }}
        onToggleStarred={() => {
          setStarredOnly((s) => !s);
          setActiveLabel(null);
        }}
        onLabelsChanged={loadMeta}
        syncing={syncing}
        onSync={handleSync}
      />
      )}

      <div className="flex w-72 shrink-0 flex-col border-r border-slate-700 bg-slate-800/40">
        <div className="border-b border-slate-700 p-2">
          <div className="mb-1.5 flex items-center gap-1.5">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                className="rounded p-1 text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                title="Папки"
              >
                ☰
              </button>
            )}
            <input
            ref={searchRef}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder={t("search")}
            className="flex-1 rounded bg-slate-700 px-3 py-1.5 text-sm text-slate-100 placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          </div>
          <div className="mt-1.5 flex items-center gap-2 text-xs">
            <button
              onClick={() => setUnreadOnly((u) => !u)}
              className={`rounded-full px-2 py-0.5 ${unreadOnly ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-700"}`}
            >
              {t("unread", { n: "" }).trim() || "new"}
            </button>
            <button
              onClick={() => setCompose({ kind: "new" })}
              className="ml-auto rounded bg-blue-600 px-2.5 py-0.5 text-white hover:bg-blue-500"
            >
              + {t("compose")}
            </button>
          </div>
        </div>

        {selected.size > 0 && (
          <div className="flex items-center gap-2 border-b border-slate-700 bg-slate-800 px-2 py-1.5 text-xs">
            <span className="text-slate-300">{t("bulk.selected", { n: selected.size })}</span>
            <button onClick={() => bulk("read")} className="text-slate-400 hover:text-slate-200">
              {t("actions.markRead")}
            </button>
            <button onClick={() => bulk("archive")} className="text-slate-400 hover:text-slate-200">
              {t("actions.archive")}
            </button>
            <button
              onClick={() => bulk("trash")}
              className="text-slate-400 hover:text-red-300"
            >
              {t("actions.trash")}
            </button>
            <button
              onClick={() => setSelected(new Set())}
              className="ml-auto text-slate-500 hover:text-slate-300"
            >
              {t("bulk.clear")}
            </button>
          </div>
        )}

        <div className="min-h-0 flex-1">
          <ThreadList
            threads={threads}
            loading={loading}
            selectedId={openThreadId}
            selected={selected}
            onOpen={openThread}
            onToggleSelect={(id) =>
              setSelected((prev) => {
                const n = new Set(prev);
                if (n.has(id)) n.delete(id);
                else n.add(id);
                return n;
              })
            }
            onStar={(th) => bulk(th.is_starred ? "unstar" : "star", [th.id])}
            emptyState={emptyState}
          />
        </div>
        <p className="border-t border-slate-800 px-2 py-1 text-[10px] text-slate-600">
          {t("keysHint")}
        </p>
      </div>

      <div className="flex-1 bg-slate-900">
        {compose ? (
          <Composer
            mode={compose}
            mailboxes={mailboxes}
            defaultMailbox={activeMailbox || mailboxes[0]?.name || ""}
            onClose={() => setCompose(null)}
            onSent={() => {
              loadThreads();
              loadMeta();
            }}
          />
        ) : openThreadId ? (
          <ThreadView
            key={openThreadId}
            threadId={openThreadId}
            onReply={(m, all) => setCompose({ kind: "reply", message: m, all })}
            onForward={(m) => setCompose({ kind: "forward", message: m })}
            onArchive={() => {
              bulk("archive", [openThreadId]);
              closeThread();
            }}
            onTrash={() => {
              bulk("trash", [openThreadId]);
              closeThread();
            }}
            onClose={closeThread}
          />
        ) : (
          <div className="flex h-full items-center justify-center text-center text-slate-500">
            <div>
              <div className="mb-2 text-4xl">✉</div>
              <p className="text-sm">{t("noThreadSelected")}</p>
            </div>
          </div>
        )}
      </div>

      {showKeys && (
        <div
          onClick={() => setShowKeys(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        >
          <div className="rounded-xl border border-slate-700 bg-slate-800 p-5 text-sm text-slate-200">
            <h4 className="mb-2 font-semibold">Клавиши</h4>
            <ul className="space-y-1 text-xs text-slate-400">
              <li>j / k — навигация</li>
              <li>Enter — открыть · u — назад</li>
              <li>c — написать · r — ответить (в треде)</li>
              <li>e — архив · # — удалить · s — пометить · x — выделить</li>
              <li>/ — поиск</li>
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
