"use client";

import { useEffect } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useCurrentUser } from "@/lib/auth-context";
import { canUseSection, pathToSectionKey } from "@/lib/nav-catalog";

/**
 * Redirects the current user away from a section they have not been granted.
 *
 * Sits inside the authenticated layout and complements the sidebar filtering:
 * hiding a nav item isn't enough — a user could still type the URL. Routes not
 * tied to any section (auth pages, utility routes) and admins pass through.
 * Auth/loading states are handled by AuthProvider, so we only act on a resolved
 * user object.
 */
export function SectionGuard({ children }: { children: React.ReactNode }) {
  const user = useCurrentUser();
  const pathname = usePathname();
  const router = useRouter();

  const key = pathToSectionKey(pathname);
  const blocked =
    !!user && !!key && !canUseSection(user.sections, user.roles, key);

  useEffect(() => {
    if (blocked) router.replace("/");
  }, [blocked, router]);

  if (blocked) {
    return (
      <div className="flex flex-col items-center justify-center h-full gap-2 p-8 text-center">
        <p className="text-sm font-medium text-slate-300">Раздел недоступен</p>
        <p className="text-xs text-slate-400">
          У вас нет доступа к этому разделу. Обратитесь к администратору.
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
