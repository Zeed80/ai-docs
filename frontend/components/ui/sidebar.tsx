"use client";

import { getApiBaseUrl } from "@/lib/api-base";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import clsx from "clsx";
import { fetchMe, logout, type UserInfo } from "@/lib/auth";

const API = getApiBaseUrl();

function useFeedCount() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    function load() {
      fetch(`${API}/api/dashboard/feed`)
        .then((r) => r.json())
        .then((d) => setCount(d.total ?? 0))
        .catch(() => {});
    }
    load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, []);
  return count;
}

function useQuarantineCount() {
  const [count, setCount] = useState(0);
  useEffect(() => {
    function load() {
      fetch(`${API}/api/quarantine/count`)
        .then((r) => r.json())
        .then((d) => setCount(d.count ?? 0))
        .catch(() => {});
    }
    load();
    const t = setInterval(load, 60_000);
    return () => clearInterval(t);
  }, []);
  return count;
}

function useCurrentUser() {
  const [user, setUser] = useState<UserInfo | null>(null);
  useEffect(() => {
    fetchMe().then((u) => setUser(u));
  }, []);
  return user;
}

// ── Nav structure ─────────────────────────────────────────────────────────────

const NAV_PRIMARY = [
  { href: "/", icon: "home", key: "feed", exact: true },
] as const;

const NAV_DOCS = [
  { href: "/inbox", icon: "inbox", key: "inbox" },
  { href: "/documents", icon: "file-text", key: "documents" },
  { href: "/invoices", icon: "receipt", key: "invoices" },
  { href: "/email", icon: "mail", key: "email" },
] as const;

const NAV_REF = [
  { href: "/boms", icon: "list", key: "boms" },
  { href: "/anomalies", icon: "alert-triangle", key: "anomalies" },
  { href: "/settings/ntd", icon: "file-text", key: "ntd" },
  { href: "/settings/norm-cards", icon: "sliders", key: "normalization" },
] as const;

const NAV_ENGINEERING = [
  { href: "/drawings", icon: "drafting-compass", key: "drawings" },
  { href: "/catalogs", icon: "tool-catalog", key: "catalogs" },
] as const;

const NAV_WAREHOUSE = [
  { href: "/warehouse", icon: "box", key: "warehouse" },
] as const;

const NAV_PROCUREMENT = [
  { href: "/procurement", icon: "shopping-cart", key: "procurement" },
  { href: "/suppliers", icon: "users", key: "suppliers" },
] as const;

const NAV_FINANCE = [
  { href: "/payments", icon: "credit-card", key: "payments" },
  { href: "/calendar", icon: "calendar", key: "calendar" },
  { href: "/approvals", icon: "check-circle", key: "approvals" },
] as const;

const NAV_SYSTEM = [
  { href: "/quarantine", icon: "shield", key: "quarantine" },
  { href: "/settings", icon: "settings", key: "settings" },
] as const;

// ── Icons ─────────────────────────────────────────────────────────────────────

function Icon({ name }: { name: string }) {
  const map: Record<string, React.ReactNode> = {
    home: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6"
        />
      </svg>
    ),
    inbox: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-2.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4"
        />
      </svg>
    ),
    "file-text": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
        />
      </svg>
    ),
    receipt: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"
        />
      </svg>
    ),
    mail: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"
        />
      </svg>
    ),
    users: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"
        />
      </svg>
    ),
    "alert-triangle": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
        />
      </svg>
    ),
    calendar: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
        />
      </svg>
    ),
    shield: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z"
        />
      </svg>
    ),
    settings: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
        />
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
        />
      </svg>
    ),
    box: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"
        />
      </svg>
    ),
    "shopping-cart": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z"
        />
      </svg>
    ),
    "credit-card": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M3 10h18M7 15h1m4 0h1m-7 4h12a3 3 0 003-3V8a3 3 0 00-3-3H6a3 3 0 00-3 3v8a3 3 0 003 3z"
        />
      </svg>
    ),
    "check-circle": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
        />
      </svg>
    ),
    sliders: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"
        />
      </svg>
    ),
    list: (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01"
        />
      </svg>
    ),
    "drafting-compass": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M9 3H5a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2V5a2 2 0 00-2-2h-4M9 3V1m0 2v2m6-2V1m0 2v2M9 7h6M9 11h6M9 15h4"
        />
      </svg>
    ),
    "tool-catalog": (
      <svg
        className="w-4 h-4"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M11 4a2 2 0 114 0v1a1 1 0 001 1h3a1 1 0 011 1v3a1 1 0 01-1 1h-1a2 2 0 100 4h1a1 1 0 011 1v3a1 1 0 01-1 1h-3a1 1 0 01-1-1v-1a2 2 0 10-4 0v1a1 1 0 01-1 1H7a1 1 0 01-1-1v-3a1 1 0 00-1-1H4a2 2 0 110-4h1a1 1 0 001-1V7a1 1 0 011-1h3a1 1 0 001-1V4z"
        />
      </svg>
    ),
  };
  return <>{map[name] ?? null}</>;
}

