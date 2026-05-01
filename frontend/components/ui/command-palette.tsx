"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = getApiBaseUrl();

type Mode = "nav" | "search" | "nl";

interface NavItem {
  label: string;
  href: string;
  shortcut?: string;
}

const NAV_ITEMS: NavItem[] = [
  { label: "Входящие", href: "/inbox", shortcut: "g i" },
  { label: "Документы", href: "/documents", shortcut: "g d" },
  { label: "Счета", href: "/invoices", shortcut: "g v" },
  { label: "Согласования", href: "/approvals", shortcut: "g a" },
  { label: "Настройки", href: "/settings" },
  { label: "Правила нормализации", href: "/settings/normalization" },
];

interface SearchResult {
  id: string;
  file_name: string;
  status: string;
  doc_type: string | null;
}

interface NLResult {
  interpretation: string;
  results: SearchResult[];
  total: number;
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("nav");
  const [selected, setSelected] = useState(0);
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [nlResult, setNLResult] = useState<NLResult | null>(null);
  const [loading, setLoading] = useState(false);

  // Focus input on open
  useEffect(() => {
    if (open) {
      setQuery("");
      setSelected(0);
      setMode("nav");
      setSearchResults([]);
      setNLResult(null);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  // Detect mode from input
  useEffect(() => {
    if (query.startsWith("/")) {
      setMode("nl");
    } else if (query.length > 0) {
      setMode("search");
    } else {
      setMode("nav");
    }
    setSelected(0);
  }, [query]);

  // Debounced hybrid search (vector + text fallback)
  useEffect(() => {
    if (mode === "search" && query.length >= 2) {
      const timer = setTimeout(async () => {
        setLoading(true);
        try {
          const res = await fetch(`${API_BASE}/api/search/hybrid`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query, limit: 10 }),
          });
          if (res.ok) {
            const data = await res.json();
            // hybrid returns {results: [...]} or plain array
            const items = Array.isArray(data) ? data : (data.results ?? []);
            setSearchResults(items);
          }
        } catch {
          // ignore
        } finally {
          setLoading(false);
        }
      }, 400);
      return () => clearTimeout(timer);
    }
    if (mode === "nav") {
      setSearchResults([]);
    }
  }, [query, mode]);

