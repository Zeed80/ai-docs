"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import clsx from "clsx";
import { useHasRole } from "@/lib/rbac";

const ADMIN_TABS = [
  { href: "/admin", label: "Обзор", exact: true },
  { href: "/admin/users", label: "Пользователи" },
  { href: "/admin/permissions", label: "Права доступа" },
  { href: "/admin/audit", label: "Журнал аудита" },
  { href: "/admin/api-keys", label: "API-ключи" },
  { href: "/admin/system", label: "Система" },
] as const;

function AdminNav() {
  const pathname = usePathname();
  return (
    <nav className="flex flex-wrap gap-0 border-b border-border mb-6">
      {ADMIN_TABS.map((tab) => {
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
    </nav>
  );
}

export default function AdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const isAdmin = useHasRole("admin");
  const router = useRouter();

  useEffect(() => {
    if (isAdmin === false) {
      router.replace("/");
    }
  }, [isAdmin, router]);

  if (isAdmin === false) return null;

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-4">
        <h1 className="text-xl font-bold">Администрирование</h1>
        <p className="text-sm text-muted-foreground">
          Управление пользователями, правами и системой
        </p>
      </div>
      <AdminNav />
      {children}
    </div>
  );
}
