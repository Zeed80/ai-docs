"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { emailApi } from "./api";

type Suggestion = {
  email: string;
  name: string | null;
  organization?: string | null;
  source?: string;
  is_favorite?: boolean;
};

/** Группа = метка адресной книги. «Написать всем закупщикам» раньше означало
 *  добавить их по одному: теги были только фильтром в разделе контактов. */
type Group = { tag: string; emails: string[] };

function initials(s: string) {
  const parts = s.replace(/[<>]/g, "").trim().split(/[\s@.]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}
function colorFor(s: string) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return `hsl(${h} 45% 40%)`;
}
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** Gmail-style multi-recipient field: chips + autocomplete dropdown. */
export function RecipientInput({
  label,
  value,
  onChange,
  autoFocus,
  names,
}: {
  label: string;
  value: string[];
  onChange: (emails: string[]) => void;
  autoFocus?: boolean;
  /** email -> display name, to render nicer chips */
  names?: Record<string, string>;
}) {
  const t = useTranslations("email");
  const [text, setText] = useState("");
  const [sug, setSug] = useState<Suggestion[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = setTimeout(() => {
      emailApi
        .contacts(text.trim(), 8)
        .then((s) => setSug(s.filter((c) => !value.includes(c.email.toLowerCase()))))
        .catch(() => setSug([]));
    }, 180);
    return () => clearTimeout(h);
  }, [text, value]);

  // Метки книги, подходящие под набранное: попадают в тот же список подсказок
  // отдельным разделом.
  useEffect(() => {
    const query = text.trim().toLowerCase();
    if (query.length < 2) {
      setGroups([]);
      return;
    }
    let cancelled = false;
    const h = setTimeout(async () => {
      try {
        const tags = await emailApi.contactTags();
        const matched = tags.filter((tg) => tg.toLowerCase().includes(query)).slice(0, 3);
        const loaded = await Promise.all(
          matched.map(async (tg) => ({
            tag: tg,
            emails: (await emailApi.contactBook({ tag: tg })).map((c) => c.email),
          })),
        );
        if (!cancelled) setGroups(loaded.filter((g) => g.emails.length > 0));
      } catch {
        if (!cancelled) setGroups([]);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(h);
    };
  }, [text]);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, []);

  function add(email: string) {
    const e = email.trim().replace(/[,;]+$/, "").toLowerCase();
    if (!e) return;
    if (!value.includes(e)) onChange([...value, e]);
    setText("");
    setSug([]);
    setActive(0);
  }
  function addGroup(g: Group) {
    const fresh = g.emails
      .map((e) => e.trim().toLowerCase())
      .filter((e) => e && !value.includes(e));
    if (fresh.length) onChange([...value, ...fresh]);
    setText("");
    setSug([]);
    setGroups([]);
    setActive(0);
  }

  function commitTyped() {
    text
      .split(/[,;\s]+/)
      .map((x) => x.trim())
      .filter(Boolean)
      .forEach(add);
  }

  function onKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (open && sug.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setActive((a) =>
        e.key === "ArrowDown"
          ? (a + 1) % sug.length
          : (a - 1 + sug.length) % sug.length,
      );
      return;
    }
    if ((e.key === "Enter" || e.key === "Tab" || e.key === "," || e.key === ";") && (text || (open && sug.length))) {
      if (e.key === ",") return; // handled by input change
      e.preventDefault();
      if (open && sug[active]) add(sug[active].email);
      else commitTyped();
      return;
    }
    if (e.key === "Backspace" && !text && value.length) {
      onChange(value.slice(0, -1));
    }
  }

  return (
    <div ref={boxRef} className="relative flex min-w-0 flex-1 flex-wrap items-center gap-1">
      <span className="w-8 shrink-0 text-xs text-slate-400 dark:text-slate-400">{label}</span>
      <div
        className="flex min-h-[30px] flex-1 flex-wrap items-center gap-1 rounded border border-slate-300 dark:border-slate-600 bg-slate-100 dark:bg-slate-700 px-1.5 py-1"
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((e) => (
          <span
            key={e}
            className={`flex items-center gap-1 rounded-full px-1.5 py-0.5 text-xs ${
              EMAIL_RE.test(e) ? "bg-slate-600 text-slate-900 dark:text-slate-100" : "bg-red-900/50 text-red-200"
            }`}
            title={e}
          >
            <span
              className="flex h-4 w-4 items-center justify-center rounded-full text-[8px] font-bold text-white"
              style={{ background: colorFor(e) }}
            >
              {initials(names?.[e] || e)}
            </span>
            <span className="max-w-[180px] truncate">{names?.[e] || e}</span>
            <button
              onClick={(ev) => {
                ev.stopPropagation();
                onChange(value.filter((x) => x !== e));
              }}
              className="text-slate-400 dark:text-slate-400 hover:text-red-600 dark:hover:text-red-300"
            >
              ×
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          autoFocus={autoFocus}
          value={text}
          onChange={(e) => {
            const v = e.target.value;
            if (v.endsWith(",") || v.endsWith(";")) {
              setText(v.slice(0, -1));
              commitTyped();
            } else setText(v);
            setOpen(true);
            setActive(0);
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={onKey}
          onBlur={() => text && !sug.length && commitTyped()}
          placeholder={value.length ? "" : t("recipientPlaceholder")}
          className="min-w-[120px] flex-1 bg-transparent text-sm text-slate-900 dark:text-slate-100 placeholder-slate-400 dark:placeholder-slate-500 focus:outline-none"
        />
      </div>

      {open && (sug.length > 0 || groups.length > 0) && (
        <div className="absolute left-8 top-full z-30 mt-1 max-h-64 w-[min(420px,90vw)] overflow-auto rounded-lg border border-slate-300 dark:border-slate-600 bg-white dark:bg-slate-800 py-1 shadow-xl">
          {groups.map((g) => (
            <button
              key={`group-${g.tag}`}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                addGroup(g);
              }}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-slate-100 dark:hover:bg-slate-700/50"
            >
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-500 text-[10px] font-bold text-white">
                ☰
              </span>
              <span className="min-w-0 flex-1">
                <span className="truncate text-sm text-slate-800 dark:text-slate-100">{g.tag}</span>
                <span className="block truncate text-xs text-slate-400">
                  {g.emails.length}
                </span>
              </span>
            </button>
          ))}
          {sug.map((c, i) => (
            <button
              key={c.email}
              onMouseEnter={() => setActive(i)}
              onMouseDown={(e) => {
                e.preventDefault();
                add(c.email);
              }}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left ${
                i === active ? "bg-blue-100 dark:bg-blue-900/40" : "hover:bg-slate-700/50"
              }`}
            >
              <span
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[10px] font-bold text-white"
                style={{ background: colorFor(c.email) }}
              >
                {initials(c.name || c.email)}
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-1.5">
                  <span className="truncate text-sm text-slate-900 dark:text-slate-100">
                    {c.name || c.email}
                  </span>
                  {c.is_favorite && <span className="text-amber-600 dark:text-amber-400 text-xs">★</span>}
                </span>
                <span className="block truncate text-xs text-slate-400 dark:text-slate-400">
                  {c.name ? c.email : c.organization || ""}
                  {c.organization && c.name ? ` · ${c.organization}` : ""}
                </span>
              </span>
              <span className="shrink-0 text-[9px] uppercase text-slate-400 dark:text-slate-400">
                {c.source === "book"
                  ? t("source.book")
                  : c.source === "party"
                    ? t("source.party")
                    : t("source.recent")}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