  // NL query on Enter
  const runNLQuery = useCallback(async () => {
    const nlQuery = query.startsWith("/") ? query.slice(1).trim() : query;
    if (!nlQuery) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/search/nl`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: nlQuery, limit: 10 }),
      });
      if (res.ok) {
        const data = await res.json();
        setNLResult(data);
      }
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [query]);

  // Filtered nav items
  const filteredNav = NAV_ITEMS.filter(
    (item) =>
      query === "" || item.label.toLowerCase().includes(query.toLowerCase()),
  );

  // Current items for navigation
  const currentItems =
    mode === "nav"
      ? filteredNav
      : mode === "search"
        ? searchResults
        : (nlResult?.results ?? []);

  function handleSelect(index: number) {
    if (mode === "nav") {
      const item = filteredNav[index];
      if (item) {
        router.push(item.href);
        onClose();
      }
    } else {
      const item = currentItems[index] as SearchResult;
      if (item?.id) {
        router.push(`/documents/${item.id}/review`);
        onClose();
      }
    }
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setSelected((s) => Math.min(s + 1, currentItems.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setSelected((s) => Math.max(s - 1, 0));
        break;
      case "Enter":
        e.preventDefault();
        if (mode === "nl" && !nlResult) {
          runNLQuery();
        } else {
          handleSelect(selected);
        }
        break;
      case "Escape":
        onClose();
        break;
      case "Tab":
        e.preventDefault();
        // Cycle modes
        setMode((m) =>
          m === "nav" ? "search" : m === "search" ? "nl" : "nav",
        );
        break;
    }
  }

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 bg-black/40 flex items-start justify-center pt-[15vh] z-50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-2xl w-full max-w-lg overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Input */}
        <div className="flex items-center border-b border-slate-200 px-4">
          <span className="text-slate-400 text-sm mr-2">
            {mode === "nl" ? "/" : mode === "search" ? ">" : "#"}
          </span>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              mode === "nl"
                ? "Спросите на естественном языке..."
                : mode === "search"
                  ? "Поиск документов..."
                  : "Перейти к..."
            }
            className="flex-1 py-3 text-sm outline-none bg-transparent"
          />
          {loading && (
            <span className="text-xs text-slate-400 animate-pulse">...</span>
          )}
        </div>

        {/* Mode tabs */}
        <div className="flex gap-1 px-4 pt-2 pb-1">
          {(["nav", "search", "nl"] as Mode[]).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-2 py-0.5 text-xs rounded ${
                mode === m
                  ? "bg-slate-200 text-slate-800"
                  : "text-slate-400 hover:text-slate-600"
              }`}
            >
              {m === "nav"
                ? "Навигация"
                : m === "search"
                  ? "Поиск"
                  : "AI запрос"}
            </button>
          ))}
          <span className="flex-1" />
          <span className="text-[10px] text-slate-400 self-center">
            Tab — сменить режим
          </span>
        </div>

        {/* NL interpretation */}
        {mode === "nl" && nlResult && (
          <div className="px-4 py-1.5 bg-blue-50 text-xs text-blue-700">
            {nlResult.interpretation} ({nlResult.total} результатов)
          </div>
        )}

        {/* Results */}
        <div className="max-h-64 overflow-auto">
          {mode === "nav" &&
            filteredNav.map((item, i) => (
              <button
                key={item.href}
                onClick={() => handleSelect(i)}
                className={`w-full flex items-center justify-between px-4 py-2 text-sm ${
                  i === selected
                    ? "bg-blue-50 text-blue-700"
                    : "text-slate-700 hover:bg-slate-50"
                }`}
              >
                <span>{item.label}</span>
                {item.shortcut && (
                  <kbd className="text-[10px] px-1.5 py-0.5 bg-slate-100 rounded border text-slate-400">
                    {item.shortcut}
                  </kbd>
                )}
              </button>
            ))}

          {(mode === "search" || (mode === "nl" && nlResult)) &&
            (currentItems as SearchResult[]).map((item, i) => (
              <button
                key={item.id}
                onClick={() => handleSelect(i)}
                className={`w-full flex items-center justify-between px-4 py-2 text-sm ${
                  i === selected
                    ? "bg-blue-50 text-blue-700"
                    : "text-slate-700 hover:bg-slate-50"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="font-medium">{item.file_name}</span>
                  {item.doc_type && (
                    <span className="text-[10px] px-1 bg-slate-100 rounded">
                      {item.doc_type}
                    </span>
                  )}
                </div>
                <span
                  className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                    item.status === "needs_review"
                      ? "bg-amber-100 text-amber-700"
                      : item.status === "approved"
                        ? "bg-green-100 text-green-700"
                        : "bg-slate-100 text-slate-600"
                  }`}
                >
                  {item.status}
                </span>
              </button>
            ))}

          {mode === "search" &&
            query.length >= 2 &&
            searchResults.length === 0 &&
            !loading && (
              <div className="px-4 py-6 text-sm text-slate-400 text-center">
                Ничего не найдено
              </div>
            )}

          {mode === "nl" && !nlResult && query.length > 1 && (
            <div className="px-4 py-6 text-sm text-slate-400 text-center">
              Нажмите Enter для AI-поиска
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-slate-100 flex gap-3 text-[10px] text-slate-400">
          <span>
            <kbd className="px-1 border rounded">↑↓</kbd> навигация
          </span>
          <span>
            <kbd className="px-1 border rounded">Enter</kbd> выбрать
          </span>
          <span>
            <kbd className="px-1 border rounded">Esc</kbd> закрыть
          </span>
          <span>
            <kbd className="px-1 border rounded">/</kbd> AI-запрос
          </span>
        </div>
      </div>
    </div>
  );
}
