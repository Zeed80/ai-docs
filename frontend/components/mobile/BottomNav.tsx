"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import clsx from "clsx";
import { scanDocument } from "@/lib/native-bridge";
import { documents } from "@/lib/api-client";

type Tab = { href: string; label: string; icon: React.ReactNode };

function I({ d }: { d: string }) {
  return (
    <svg
      className="w-6 h-6"
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={1.8}
        d={d}
      />
    </svg>
  );
}

const LEFT: Tab[] = [
  {
    href: "/inbox",
    label: "Входящие",
    icon: <I d="M4 4h16v10h-5l-3 3-3-3H4z" />,
  },
  {
    href: "/documents",
    label: "Документы",
    icon: <I d="M7 3h7l5 5v13H7z M14 3v5h5" />,
  },
];

const RIGHT: Tab[] = [
  { href: "/approvals", label: "Согласования", icon: <I d="M5 13l4 4L19 7" /> },
  { href: "/chat", label: "Света", icon: <I d="M4 5h16v11H8l-4 4z" /> },
];

function NavLink({ tab, active }: { tab: Tab; active: boolean }) {
  return (
    <Link
      href={tab.href}
      className={clsx(
        "flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[10px]",
        active ? "text-sky-400" : "text-slate-400",
      )}
    >
      {tab.icon}
      <span>{tab.label}</span>
    </Link>
  );
}

/**
 * Bottom tab bar for the mobile shell. The center button captures a document
 * with the native camera/scanner and ingests it, then opens the new document.
 */
export function BottomNav() {
  const pathname = usePathname();
  const router = useRouter();
  const [busy, setBusy] = useState(false);

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  async function captureAndIngest() {
    if (busy) return;
    setBusy(true);
    try {
      const files = await scanDocument();
      if (!files.length) return;
      let firstId: string | undefined;
      for (const f of files) {
        const res = await documents.ingest(f, "mobile_camera");
        firstId = firstId ?? (res?.id as string | undefined);
      }
      if (firstId) router.push(`/documents/${firstId}`);
      else router.push("/documents");
    } catch (e) {
      console.error("capture/ingest failed", e);
      alert("Не удалось загрузить документ");
    } finally {
      setBusy(false);
    }
  }

  return (
    <nav
      className="fixed inset-x-0 bottom-0 z-40 flex items-stretch border-t border-slate-700 bg-slate-900/95 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      {LEFT.map((t) => (
        <NavLink key={t.href} tab={t} active={isActive(t.href)} />
      ))}

      <div className="relative flex w-16 shrink-0 items-start justify-center">
        <button
          type="button"
          onClick={captureAndIngest}
          disabled={busy}
          aria-label="Снять документ"
          className="-mt-5 flex h-14 w-14 items-center justify-center rounded-full bg-sky-500 text-white shadow-lg shadow-sky-900/40 disabled:opacity-60"
        >
          {busy ? (
            <svg
              className="h-6 w-6 animate-spin"
              viewBox="0 0 24 24"
              fill="none"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4z"
              />
            </svg>
          ) : (
            <svg
              className="h-7 w-7"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.8}
                d="M4 7h3l2-2h6l2 2h3v12H4z"
              />
              <circle cx="12" cy="13" r="3.5" strokeWidth={1.8} />
            </svg>
          )}
        </button>
      </div>

      {RIGHT.map((t) => (
        <NavLink key={t.href} tab={t} active={isActive(t.href)} />
      ))}
    </nav>
  );
}
