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
import { AgentActivityPanel } from "./AgentActivityPanel";
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
  const [syncTimedOut, setSyncTimedOut] = useState(false);
  // Чек-лист первого запуска: пустой ящик предлагал «настроить почту» и на
  // этом заканчивался — про согласие, правила, шаблоны и подпись человек
  // должен был догадаться сам.
  const [setup, setSetup] = useState<
    { key: string; title: string; hint: string; done: boolean; url: string }[]
  >([]);
  const [setupHidden, setSetupHidden] = useState(false);
  useEffect(() => {
    try {
      setSetupHidden(localStorage.getItem("email:setup-hidden") === "1");
    } catch {
      /* ignore */
    }
    emailApi.setupStatus().then(setSetup).catch(() => setSetup([]));
  }, []);
  const setupPending = setup.filter((st) => !st.done);
  // Плотность списка и порядок сортировки — выбор человека, а не константа
  // в разметке. Хранится локально: это предпочтение рабочего места.
  const [dense, setDense] = useState(false);
  const [sort, setSort] = useState<"date_desc" | "date_asc" | "relevance">("date_desc");
  useEffect(() => {
    try {
      setDense(localStorage.getItem("email:dense") === "1");
      const saved = localStorage.getItem("email:sort");
      if (saved === "date_asc" || saved === "relevance" || saved === "date_desc") {
        setSort(saved);
      }
    } catch {
      /* приватный режим — просто дефолты */
    }
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem("email:dense", dense ? "1" : "0");
      localStorage.setItem("email:sort", sort);
    } catch {
      /* ignore */
    }
  }, [dense, sort]);

  const [activeMailbox, setActiveMailbox] = useState("");
  const [activeFolder, setActiveFolder] = useState("inbox");
  const [activeLabel, setActiveLabel] = useState<string | null>(null);
  const [starredOnly, setStarredOnly] = useState(false);
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  // Расширенный поиск. Сервер принимал from_addr/to_addr/date_from/date_to/
  // has_attachments/sort с самого начала, интерфейс не давал ничего, кроме
  // строки запроса: «письмо от Ромекса за март со вложением» искали глазами.
  const [advOpen, setAdvOpen] = useState(false);
  const [adv, setAdv] = useState<{
    from_addr: string;
    to_addr: string;
    date_from: string;
    date_to: string;
    has_attachments: boolean;
  }>({ from_addr: "", to_addr: "", date_from: "", date_to: "", has_attachments: false });
  const advActive =
    Boolean(adv.from_addr || adv.to_addr || adv.date_from || adv.date_to || adv.has_attachments);
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
  const [view, setView] = useState<"mail" | "contacts" | "activity">("mail");
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

  const searchFilters = useCallback(
    () => ({
      from_addr: adv.from_addr || undefined,
      to_addr: adv.to_addr || undefined,
      date_from: adv.date_from ? new Date(adv.date_from).toISOString() : undefined,
      date_to: adv.date_to ? new Date(adv.date_to).toISOString() : undefined,
      has_attachments: adv.has_attachments || undefined,
    }),
    [adv],
  );

  const loadThreads = useCallback(async () => {
    setLoading(true);
    try {
      // Ф5.1 — drafts and delayed sends do not live in EmailThread; they are
      // DraftAction rows. emailApi.drafts() existed and was called from
      // nowhere, so "Сохранить черновик" put the letter somewhere the user
      // could never reach again.
      if (!searchQuery.trim() && !advActive && (activeFolder === "drafts" || activeFolder === "outbox")) {
        const all = await emailApi.drafts(activeMailbox || undefined);
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
      if (searchQuery.trim() || advActive) {
        const res = await emailApi.search({
          query: searchQuery || undefined,
          mailbox: activeMailbox || undefined,
          folder: activeFolder !== "inbox" ? activeFolder : undefined,
          label_ids: activeLabel ? [activeLabel] : [],
          // Фильтры «непрочитанные» и «помеченные» видны включёнными и в
          // режиме поиска — раньше они там молча переставали действовать.
          is_unread: unreadOnly || undefined,
          is_starred: starredOnly || undefined,
          sort,
          ...searchFilters(),
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
  }, [activeMailbox, activeFolder, activeLabel, starredOnly, unreadOnly, searchQuery, advActive, searchFilters, sort, t]);

  /** Ф5.1 — append the next page. Without this a conversation older than the
   *  first page was simply unreachable. */
  const loadMore = useCallback(async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      if (searchQuery.trim() || advActive) {
        const res = await emailApi.search({
          query: searchQuery || undefined,
          mailbox: activeMailbox || undefined,
          folder: activeFolder !== "inbox" ? activeFolder : undefined,
          label_ids: activeLabel ? [activeLabel] : [],
          is_unread: unreadOnly || undefined,
          is_starred: starredOnly || undefined,
          sort,
          ...searchFilters(),
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
    activeFolder, activeLabel, starredOnly, unreadOnly, advActive, searchFilters, sort,
  ]);

  useEffect(loadMeta, [loadMeta]);

  // Ф7.3 — arriving from "Поделиться → Приложить к письму": open the composer
  // with the already-staged attachments.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const from = new URLSearchParams(window.location.search).get("from");
    if (!from) return;
    // Переход «все письма от этого отправителя» из карточки письма и из
    // адресной книги.
    setAdv((a) => ({ ...a, from_addr: from }));
    setAdvOpen(true);
    window.history.replaceState(null, "", "/email");
  }, []);

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

  const mergeFreshPage = useCallback(async () => {
    if (searchQuery.trim() || advActive || activeFolder === "drafts" || activeFolder === "outbox") {
      loadThreads();
      return;
    }
    const page = await emailApi
      .threads({
        mailbox: activeMailbox || undefined,
        folder: starredOnly ? undefined : activeFolder,
        label_id: activeLabel || undefined,
        is_starred: starredOnly || undefined,
        is_unread: unreadOnly || undefined,
      })
      .catch(() => null);
    if (!page) return;
    setThreads((prev) => {
      const fresh = new Map(page.items.map((th) => [th.id, th]));
      const updated = prev.map((th) => fresh.get(th.id) ?? th);
      const seen = new Set(updated.map((th) => th.id));
      const added = page.items.filter((th) => !seen.has(th.id));
      return added.length ? [...added, ...updated] : updated;
    });
  }, [
    searchQuery, advActive, activeFolder, activeMailbox, activeLabel,
    starredOnly, unreadOnly, loadThreads,
  ]);

  // Ф5.4 — every event used to trigger a full reload of the list AND the
  // sidebar; on a busy shared mailbox that is a request storm. Coalesce.
  const reloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEmailStream((e) => {
    if (e.type !== "email.new" && e.type !== "email.sent" && e.type !== "email.thread_updated") {
      return;
    }
    if (reloadTimer.current) clearTimeout(reloadTimer.current);
    reloadTimer.current = setTimeout(() => {
      // Полная перезагрузка заменяла список первой страницей: пролистал пять
      // страниц, пришло письмо — и список схлопнулся к началу. Подмешиваем
      // свежую страницу в уже показанное, сохраняя позицию.
      mergeFreshPage();
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
      // pushState, а не replaceState: на узком экране письмо ЗАМЕНЯЕТ список,
      // и системная кнопка «назад» — основной способ вернуться. С заменой
      // истории она уносила из почты целиком.
      window.history.pushState({ threadId: th.id }, "", `/email/${th.id}`);
    },
    [drafts],
  );

  const closeThread = useCallback(() => {
    setOpenThreadId(null);
    if (window.history.state?.threadId) window.history.back();
    else window.history.replaceState(null, "", "/email");
  }, []);

  // Кнопка «назад» браузера/телефона: закрывает письмо или композер, а не
  // выкидывает из раздела.
  useEffect(() => {
    function onPop() {
      const m = window.location.pathname.match(/^\/email\/([^/]+)$/);
      setCompose(null);
      setOpenThreadId(m ? m[1] : null);
    }
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Последнее «уносящее» действие — чтобы его можно было отменить. Отмена
  // существовала только для отправки: архив и удаление были необратимы одним
  // нажатием, хотя механика уже была написана.
  const [undoable, setUndoable] = useState<
    { ids: string[]; from: string; label: string } | null
  >(null);
  const undoTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const bulk = useCallback(
    async (
      action: string,
      ids?: string[],
      extra?: { folder?: string; label_id?: string; silent?: boolean },
    ) => {
      const target = ids ?? [...selected];
      if (!target.length) return;

      // Оптимистично: строка перерисовывается сразу, а не после полной
      // перезагрузки списка. Ошибка сервера возвращает состояние обратно.
      const snapshot = threads;
      setThreads((prev) =>
        prev
          .map((th) => {
            if (!target.includes(th.id)) return th;
            if (action === "read") return { ...th, is_read: true, unread_count: 0 };
            if (action === "unread")
              return { ...th, is_read: false, unread_count: Math.max(th.message_count, 1) };
            if (action === "star") return { ...th, is_starred: true };
            if (action === "unstar") return { ...th, is_starred: false };
            return th;
          })
          .filter(
            (th) =>
              !(
                ["archive", "trash", "spam", "inbox", "move"].includes(action) &&
                target.includes(th.id)
              ),
          ),
      );
      setSelected(new Set());

      try {
        await emailApi.bulkAction(target, action, {
          folder: extra?.folder,
          label_id: extra?.label_id,
        });
      } catch (e) {
        setThreads(snapshot);
        throw e;
      }

      if (["archive", "trash", "spam"].includes(action) && !extra?.silent) {
        const from = activeFolder === "drafts" ? "inbox" : activeFolder || "inbox";
        setUndoable({ ids: target, from, label: action });
        if (undoTimer.current) clearTimeout(undoTimer.current);
        undoTimer.current = setTimeout(() => setUndoable(null), 8000);
      }
      loadMeta();
    },
    [selected, threads, activeFolder, loadMeta],
  );

  const undoLastAction = useCallback(async () => {
    if (!undoable) return;
    const { ids, from } = undoable;
    setUndoable(null);
    await emailApi
      .bulkAction(ids, from === "inbox" ? "inbox" : "move", {
        folder: from === "inbox" ? undefined : from,
      })
      .catch(() => {});
    loadThreads();
    loadMeta();
  }, [undoable, loadThreads, loadMeta]);

  async function handleSync() {
    // Ф5.4 — was a fixed setTimeout(2500): the spinner stopped after 2.5 s no
    // matter what actually happened, so a slow or failed sync looked finished.
    setSyncing(true);
    try {
      const res = await emailApi.syncMailbox(activeMailbox || null);
      const taskId = (await res.json().catch(() => null))?.task_id as string | undefined;
      let finished = false;
      if (!taskId) {
        await new Promise((r) => setTimeout(r, 1500));
        finished = true;
      } else {
        for (let i = 0; i < 40; i++) {
          await new Promise((r) => setTimeout(r, 1500));
          const st = await emailApi.taskStatus(taskId).catch(() => null);
          if (st && ["SUCCESS", "FAILURE", "REVOKED"].includes(st.status)) {
            finished = true;
            break;
          }
        }
      }
      // «Проверка закончилась» и «мы устали ждать» выглядели одинаково: после
      // минуты ожидания спиннер просто гас, как будто всё прошло.
      setSyncTimedOut(!finished);
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
        // Действия, которые были только в панели выделения.
        case "!":
          if (cur) bulk("spam", [cur.id]);
          break;
        case "I":
          if (cur) bulk("read", [cur.id]);
          break;
        case "U":
          if (cur) bulk("unread", [cur.id]);
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
      return one ? t("syncBroken", { error: one.sync_error! }) : null;
    }
    return broken.length === 1
      ? t("syncBrokenMailbox", { mailbox: broken[0].name, error: broken[0].sync_error! })
      : t("syncBrokenMany", { n: broken.length });
  })();

  const emptyState =
    mailboxes.length === 0 ? (
      <>
        {t("noMailboxes")}
        <br />
        <a href="/settings?tab=email" className="text-blue-600 dark:text-blue-400 underline">
          {t("configure")}
        </a>
      </>
    ) : mailboxes.find((m) => m.name === activeMailbox)?.sync_error ? (
      <>
        <span className="text-red-500 dark:text-red-400">
          {t("syncError", {
            error: mailboxes.find((m) => m.name === activeMailbox)!.sync_error!,
          })}
        </span>
        <br />
        <button onClick={handleSync} className="mt-2 text-blue-600 dark:text-blue-400 underline">
          {t("retry")}
        </button>
      </>
    ) : activeMailbox && !mailboxes.some((m) => m.name === activeMailbox) ? (
      // Ф5.4 — «нет доступа» и «нет писем» выглядели одинаково, и человек
      // ждал письма в ящике, которого он вообще не видит.
      <>
        <span className="text-amber-600 dark:text-amber-400">
          {t("empty.noAccess", { mailbox: activeMailbox })}
        </span>
        <br />
        <span className="text-xs">{t("empty.noAccessHint")}</span>
      </>
    ) : searchQuery ? (
      <>
        {t("empty.noResults", { query: searchQuery })}
        <br />
        <span className="text-xs">{t("empty.noResultsHint")}</span>
      </>
    ) : unreadOnly || starredOnly || activeLabel || advActive ? (
      <>
        {t("empty.filtered")}
        <br />
        <button
          onClick={() => {
            setUnreadOnly(false);
            setStarredOnly(false);
            setActiveLabel(null);
            setAdv({
              from_addr: "", to_addr: "", date_from: "", date_to: "",
              has_attachments: false,
            });
          }}
          className="mt-2 text-blue-600 underline dark:text-blue-400"
        >
          {t("empty.showAll")}
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
        onSelectActivity={() => setView("activity")}
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

      {view === "activity" ? (
        <AgentActivityPanel mailbox={activeMailbox} />
      ) : view === "contacts" ? (
        <ContactsPanel
          onCompose={(email) => {
            setView("mail");
            setCompose({ kind: "new", to: [email] });
          }}
        />
      ) : (
      <>
      <div
        className={`flex flex-col border-r border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/40 ${
          isNarrow
            ? openThreadId || compose
              ? "hidden"
              : "w-full"
            : "w-72 shrink-0"
        }`}
      >
        {setupPending.length > 0 && !setupHidden && (
          <div className="border-b border-blue-200 bg-blue-50 px-3 py-2 text-[11px] dark:border-blue-900/60 dark:bg-blue-950/25">
            <div className="mb-1 flex items-center gap-2">
              <span className="font-medium text-blue-800 dark:text-blue-300">
                {t("setup.title", { done: setup.length - setupPending.length, total: setup.length })}
              </span>
              <button
                onClick={() => {
                  setSetupHidden(true);
                  try {
                    localStorage.setItem("email:setup-hidden", "1");
                  } catch {
                    /* ignore */
                  }
                }}
                className="ml-auto text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
              >
                {t("setup.hide")}
              </button>
            </div>
            <ul className="space-y-0.5">
              {setupPending.slice(0, 3).map((st) => (
                <li key={st.key}>
                  <a href={st.url} className="text-blue-700 hover:underline dark:text-blue-300">
                    {st.title}
                  </a>
                  <span className="ml-1 text-slate-500">— {st.hint}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {syncTimedOut && !syncErrorBanner && (
          <div className="flex items-center gap-2 border-b border-amber-300 bg-amber-50 px-3 py-1.5 text-[11px] text-amber-800 dark:border-amber-800/60 dark:bg-amber-950/20 dark:text-amber-300">
            <span>{t("syncStillRunning")}</span>
            <button
              onClick={() => {
                setSyncTimedOut(false);
                loadThreads();
              }}
              className="underline"
            >
              {t("retry")}
            </button>
          </div>
        )}
        {syncErrorBanner && (
          <div className="border-b border-red-300 dark:border-red-900/60 bg-red-50 dark:bg-red-950/30 px-3 py-1.5 text-[11px] text-red-600 dark:text-red-300">
            {syncErrorBanner}{" "}
            <button onClick={handleSync} className="underline hover:text-red-500 dark:hover:text-red-200">
              {t("retry")}
            </button>
          </div>
        )}
        <div className="border-b border-slate-200 dark:border-slate-700 p-2">
          <div className="mb-1.5 flex items-center gap-1.5">
            {!sidebarOpen && (
              <button
                onClick={() => setSidebarOpen(true)}
                className="rounded p-1 text-slate-500 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-700 hover:text-slate-800 dark:hover:text-slate-200"
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
            className="flex-1 rounded bg-slate-100 dark:bg-slate-700 px-3 py-1.5 text-sm text-slate-900 dark:text-slate-100 placeholder-slate-400 dark:placeholder-slate-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
            <button
              onClick={() => setUnreadOnly((u) => !u)}
              aria-pressed={unreadOnly}
              className={`rounded-full px-2 py-0.5 ${unreadOnly ? "bg-blue-600 text-white" : "text-slate-500 hover:bg-slate-100 dark:hover:text-slate-400 dark:hover:bg-slate-100 dark:hover:bg-slate-700"}`}
            >
              {t("filters.unreadOnly")}
            </button>
            <button
              onClick={() => setAdvOpen((o) => !o)}
              aria-expanded={advOpen}
              className={`rounded-full px-2 py-0.5 ${advActive || advOpen ? "bg-blue-600 text-white" : "text-slate-500 hover:bg-slate-100 dark:hover:text-slate-400 dark:hover:bg-slate-100 dark:hover:bg-slate-700"}`}
            >
              {t("filters.advanced")}
            </button>
            <button
              onClick={() => {
                const unread = threads.filter((th) => !th.is_read).map((th) => th.id);
                if (unread.length) bulk("read", unread);
              }}
              className="text-slate-500 hover:text-slate-900 dark:hover:text-slate-400 dark:hover:text-slate-800 dark:hover:text-slate-200"
            >
              {t("actions.markAllRead")}
            </button>
            <button
              onClick={() => setCompose({ kind: "new" })}
              className="ml-auto rounded bg-blue-600 px-2.5 py-0.5 text-white hover:bg-blue-500"
            >
              + {t("compose")}
            </button>
          </div>

          {advOpen && (
            <div className="mt-2 space-y-1.5 rounded border border-slate-200 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-800/60">
              <input
                value={adv.from_addr}
                onChange={(e) => setAdv({ ...adv, from_addr: e.target.value })}
                placeholder={t("filters.fromAddr")}
                className="w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100"
              />
              <input
                value={adv.to_addr}
                onChange={(e) => setAdv({ ...adv, to_addr: e.target.value })}
                placeholder={t("filters.toAddr")}
                className="w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100"
              />
              <div className="flex gap-1.5">
                <input
                  type="date"
                  aria-label={t("filters.dateFrom")}
                  value={adv.date_from}
                  onChange={(e) => setAdv({ ...adv, date_from: e.target.value })}
                  className="w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100"
                />
                <input
                  type="date"
                  aria-label={t("filters.dateTo")}
                  value={adv.date_to}
                  onChange={(e) => setAdv({ ...adv, date_to: e.target.value })}
                  className="w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-800 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-100"
                />
              </div>
              <label className="flex items-center gap-1.5 text-slate-400 dark:text-slate-600 dark:text-slate-300">
                <input
                  type="checkbox"
                  checked={adv.has_attachments}
                  onChange={(e) => setAdv({ ...adv, has_attachments: e.target.checked })}
                  className="accent-blue-600"
                />
                {t("filters.hasAttachments")}
              </label>
              <div className="flex items-center gap-2">
                <select
                  value={sort}
                  aria-label={t("filters.sort")}
                  onChange={(e) => setSort(e.target.value as typeof sort)}
                  className="rounded border border-slate-300 bg-white px-1 py-0.5 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-200"
                >
                  <option value="date_desc">{t("filters.sortNewest")}</option>
                  <option value="date_asc">{t("filters.sortOldest")}</option>
                  <option value="relevance">{t("filters.sortRelevance")}</option>
                </select>
                <label className="flex items-center gap-1 text-slate-600 dark:text-slate-300">
                  <input
                    type="checkbox"
                    checked={dense}
                    onChange={(e) => setDense(e.target.checked)}
                    className="accent-blue-600"
                  />
                  {t("filters.dense")}
                </label>
              </div>
              {advActive && (
                <button
                  onClick={() =>
                    setAdv({
                      from_addr: "", to_addr: "", date_from: "", date_to: "",
                      has_attachments: false,
                    })
                  }
                  className="text-blue-600 hover:underline dark:text-blue-400"
                >
                  {t("filters.reset")}
                </button>
              )}
            </div>
          )}
        </div>

        {selected.size > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-b border-slate-200 bg-slate-100 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800">
            <span className="text-slate-700 dark:text-slate-300">
              {t("bulk.selected", { n: selected.size })}
            </span>
            <button onClick={() => bulk("read")} className="text-slate-500 hover:text-slate-900 dark:hover:text-slate-400 dark:hover:text-slate-800 dark:hover:text-slate-200">
              {t("actions.markRead")}
            </button>
            {/* Действия, которые сервер поддерживал всё это время, а нажать
                их было негде: «не прочитано», «в спам», «вернуть во входящие»
                и метки. */}
            <button onClick={() => bulk("unread")} className="text-slate-500 hover:text-slate-900 dark:hover:text-slate-400 dark:hover:text-slate-800 dark:hover:text-slate-200">
              {t("actions.markUnread")}
            </button>
            <button onClick={() => bulk("archive")} className="text-slate-500 hover:text-slate-900 dark:hover:text-slate-400 dark:hover:text-slate-800 dark:hover:text-slate-200">
              {t("actions.archive")}
            </button>
            {activeFolder !== "spam" ? (
              <button onClick={() => bulk("spam")} className="text-slate-500 hover:text-amber-600 dark:hover:text-slate-400 dark:hover:text-amber-700 dark:hover:text-amber-300">
                {t("actions.spam")}
              </button>
            ) : (
              <button onClick={() => bulk("inbox")} className="text-slate-500 hover:text-emerald-600 dark:hover:text-slate-400 dark:hover:text-emerald-700 dark:hover:text-emerald-300">
                {t("actions.notSpam")}
              </button>
            )}
            {(activeFolder === "trash" || activeFolder === "archive") && (
              <button onClick={() => bulk("inbox")} className="text-slate-500 hover:text-emerald-600 dark:hover:text-slate-400 dark:hover:text-emerald-700 dark:hover:text-emerald-300">
                {t("actions.restore")}
              </button>
            )}
            <button
              onClick={() => bulk("trash")}
              className="text-slate-500 hover:text-red-500 dark:hover:text-slate-400 dark:hover:text-red-600 dark:hover:text-red-300"
            >
              {t("actions.trash")}
            </button>
            {labels.length > 0 && (
              <select
                value=""
                aria-label={t("actions.label")}
                onChange={(e) => {
                  if (e.target.value) bulk("add_label", undefined, { label_id: e.target.value });
                }}
                className="rounded border border-slate-300 bg-white px-1 py-0.5 text-xs text-slate-700 dark:border-slate-600 dark:bg-slate-700 dark:text-slate-200"
              >
                <option value="">{t("actions.label")}…</option>
                {labels.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            )}
            <button
              onClick={() => setSelected(new Set(threads.map((th) => th.id)))}
              className="text-slate-500 hover:text-slate-900 dark:hover:text-slate-400 dark:hover:text-slate-800 dark:hover:text-slate-200"
            >
              {t("actions.selectAll")}
            </button>
            <button
              onClick={() => setSelected(new Set())}
              className="ml-auto text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-500 dark:hover:text-slate-700 dark:hover:text-slate-300"
            >
              {t("bulk.clear")}
            </button>
          </div>
        )}

        {undoable && (
          <div className="flex items-center gap-2 border-b border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs text-emerald-800 dark:border-emerald-800/60 dark:bg-emerald-950/25 dark:text-emerald-300">
            <span>{t("bulk.moved", { n: undoable.ids.length })}</span>
            <button
              onClick={undoLastAction}
              className="rounded border border-emerald-400 px-2 py-0.5 hover:bg-emerald-100 dark:hover:border-emerald-700 dark:hover:bg-emerald-900/40"
            >
              {t("undo")}
            </button>
          </div>
        )}

        <div className="min-h-0 flex-1">
          <ThreadList
            threads={threads}
            folder={starredOnly ? "starred" : activeFolder}
            dense={dense}
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
        <p className="border-t border-slate-200 dark:border-slate-800 px-2 py-1 text-[10px] text-slate-400 dark:text-slate-600">
          {t("keysHint")}
        </p>
      </div>

      <div
        className={`flex-1 bg-white dark:bg-slate-900 ${
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
            onRead={(id) => {
              // Раньше тред помечался прочитанным внутри просмотра и никому об
              // этом не сообщал: слева письмо оставалось жирным, а счётчик
              // непрочитанных не менялся до перезагрузки.
              setThreads((prev) =>
                prev.map((th) =>
                  th.id === id ? { ...th, is_read: true, unread_count: 0 } : th,
                ),
              );
              loadMeta();
            }}
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
          role="dialog"
          aria-modal="true"
          aria-label={t("keysTitle")}
          onClick={() => setShowKeys(false)}
          onKeyDown={(e) => {
            if (e.key === "Escape") setShowKeys(false);
          }}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
        >
          <div
            role="document"
            tabIndex={-1}
            ref={(el) => el?.focus()}
            onClick={(e) => e.stopPropagation()}
            className="rounded-xl border border-slate-300 bg-white p-5 text-sm text-slate-800 focus:outline-none dark:focus:border-slate-700 dark:bg-slate-800 dark:text-slate-200"
          >
            <h4 className="mb-2 font-semibold">{t("keysTitle")}</h4>
            <ul className="space-y-1 text-xs text-slate-400 dark:text-slate-600 dark:text-slate-400">
              {(t.raw("keys") as string[]).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
