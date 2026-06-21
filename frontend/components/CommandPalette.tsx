"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";

const API = getApiBaseUrl();

interface SearchResult {
  id: string;
  type: string;
  title: string;
  subtitle?: string;
  href: string;
}

const NAV_COMMANDS = [
  { title: "Главная / Рабочий стол", href: "/", icon: "🏠" },
  { title: "Входящие документы", href: "/inbox", icon: "📥" },
  { title: "Счета", href: "/invoices", icon: "🧾" },
  { title: "Документы", href: "/documents", icon: "📄" },
  { title: "Поставщики", href: "/suppliers", icon: "🏭" },
  { title: "Аномалии", href: "/anomalies", icon: "⚠️" },
  { title: "Дела / Подборки", href: "/cases", icon: "📂" },
  { title: "Сравнение КП", href: "/compare", icon: "⚖️" },
  { title: "Календарь", href: "/calendar", icon: "📅" },
  { title: "Согласования", href: "/approvals", icon: "✅" },
  { title: "Поиск (NL)", href: "/search", icon: "🔍" },
  { title: "Настройки", href: "/settings", icon: "⚙️" },
  { title: "Чат с агентом AI-DOCS", href: "/chat", icon: "💬" },
];

const TYPE_ICONS: Record<string, string> = {
  invoice: "🧾",
  document: "📄",
  supplier: "🏭",
  anomaly: "⚠️",
  collection: "📂",
};

const TYPE_LABELS: Record<string, string> = {
  invoice: "Счёт",
  document: "Документ",
  supplier: "Поставщик",
  anomaly: "Аномалия",
  collection: "Дело",
};

export function CommandPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        setQuery("");
        setResults([]);
        setSelected(0);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (open) {
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  const filtered = query.trim()
    ? NAV_COMMANDS.filter((c) =>
        c.title.toLowerCase().includes(query.toLowerCase()),
      )
    : NAV_COMMANDS;

  const allItems: Array<
    | { type: "nav"; title: string; href: string; icon: string }
    | (SearchResult & { type: string })
  > = [...filtered.map((c) => ({ ...c, type: "nav" as const })), ...results];

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await fetch(
          `${API}/api/search/unified?q=${encodeURIComponent(query)}&limit=5`,
        );
        if (res.ok) {
          const data = await res.json();
          const mapped: SearchResult[] = (data.results ?? []).map(
            (r: {
              id: string;
              type: string;
              title: string;
              subtitle?: string;
            }) => ({
              id: r.id,
              type: r.type,
              title: r.title,
              subtitle: r.subtitle,
              href: `/${r.type}s/${r.id}`,
            }),
          );
          setResults(mapped);
        }
      } catch {
        // ignore
      } finally {
        setLoading(false);
      }
    }, 250);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  useEffect(() => {
    setSelected(0);
  }, [query]);

  function navigate(href: string) {
    router.push(href);
    setOpen(false);
    setQuery("");
    setResults([]);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, allItems.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      const item = allItems[selected];
      if (item) navigate(item.href);
    }
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/50 backdrop-blur-sm"
      onClick={() => setOpen(false)}
    >
      <div
        className="w-full max-w-xl bg-slate-900 border border-slate-700 rounded-xl shadow-2xl overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Input */}
        <div className="flex items-center gap-3 px-4 py-3 border-b border-slate-700">
          <span className="text-slate-500 text-sm">🔍</span>
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Поиск или переход... (↑↓ Enter)"
            className="flex-1 bg-transparent text-slate-100 placeholder-slate-500 outline-none text-sm"
          />
          {loading && (
            <span className="text-[10px] text-slate-500 animate-pulse">
              ...
            </span>
          )}
          <kbd className="text-[10px] px-1.5 py-0.5 bg-slate-800 text-slate-400 rounded border border-slate-700">
            Esc
          </kbd>
        </div>

        {/* Items */}
        <div className="max-h-[60vh] overflow-y-auto py-1">
          {allItems.length === 0 && query && !loading && (
            <div className="px-4 py-6 text-center text-slate-500 text-sm">
              Ничего не найдено
            </div>
          )}

          {allItems.map((item, i) => {
            const isNav = item.type === "nav";
            const icon = isNav
              ? (item as { icon: string }).icon
              : (TYPE_ICONS[item.type] ?? "📄");
            const label = isNav
              ? item.title
              : `${TYPE_LABELS[item.type] ?? item.type}: ${item.title}`;
            const sub =
              !isNav && "subtitle" in item ? item.subtitle : undefined;

            return (
              <button
                key={`${item.type}-${item.href}-${i}`}
                onClick={() => navigate(item.href)}
                className={`w-full text-left flex items-center gap-3 px-4 py-2.5 transition-colors ${
                  i === selected
                    ? "bg-blue-600/20 text-slate-100"
                    : "text-slate-300 hover:bg-slate-800"
                }`}
              >
                <span className="text-base w-5 text-center shrink-0">
                  {icon}
                </span>
                <div className="flex-1 min-w-0">
                  <div className="text-sm truncate">{label}</div>
                  {sub && (
                    <div className="text-[10px] text-slate-500 truncate">
                      {sub}
                    </div>
                  )}
                </div>
                {!isNav && (
                  <span className="text-[10px] px-1.5 py-0.5 bg-slate-700 text-slate-400 rounded shrink-0">
                    {TYPE_LABELS[item.type] ?? item.type}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-800 flex gap-3 text-[10px] text-slate-600">
          <span>↑↓ навигация</span>
          <span>Enter выбор</span>
          <span>Esc закрыть</span>
          <span>Ctrl+K переключить</span>
        </div>
      </div>
    </div>
  );
}