// ── NavItem ───────────────────────────────────────────────────────────────────

function NavItem({
  href,
  icon,
  label,
  badge,
  exact = false,
}: {
  href: string;
  icon: string;
  label: string;
  badge?: number | null;
  exact?: boolean;
}) {
  const pathname = usePathname();
  const isActive = exact ? pathname === href : pathname.startsWith(href);
  return (
    <Link
      href={href}
      className={clsx(
        "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
        isActive
          ? "bg-slate-700 text-white"
          : "text-slate-400 hover:bg-slate-700/50 hover:text-slate-200",
      )}
    >
      <Icon name={icon} />
      <span className="flex-1 text-xs">{label}</span>
      {badge != null && badge > 0 && (
        <span className="text-[10px] font-bold bg-red-500 text-white rounded-full px-1.5 py-0.5 leading-none">
          {badge}
        </span>
      )}
    </Link>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────────────────

export function Sidebar() {
  const t = useTranslations("nav");
  const router = useRouter();
  const feedCount = useFeedCount();
  const quarantineCount = useQuarantineCount();
  const user = useCurrentUser();

  async function handleLogout() {
    await logout();
    router.push("/auth/login");
  }

  return (
    <aside className="w-full h-full bg-slate-800 border-r border-slate-700 flex flex-col overflow-hidden">
      {/* Logo */}
      <div className="px-4 py-3 border-b border-slate-700">
        <h1 className="text-sm font-bold text-slate-100 tracking-tight">
          AI Docs
        </h1>
        <p className="text-[10px] text-slate-500 mt-0.5">
          Света · рабочее место
        </p>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-2 space-y-4 overflow-y-auto">
        {/* Primary */}
        <div>
          {NAV_PRIMARY.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
              badge={feedCount || null}
              exact={item.exact}
            />
          ))}
        </div>

        {/* Документооборот */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Документы
          </p>
          {NAV_DOCS.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
            />
          ))}
        </div>

        {/* Производство */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Производство
          </p>
          {NAV_ENGINEERING.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
            />
          ))}
        </div>

        {/* Склад */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Склад
          </p>
          {NAV_WAREHOUSE.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
            />
          ))}
        </div>

        {/* Закупки */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Закупки
          </p>
          {NAV_PROCUREMENT.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
            />
          ))}
        </div>

        {/* Финансы */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Финансы
          </p>
          {NAV_FINANCE.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
            />
          ))}
        </div>

        {/* Данные */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Данные
          </p>
          {NAV_REF.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
            />
          ))}
        </div>

        {/* Система */}
        <div>
          <p className="px-3 mb-1 text-[9px] font-semibold uppercase tracking-wider text-slate-600">
            Система
          </p>
          {NAV_SYSTEM.map((item) => (
            <NavItem
              key={item.key}
              href={item.href}
              icon={item.icon}
              label={t(item.key)}
              badge={item.key === "quarantine" ? quarantineCount || null : null}
            />
          ))}
        </div>
      </nav>

      {/* User panel */}
      <div className="p-2 border-t border-slate-700 flex items-center gap-2 min-w-0">
        <div className="w-6 h-6 rounded-full bg-blue-600 flex items-center justify-center text-[10px] font-bold text-white shrink-0">
          {user
            ? (user.name[0] ?? user.preferred_username[0] ?? "?").toUpperCase()
            : "…"}
        </div>
        <div className="flex-1 min-w-0">
          <p className="text-xs font-medium text-slate-300 truncate leading-tight">
            {user?.name ?? "…"}
          </p>
        </div>
        <button
          onClick={handleLogout}
          title="Выйти"
          className="p-1 rounded hover:bg-slate-700 text-slate-500 hover:text-slate-300 transition-colors shrink-0"
        >
          <svg
            className="w-3.5 h-3.5"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"
            />
          </svg>
        </button>
      </div>
    </aside>
  );
}
