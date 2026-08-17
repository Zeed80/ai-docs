// Shared workspace navigation catalog — the single frontend source of truth for
// the sidebar structure AND per-user section access (sidebar filtering + route
// guard). Item `key`s are stable section identifiers and MUST match the backend
// catalog in `backend/app/domain/sections.py`. Never rename an existing key.

export interface NavItem {
  key: string;
  href: string;
  icon: string;
  exact?: boolean;
  adminOnly?: boolean;
}

export interface NavGroup {
  key: string;
  /** Section-group heading; empty for the primary group (no heading). */
  title: string;
  items: NavItem[];
}

// Always visible to every authenticated user; not part of the assignable grant.
export const BASE_SECTION_KEYS = ["feed", "work_orders"];

export const NAV_GROUPS: NavGroup[] = [
  {
    key: "primary",
    title: "",
    items: [{ key: "feed", href: "/", icon: "home", exact: true }],
  },
  {
    key: "docs",
    title: "Документы",
    items: [
      { key: "inbox", href: "/inbox", icon: "inbox" },
      { key: "documents", href: "/documents", icon: "file-text" },
      { key: "invoices", href: "/invoices", icon: "receipt" },
      { key: "handovers", href: "/handovers", icon: "arrow-right-circle" },
      { key: "email", href: "/email", icon: "mail" },
    ],
  },
  {
    key: "engineering",
    title: "Производство",
    items: [
      { key: "cad", href: "/cad", icon: "drafting-compass" },
      { key: "engineering", href: "/engineering", icon: "drafting-compass" },
      { key: "drawings", href: "/drawings", icon: "drafting-compass" },
      { key: "studio", href: "/studio", icon: "image-studio" },
      { key: "technology", href: "/technology", icon: "cpu" },
      { key: "catalogs", href: "/catalogs", icon: "tool-catalog" },
    ],
  },
  {
    key: "warehouse",
    title: "Склад",
    items: [{ key: "warehouse", href: "/warehouse", icon: "box" }],
  },
  {
    key: "procurement",
    title: "Закупки",
    items: [
      { key: "procurement", href: "/procurement", icon: "shopping-cart" },
      { key: "suppliers", href: "/suppliers", icon: "users" },
      { key: "compare", href: "/compare", icon: "scale" },
      { key: "cases", href: "/cases", icon: "folder" },
    ],
  },
  {
    key: "finance",
    title: "Финансы",
    items: [
      { key: "payments", href: "/payments", icon: "credit-card" },
      { key: "calendar", href: "/calendar", icon: "calendar" },
      { key: "approvals", href: "/approvals", icon: "check-circle" },
    ],
  },
  {
    key: "data",
    title: "Данные",
    items: [
      { key: "boms", href: "/boms", icon: "list" },
      { key: "anomalies", href: "/anomalies", icon: "alert-triangle" },
      { key: "canonical", href: "/canonical", icon: "tag" },
      { key: "search", href: "/search", icon: "search" },
      { key: "ntd", href: "/settings/ntd", icon: "file-text" },
      { key: "normalization", href: "/settings/norm-cards", icon: "sliders" },
    ],
  },
  {
    key: "system",
    title: "Система",
    items: [
      { key: "work_orders", href: "/work-orders", icon: "cpu" },
      { key: "quarantine", href: "/quarantine", icon: "shield" },
      { key: "settings", href: "/settings", icon: "settings" },
      { key: "admin", href: "/admin", icon: "admin", adminOnly: true },
    ],
  },
  {
    key: "comms",
    title: "Общение",
    items: [{ key: "chat", href: "/chat", icon: "chat" }],
  },
];

// [href, key] pairs sorted by href length desc so the most specific route wins
// (e.g. /settings/ntd beats /settings). "/" is handled separately as `feed`.
const HREF_TO_KEY: Array<[string, string]> = NAV_GROUPS.flatMap((g) =>
  g.items.map((it) => [it.href, it.key] as [string, string]),
)
  .filter(([href]) => href !== "/")
  .sort((a, b) => b[0].length - a[0].length);

/**
 * Map a pathname to the section key that owns it, or null for routes not tied to
 * any section (utility pages like /auth/*, /offline — always allowed).
 */
export function pathToSectionKey(pathname: string): string | null {
  if (pathname === "/") return "feed";
  for (const [href, key] of HREF_TO_KEY) {
    if (pathname === href || pathname.startsWith(href + "/")) return key;
  }
  return null;
}

/** Whether a user may see/use the given section. Admins may use everything. */
export function canUseSection(
  sections: string[] | undefined,
  roles: string[] | undefined,
  key: string,
): boolean {
  if (roles?.includes("admin")) return true;
  if (BASE_SECTION_KEYS.includes(key)) return true;
  return (sections ?? []).includes(key);
}
