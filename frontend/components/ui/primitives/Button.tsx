"use client";

import type { ButtonHTMLAttributes, ReactNode } from "react";
import { btn, focusRing } from "./tokens";

type Variant = "primary" | "secondary" | "danger" | "ghost";

const VARIANT: Record<Variant, string> = {
  primary: "bg-blue-600 hover:bg-blue-700 text-white",
  secondary: "bg-slate-700 hover:bg-slate-600 text-slate-200",
  danger: "bg-red-700 hover:bg-red-600 text-white",
  ghost: "bg-transparent hover:bg-slate-800 text-slate-300",
};

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  /** Показывает спиннер и блокирует кнопку — без отдельного disabled у вызывающего. */
  loading?: boolean;
  children?: ReactNode;
}

export function Button({
  variant = "secondary",
  loading = false,
  disabled,
  className = "",
  children,
  ...rest
}: Props) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`${btn} ${VARIANT[variant]} ${focusRing} disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5 ${className}`}
      {...rest}
    >
      {loading && (
        <span
          aria-hidden="true"
          className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {children}
    </button>
  );
}
