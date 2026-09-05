"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch } from "@/lib/auth";
import { httpDetail, notifyError } from "@/components/ui/primitives/Toast";

const API = getApiBaseUrl();

type Command = {
  id: string;
  label: string;
  hint?: string;
  run: () => void;
};

const SECTIONS: { label: string; path: string; hint: string }[] = [
  { label: "Почта", path: "/email", hint: "письма и переписка" },
  { label: "Входящие дела", path: "/inbox", hint: "что ждёт решения" },
  { label: "Счета", path: "/invoices", hint: "" },
  { label: "Документы", path: "/documents", hint: "" },
  { label: "Аномалии", path: "/anomalies", hint: "" },
  { label: "Поручения", path: "/work-orders", hint: "работа агента" },
  { label: "Согласования", path: "/approvals", hint: "" },
  { label: "Каталоги", path: "/catalogs", hint: "позиции поставщиков" },
  { label: "Настройки", path: "/settings", hint: "" },
];

/**
 * Одна строка на всё: разделы, действия и поиск по письмам и счетам.
 *
 * Поиск жил отдельно в каждом разделе, а проект заявляет keyboard-first: чтобы
 * найти счёт, нужно было сначала догадаться, в каком разделе искать. Ctrl/⌘+K
 * открывает палитру откуда угодно.
 */
export function CommandPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [remote, setRemote] = useState<Command[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        setQuery("");
        setActive(0);
      }
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 10);
  }, [open]);

  const go = useCallback(
    (path: string) => {
      setOpen(false);
      router.push(path);
    },
    [router],
  );

  // Поиск по данным: письма и счета. Запускается с трёх символов, чтобы
  // палитра оставалась быстрой на навигации.
  useEffect(() => {
    const q = query.trim();
    if (q.length < 3) {
      setRemote([]);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      const found: Command[] = [];
      try {
        const res = await apiFetch(`${API}/api/email/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ query: q, limit: 5 }),
        });
        if (res.ok) {
          const data = (await res.json()) as {
            results: { id: string; thread_id: string | null; subject: string | null;
                       from_address: string }[];
          };
          for (const m of data.results) {
            found.push({
              id: `mail-${m.id}`,
              label: m.subject || "(без темы)",
              hint: `письмо · ${m.from_address}`,
              run: () => go(m.thread_id ? `/email/${m.thread_id}` : "/email"),
            });
          }
        } else {
          notifyError(
            "Поиск по письмам не выполнен",
            httpDetail(res.status, res.statusText),
          );
        }
      } catch {
        /* поиск не обязан работать, чтобы палитра открылась */
      }
      try {
        const res = await apiFetch(
          `${API}/api/invoices?search=${encodeURIComponent(q)}&limit=5`,
        );
        if (res.ok) {
          const data = (await res.json()) as {
            items?: { id: string; invoice_number: string | null; total_amount: number | null }[];
          };
          for (const inv of data.items ?? []) {
            found.push({
              id: `inv-${inv.id}`,
              label: `Счёт ${inv.invoice_number ?? "б/н"}`,
              hint: inv.total_amount != null ? `${inv.total_amount}` : "счёт",
              run: () => go(`/invoices/${inv.id}`),
            });
          }
        }
      } catch {
        /* см. выше */
      }
      if (!cancelled) setRemote(found);
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, go]);

  const commands = useMemo<Command[]>(() => {
    const q = query.trim().toLowerCase();
    const local: Command[] = [
      ...SECTIONS.map((s) => ({
        id: `nav-${s.path}`,
        label: s.label,
        hint: s.hint,
        run: () => go(s.path),
      })),
      {
        id: "act-compose",
        label: "Написать письмо",
        hint: "новый черновик",
        run: () => go("/email?compose=1"),
      },
      {
        id: "act-agent",
        label: "Спросить агента",
        hint: "открыть чат",
        run: () => go("/chat"),
      },
    ];
    const filtered = q
      ? local.filter(
          (c) =>
            c.label.toLowerCase().includes(q) || (c.hint ?? "").toLowerCase().includes(q),
        )
      : local;
    return [...filtered, ...remote];
  }, [query, remote, go]);

  if (!open) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Команды"
      className="fixed inset-0 z-[100] flex items-start justify-center bg-black/50 p-4 pt-24"
      onClick={() => setOpen(false)}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-xl border border-slate-300 bg-white shadow-2xl dark:border-slate-700 dark:bg-slate-800"
      >
        <input
          ref={inputRef}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActive(0);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(a + 1, commands.length - 1));
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(a - 1, 0));
            }
            if (e.key === "Enter") {
              e.preventDefault();
              commands[active]?.run();
            }
          }}
          placeholder="Раздел, действие или поиск…"
          className="w-full border-b border-slate-200 bg-transparent px-4 py-3 text-sm text-slate-900 placeholder-slate-400 focus:outline-none dark:border-slate-700 dark:text-slate-100"
        />
        <ul className="max-h-80 overflow-auto py-1">
          {commands.length === 0 && (
            <li className="px-4 py-3 text-xs text-slate-400">Ничего не найдено</li>
          )}
          {commands.map((c, i) => (
            <li key={c.id}>
              <button
                onMouseEnter={() => setActive(i)}
                onClick={c.run}
                className={`flex w-full items-baseline gap-2 px-4 py-2 text-left text-sm ${
                  i === active
                    ? "bg-blue-50 dark:bg-blue-900/30"
                    : "hover:bg-slate-100 dark:hover:bg-slate-700/50"
                }`}
              >
                <span className="text-slate-800 dark:text-slate-100">{c.label}</span>
                {c.hint && (
                  <span className="truncate text-xs text-slate-400">{c.hint}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
        <p className="border-t border-slate-200 px-4 py-1.5 text-[10px] text-slate-400 dark:border-slate-700">
          ↑↓ выбор · Enter открыть · Esc закрыть
        </p>
      </div>
    </div>
  );
}
