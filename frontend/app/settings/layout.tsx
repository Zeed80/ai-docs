"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import clsx from "clsx";
import { useHasRole } from "@/lib/rbac";

const TABS = [
  { href: "/settings", label: "Интерфейс", exact: true },
  { href: "/settings/normalization", label: "Нормализация" },
  { href: "/settings/norm-cards", label: "Карты норм" },
  { href: "/settings/ntd", label: "НТД" },
  { href: "/settings/notifications", label: "Уведомления" },
] as const;

function TabNav() {
  const pathname = usePathname();
  const isAdmin = useHasRole("admin");

  return (
    <nav className="flex flex-wrap gap-0 border-b border-border mb-6">
      {TABS.map((tab) => {
        const active =
          "exact" in tab && tab.exact
            ? pathname === tab.href
            : pathname === tab.href || pathname.startsWith(tab.href + "/");
        return (
          <Link
            key={tab.href}
            href={tab.href}
            className={clsx(
              "px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap",
              active
                ? "border-primary text-primary"
                : "border-transparent text-muted-foreground hover:text-foreground",
            )}
          >
            {tab.label}
          </Link>
        );
      })}
      {isAdmin && (
        <Link
          href="/admin"
          className="ml-auto px-4 py-2 text-sm font-medium border-b-2 border-transparent text-muted-foreground hover:text-foreground transition-colors whitespace-nowrap"
        >
          Администрирование →
        </Link>
      )}
    </nav>
  );
}

export default function SettingsLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-4">
        <h1 className="text-xl font-bold">Настройки</h1>
        <p className="text-sm text-muted-foreground">
          Конфигурация системы и управление пользователями
        </p>
      </div>
      <TabNav />
      {children}
    </div>
  );
}
