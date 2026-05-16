"use client";

import { useMemo } from "react";
import { useCurrentUser } from "./auth-context";

export const ROLE_PERMISSIONS: Record<string, string[]> = {
  admin: ["*"],
  manager: [
    "invoice.approve",
    "invoice.reject",
    "anomaly.resolve",
    "approval.decide",
    "document.read",
    "document.approve",
    "compare.decide",
  ],
  accountant: [
    "invoice.read",
    "invoice.export",
    "document.read",
    "document.extract",
    "table.export",
    "table.import",
    "norm.read",
  ],
  buyer: [
    "supplier.read",
    "supplier.merge",
    "compare.read",
    "document.read",
    "email.read",
    "email.send",
  ],
  engineer: ["document.read", "document.extract", "collection.read"],
  viewer: ["document.read", "invoice.read", "supplier.read"],
};

export function hasPermission(roles: string[], permission: string): boolean {
  for (const role of roles) {
    const perms = ROLE_PERMISSIONS[role] ?? [];
    if (perms.includes("*") || perms.includes(permission)) return true;
  }
  return false;
}

export function hasRole(roles: string[], ...required: string[]): boolean {
  if (roles.includes("admin")) return true;
  return required.some((r) => roles.includes(r));
}

export function usePermission(permission: string): boolean {
  const user = useCurrentUser();
  return useMemo(
    () => (user ? hasPermission(user.roles, permission) : false),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [user?.roles?.join(","), permission],
  );
}

export function useHasRole(...roles: string[]): boolean {
  const user = useCurrentUser();
  return useMemo(
    () => (user ? hasRole(user.roles, ...roles) : false),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [user?.roles?.join(","), roles.join(",")],
  );
}
