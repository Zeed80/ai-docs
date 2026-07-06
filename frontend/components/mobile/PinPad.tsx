"use client";

import { PIN_MAX_LEN } from "@/lib/app-lock";

interface Props {
  value: string;
  onChange: (v: string) => void;
  /** Fixed number of dots to render (e.g. the known PIN length on unlock). */
  dots?: number;
  maxLength?: number;
  /** Extra tint for the dots row, e.g. red on a wrong PIN. */
  error?: boolean;
  disabled?: boolean;
}

const KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"];

/**
 * Numeric keypad for entering a PIN. Purely presentational: the parent owns the
 * value and decides when a PIN is complete (auto-submit on known length, or an
 * explicit confirm button during setup).
 */
export default function PinPad({
  value,
  onChange,
  dots,
  maxLength = PIN_MAX_LEN,
  error = false,
  disabled = false,
}: Props) {
  const total = dots ?? Math.max(value.length, 4);

  function press(d: string) {
    if (disabled) return;
    if (value.length >= maxLength) return;
    onChange(value + d);
  }
  function back() {
    if (disabled) return;
    onChange(value.slice(0, -1));
  }

  return (
    <div className="flex flex-col items-center gap-6 select-none">
      <div className="flex items-center gap-3 h-4">
        {Array.from({ length: total }).map((_, i) => (
          <span
            key={i}
            className={`h-3 w-3 rounded-full transition-colors ${
              error
                ? "bg-red-500"
                : i < value.length
                  ? "bg-sky-400"
                  : "bg-white/20"
            }`}
          />
        ))}
      </div>

      <div className="grid grid-cols-3 gap-3">
        {KEYS.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => press(k)}
            disabled={disabled}
            className="h-16 w-16 rounded-full bg-white/5 text-2xl font-medium text-slate-100 active:bg-white/20 disabled:opacity-40"
          >
            {k}
          </button>
        ))}
        <span />
        <button
          type="button"
          onClick={() => press("0")}
          disabled={disabled}
          className="h-16 w-16 rounded-full bg-white/5 text-2xl font-medium text-slate-100 active:bg-white/20 disabled:opacity-40"
        >
          0
        </button>
        <button
          type="button"
          onClick={back}
          disabled={disabled || value.length === 0}
          aria-label="Стереть"
          className="h-16 w-16 rounded-full text-slate-300 active:bg-white/10 disabled:opacity-30 flex items-center justify-center"
        >
          <svg
            className="h-6 w-6"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.8}
              d="M9 6L3 12l6 6h9a2 2 0 002-2V8a2 2 0 00-2-2H9zm3 3l4 4m0-4l-4 4"
            />
          </svg>
        </button>
      </div>
    </div>
  );
}
