"use client";

import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { searchMentions, type Mention } from "@/lib/api";

/** Ссылки @ в чате: «@счёт:EV-4901» кладёт в ход точную запись, и агент не
 *  ищет её по номеру, рискуя взять соседнюю (идея hermes-agent). */
const TYPE_WORD: Record<Mention["type"], string> = {
  invoice: "счёт",
  document: "документ",
  supplier: "поставщик",
};

export function mentionToken(m: Mention): string {
  return `@${TYPE_WORD[m.type]}:${m.label}`;
}

const QUERY_AT_CARET = /(?:^|\s)@([^\s@]{0,40})$/;

export function useMentions(
  input: string,
  setInput: (value: string) => void,
  inputRef: RefObject<HTMLInputElement | null>,
) {
  const [query, setQuery] = useState<string | null>(null);
  const [items, setItems] = useState<Mention[]>([]);
  const [active, setActive] = useState(0);
  const [picked, setPicked] = useState<Mention[]>([]);
  const requestRef = useRef(0);

  const onInputChange = useCallback(
    (value: string, caret: number | null) => {
      setInput(value);
      const before = value.slice(0, caret ?? value.length);
      const match = QUERY_AT_CARET.exec(before);
      setQuery(match ? match[1] : null);
      setActive(0);
    },
    [setInput],
  );

  useEffect(() => {
    if (query === null) {
      setItems([]);
      return;
    }
    const request = ++requestRef.current;
    const timer = setTimeout(() => {
      searchMentions(query)
        .then((found) => {
          if (request === requestRef.current) setItems(found.slice(0, 12));
        })
        .catch(() => {
          if (request === requestRef.current) setItems([]);
        });
    }, 200);
    return () => clearTimeout(timer);
  }, [query]);

  const choose = useCallback(
    (m: Mention) => {
      const el = inputRef.current;
      const caret = el?.selectionStart ?? input.length;
      const before = input.slice(0, caret).replace(/@([^\s@]{0,40})$/, `${mentionToken(m)} `);
      const next = before + input.slice(caret);
      setInput(next);
      setPicked((prev) => (prev.some((p) => p.id === m.id) ? prev : [...prev, m]));
      setQuery(null);
      setItems([]);
      requestAnimationFrame(() => {
        el?.focus();
        el?.setSelectionRange(before.length, before.length);
      });
    },
    [input, inputRef, setInput],
  );

  /** true — клавишу обработал список подсказок. */
  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLInputElement>) => {
      if (query === null || items.length === 0) return false;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const step = e.key === "ArrowDown" ? 1 : -1;
        setActive((i) => (i + step + items.length) % items.length);
        return true;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        choose(items[active]);
        return true;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setQuery(null);
        return true;
      }
      return false;
    },
    [active, choose, items, query],
  );

  /** Ссылки, чей текст ещё есть в сообщении; после отправки список чистится. */
  const takeReferences = useCallback(
    (content: string) => {
      const refs = picked
        .filter((m) => content.includes(mentionToken(m)))
        .map((m) => ({ type: m.type, id: m.id, label: m.label }));
      setPicked([]);
      return refs;
    },
    [picked],
  );

  const popup =
    query !== null && items.length > 0 ? (
      <ul
        role="listbox"
        aria-label="Ссылка на запись"
        className="absolute bottom-full left-0 z-20 mb-1 max-h-60 w-full min-w-0 overflow-y-auto rounded-lg border border-slate-600 bg-slate-800 py-1 shadow-lg"
      >
        {items.map((m, i) => (
          <li
            key={`${m.type}:${m.id}`}
            role="option"
            aria-selected={i === active}
            onMouseDown={(e) => {
              e.preventDefault();
              choose(m);
            }}
            className={`flex min-w-0 cursor-pointer items-baseline gap-2 px-3 py-1.5 text-xs ${
              i === active ? "bg-blue-600/30 text-blue-100" : "text-slate-200 hover:bg-slate-700"
            }`}
          >
            <span className="shrink-0 text-slate-400">{TYPE_WORD[m.type]}</span>
            <span className="min-w-0 truncate">{m.label}</span>
            {m.hint && <span className="ml-auto shrink-0 truncate text-slate-500">{m.hint}</span>}
          </li>
        ))}
      </ul>
    ) : null;

  return { onInputChange, onKeyDown, takeReferences, popup };
}
