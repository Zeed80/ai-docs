"use client";

import { useEffect, useRef, useState } from "react";

/** A value the workspace is currently asking the user for (replaces every
 * window.prompt the editor used to fire): the command line shows the
 * question, Enter resolves it, Escape cancels. */
export interface CommandPrompt {
  message: string;
  defaultValue?: string;
}

export default function CommandLine({
  prompt,
  hint,
  onSubmit,
  onCancel,
}: {
  /** Pending value request; null = free command mode. */
  prompt: CommandPrompt | null;
  /** Idle placeholder, e.g. current tool + expected input. */
  hint: string;
  onSubmit: (text: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState("");
  const [history, setHistory] = useState<string[]>([]);
  const [, setHistoryPos] = useState(-1);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // A fresh prompt pre-fills its default and grabs focus — mid-command value
  // entry (radius, dimension text) should not require reaching for the mouse.
  useEffect(() => {
    if (prompt) {
      setValue(prompt.defaultValue ?? "");
      inputRef.current?.focus();
    }
  }, [prompt]);

  return (
    <div className="flex items-center gap-2 rounded border border-white/10 bg-zinc-900/80 px-2 py-1.5 font-mono text-xs">
      <span className={prompt ? "text-amber-300" : "text-zinc-500"}>
        {prompt ? prompt.message : "›"}
      </span>
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={prompt ? "" : hint}
        spellCheck={false}
        autoComplete="off"
        className="min-w-0 flex-1 bg-transparent text-zinc-100 outline-none placeholder:text-zinc-600"
        onKeyDown={(ev) => {
          // The workspace's global shortcuts must not fire while typing here;
          // stopPropagation keeps single-letter tool hotkeys usable elsewhere.
          ev.stopPropagation();
          if (ev.key === "Enter") {
            const text = value.trim();
            onSubmit(text);
            if (!prompt && text) {
              setHistory((h) =>
                h[h.length - 1] === text ? h : [...h, text].slice(-50),
              );
            }
            setHistoryPos(-1);
            setValue("");
            return;
          }
          if (ev.key === "Escape") {
            setValue("");
            setHistoryPos(-1);
            onCancel();
            inputRef.current?.blur();
            return;
          }
          if (!prompt && ev.key === "ArrowUp") {
            ev.preventDefault();
            setHistoryPos((pos) => {
              const next =
                pos === -1 ? history.length - 1 : Math.max(0, pos - 1);
              if (history[next] != null) setValue(history[next]);
              return next;
            });
            return;
          }
          if (!prompt && ev.key === "ArrowDown") {
            ev.preventDefault();
            setHistoryPos((pos) => {
              if (pos === -1) return -1;
              const next = pos + 1;
              if (next >= history.length) {
                setValue("");
                return -1;
              }
              setValue(history[next]);
              return next;
            });
          }
        }}
      />
    </div>
  );
}
