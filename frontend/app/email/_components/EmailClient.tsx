"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";
import { useEmailStream } from "./useEmailStream";
import { MailSidebar } from "./MailSidebar";
import { ThreadList } from "./ThreadList";
import { ThreadView } from "./ThreadView";
import { Composer } from "./Composer";
import { ContactsPanel } from "./ContactsPanel";
import type {
  ComposeMode,
  EmailDraft,
  EmailLabel,
  EmailThread,
  MailboxChip,
} from "./types";

export function EmailClient({ initialThreadId }: { initialThreadId?: string }) {
  const t = useTranslations("email");

  const [mailboxes, setMailboxes] = useState<MailboxChip[]>([]);
  const [labels, setLabels] = useState<EmailLabel[]>([]);
  const [threads, setThreads] = useState<EmailThread[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<EmailDraft[]>([]);
  const [syncing, setSyncing] = useState(false);

  const [activeMailbox, setActiveMailbox] = useState("");
  const [activeFolder, setActiveFolder] = useState("inbox");
  const [activeLabel, setActiveLabel] = useState<string | null>(null);
  const [starredOnly, setStarredOnly] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  // Ф5.4 — the query used to be a direct dependency of loadThreads, so every
  // keystroke fired a full-text search.
  const [searchQuery, setSearchQuery] = useState("");
  useEffect(() => {
    const id = setTimeout(() => setSearchQuery(searchInput), 350);
    return () => clearTimeout(id);
  }, [searchInput]);

  const [openThreadId, setOpenThreadId] = useState<string | null>(initialThreadId ?? null);
  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [compose, setCompose] = useState<ComposeMode | null>(null);
  const [view, setView] = useState<"mail" | "contacts">("mail");
  const [showKeys, setShowKeys] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  // Ф7.2 — the three-panel layout is unusable on a phone: list, thread and
  // composer all competed for the same 380 px. On narrow screens exactly one
  // pane is shown and navigation is list → thread → composer with a back arrow.
  const [isNarrow, setIsNarrow] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const apply = () => {
      setIsNarrow(mq.matches);
      if (mq.matches) setSidebarOpen(false);
    };
    apply();
    mq.addEventListener("change", apply);
    return () => mq.removeEventListener("change", apply);
  }, []);
  const searchRef = useRef<HTMLInputElement>(null);

  const loadMeta = useCallback(() => {
    emailApi.mailboxes().then(setMailboxes).catch(() => {});
    emailApi.labels().then(setLabels).catch(() => {});
  }, []);

  const loadThreads = useCallback(async () => {
    setLoading(true);
    try {
      // Ф5.1 — drafts and delayed sends do not live in EmailThread; they are
      // DraftAction rows. emailApi.drafts() existed and was called from
      // nowhere, so "Сохранить черновик" put the letter somewhere the user
      // could never reach again.
      if (!searchQuery.trim() && (activeFolder === "drafts" || activeFolder === "outbox")) {
        const all = await emailApi.drafts();
        const wanted = all.filter((d) =>
          activeFolder === "outbox"
            ? d.status === "queued"
            : d.status !== "queued",
        );
        setDrafts(wanted);
        setThreads(
          wanted.map((d) => ({
            id: `draft:${d.id}`,
            subject: d.subject || t("noSubject"),
            mailbox: d.mailbox ?? "",
            message_count: 1,
            last_message_at: d.created_at,
            created_at: d.created_at,
            is_read: true,
            is_starred: false,
            has_attachments: (d.attachment_ids ?? []).length > 0,
            folder: activeFolder,
            last_snippet: (d.to_addresses ?? []).join(", "),
            unread_count: 0,
            labels: [],
            sender: (d.to_addresses ?? [])[0] ?? "",
            messages: [],
          })),
        );
        setNextCursor(null);
        return;
      }
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
        setNextCursor(res.next_cursor);
      } else {
        const page = await emailApi.threads({
          mailbox: activeMailbox || undefined,
          folder: starredOnly ? undefined : activeFolder,
          label_id: activeLabel || undefined,
          is_starred: starredOnly || undefined,
          is_unread: unreadOnly || undefined,
        });
        setThreads(page.items);
        setNextCursor(page.next_cursor);
      }
    } finally {
      setLoading(false);
    }
  }, [activeMailbox, activeFolder, activeLabel, starredOnly, unreadOnly, searchQuery]);

  /** Ф5.1 — append the next page. Without this a conversation older than the
   *  first page was simply unreachable. */
  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      if (searchQuery.trim()) {
        const res = await emailApi.search({
          query: searchQuery,
          mailbox: activeMailbox || undefined,
          folder: activeFolder !== "inbox" ? activeFolder : undefined,
          label_ids: activeLabel ? [activeLabel] : [],
          cursor: nextCursor,
        });
        const extra: EmailThread[] = res.results.map((m) => ({
          id: m.thread_id ?? m.id,
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
        }));
        setThreads((prev) => {
          const seen = new Set(prev.map((t) => t.id));
          return [...prev, ...extra.filter((t) => !seen.has(t.id))];
        });
        setNextCursor(res.next_cursor);
      } else {
        const page = await emailApi.threads({
          mailbox: activeMailbox || undefined,
          folder: starredOnly ? undefined : activeFolder,
          label_id: activeLabel || undefined,
          is_starred: starredOnly || undefined,
          is_unread: unreadOnly || undefined,
          cursor: nextCursor,
        });
        setThreads((prev) => {
          const seen = new Set(prev.map((t) => t.id));
          return [...prev, ...page.items.filter((t) => !seen.has(t.id))];
        });
        setNextCursor(page.next_cursor);
      }
    } finally {
      setLoadingMore(false);
    }
  }, [
    nextCursor, loadingMore, searchQuery, activeMailbox,
    activeFolder, activeLabel, starredOnly, unreadOnly,
  ]);

  useEffect(loadMeta, [loadMeta]);

  // Ф7.3 — arriving from "Поделиться → Приложить к письму": open the composer
  // with the already-staged attachments.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (params.get("compose") !== "1") return;
    let staged: string[] = [];
    try {
      staged = JSON.parse(sessionStorage.getItem("email:pending-attachments") || "[]");
    } catch {
      staged = [];
    }
    sessionStorage.removeItem("email:pending-attachments");
    setCompose({ kind: "new", attachmentIds: staged });
    window.history.replaceState(null, "", "/email");
  }, []);
  useEffect(() => {
    loadThreads();
  }, [loadThreads]);

  // Ф5.4 — every event used to trigger a full reload of the list AND the
  // sidebar; on a busy shared mailbox that is a request storm. Coalesce.
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEmailStream((e) => {
    if (e.type !== "email.new" && e.type !== "email.sent" && e.type !== "email.thread_updated") {
      return;
    }
    if (reloadTimer.current) clearTimeout(reloadTimer.current);
    reloadTimer.current = setTimeout(() => {
      loadThreads();
      loadMeta();
    }, 800);
  });

  const openThread = useCallback(
    (th: EmailThread) => {
      // A draft row opens in the composer, not the reader.
      if (th.id.startsWith("draft:")) {
        const draft = drafts.find((d) => `draft:${d.id}` === th.id);
        if (draft) {
          setOpenThreadId(null);
          setCompose({ kind: "draft", draft });
        }
        return;
      }
      setOpenThreadId(th.id);
      setCompose(null);
      window.history.replaceState(null, "", `/email/${th.id}`);
    },
    [drafts],
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
    // Ф5.4 — was a fixed setTimeout(2500): the spinner stopped after 2.5 s no
    // matter what actually happened, so a slow or failed sync looked finished.
    setSyncing(true);
    try {
      const res = await emailApi.syncMailbox(activeMailbox || null);
      const taskId = (await res.json().catch(() => null))?.task_id as string | undefined;
      if (!taskId) {
        await new Promise((r) => setTimeout(r, 1500));
      } else {
        for (let i = 0; i < 40; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          const st = await emailApi.taskStatus(taskId).catch(() => null);
          if (st && ["SUCCESS", "FAILURE", "REVOKED"].includes(st.status)) break;
        }
      }
      loadThreads();
      loadMeta();
    } finally {
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
        // Ф5.4 — "r" was listed in the shortcut help and did nothing.
        case "r":
        case "a":
        case "f": {
          const target = openThreadId
            ? threads.find((t) => t.id === openThreadId)
            : cur;
          if (!target || !target.messages?.length) break;
          const last = target.messages[target.messages.length - 1];
          setCompose(
            e.key === "f"
              ? { kind: "forward", message: last }
              : { kind: "reply", message: last, all: e.key === "a" },
          );
          break;
        }
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

  // Ф5.4 — a sync failure used to appear only in the empty-list placeholder,
  // so a mailbox with old mail looked healthy while nothing new arrived.
  const syncErrorBanner = (() => {
    const broken = mailboxes.filter((m) => m.sync_error);
    if (!broken.length) return null;
    if (activeMailbox) {
      const one = broken.find((m) => m.name === activeMailbox);
      return one ? `Синхронизация не работает: ${one.sync_error}` : null;
    }
    return broken.length === 1
      ? `Ящик ${broken[0].name}: ${broken[0].sync_error}`
      : `Синхронизация не работает у ${broken.length} ящиков`;
  })();

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
    ) : activeMailbox && !mailboxes.some((m) => m.name === activeMailbox) ? (
      // Ф5.4 — «нет доступа» и «нет писем» выглядели одинаково, и человек
      // ждал письма в ящике, которого он вообще не видит.
      <>
        <span className="text-amber-400">Нет доступа к ящику «{activeMailbox}»</span>
        <br />
        <span className="text-xs">
          Личные ящики видны только владельцу. Попросите доступ или выберите
          другой ящик слева.
        </span>
      </>
    ) : searchQuery ? (
      <>
        По запросу «{searchQuery}» ничего не найдено
        <br />
        <span className="text-xs">
          Поиск идёт по теме, тексту, отправителю, именам вложений и
          распознанному содержимому. Попробуйте короче или снимите фильтры.
        </span>
      </>
    ) : unreadOnly || starredOnly || activeLabel ? (
      <>
        Здесь пусто с текущими фильтрами
        <br />
        <button
          onClick={() => {
            setUnreadOnly(false);
            setStarredOnly(false);
            setActiveLabel(null);
          }}
          className="mt-2 text-blue-400 underline"
        >
          Показать все письма
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
        view={view}
        onSelectContacts={() => setView("contacts")}
        mailboxes={mailboxes}
        labels={labels}
        activeMailbox={activeMailbox}
        activeFolder={activeFolder}
        activeLabel={activeLabel}
        starredOnly={starredOnly}
        onSelectMailbox={(n) => {
          setView("mail");
          setActiveMailbox(n);
          setActiveFolder("inbox");
          setActiveLabel(null);
          setStarredOnly(false);
        }}
        onSelectFolder={(f) => {
          setView("mail");
          setActiveFolder(f);
          setActiveLabel(null);
          setStarredOnly(false);
        }}
        onSelectLabel={(id) => {
          setView("mail");
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

      {view === "contacts" ? (
        <ContactsPanel
          onCompose={(email) => {
            setView("mail");
            setCompose({ kind: "new", to: [email] });
          }}
        />
      ) : (
      <>
      <div
        className={`flex flex-col border-r border-slate-700 bg-slate-800/40 ${
          isNarrow
            ? openThreadId || compose
              ? "hidden"
              : "w-full"
            : "w-72 shrink-0"
        }`}
      >
        {syncErrorBanner && (
          <div className="border-b border-red-900/60 bg-red-950/30 px-3 py-1.5 text-[11px] text-red-300">
            {syncErrorBanner}{" "}
            <button onClick={handleSync} className="underline hover:text-red-200">
              повторить
            </button>
          </div>
        )}
        <div className="border-b border-slate-700 p-2">
          <div className="mb-1.5 flex items-center gap-1.5">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                className="rounded p-1 text-slate-400 hover:bg-slate-700 hover:text-slate-200"
                title={t("foldersTitle")}
              >
                ☰
              </button>
            )}
            <input
            ref={searchRef}
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
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
            onLoadMore={loadMore}
            loadingMore={loadingMore}
            hasMore={Boolean(nextCursor)}
          />
        </div>
        <p className="border-t border-slate-800 px-2 py-1 text-[10px] text-slate-600">
          {t("keysHint")}
        </p>
      </div>

      <div
        className={`flex-1 bg-slate-900 ${
          isNarrow && !openThreadId && !compose ? "hidden" : ""
        }`}
      >
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
            onReply={(m, all, assist) =>
              setCompose({ kind: "reply", message: m, all, assist })
            }
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
      </>
      )}

      {showKeys && (
        <div
          onClick={() => setShowKeys(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        >
          <div className="rounded-xl border border-slate-700 bg-slate-800 p-5 text-sm text-slate-200">
            <h4 className="mb-2 font-semibold">{t("keysTitle")}</h4>
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
