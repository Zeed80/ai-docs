"use client";

import { usePermission, useHasRole } from "@/lib/rbac";

interface CanProps {
  permission: string;
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

interface ForRoleProps {
  roles: string[];
  children: React.ReactNode;
  fallback?: React.ReactNode;
}

export function Can({ permission, children, fallback = null }: CanProps) {
  const allowed = usePermission(permission);
  return allowed ? <>{children}</> : <>{fallback}</>;
}

export function ForRole({ roles, children, fallback = null }: ForRoleProps) {
  const allowed = useHasRole(...roles);
  return allowed ? <>{children}</> : <>{fallback}</>;
}
