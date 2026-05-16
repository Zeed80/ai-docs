"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { useCurrentUser } from "@/lib/auth-context";
import { hasRole } from "@/lib/rbac";

interface Props {
  children: React.ReactNode;
  requiredRoles?: string[];
  fallback?: React.ReactNode;
}

export function ProtectedRoute({ children, requiredRoles, fallback }: Props) {
  const user = useCurrentUser();
  const router = useRouter();

  useEffect(() => {
    if (user === undefined) return; // still loading
    if (user === null) {
      router.replace("/auth/login");
      return;
    }
    if (requiredRoles && !hasRole(user.roles, ...requiredRoles)) {
      router.replace("/");
    }
  }, [user, requiredRoles, router]);

  if (user === undefined) {
    return (
      fallback ?? (
        <div className="flex items-center justify-center h-32">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-muted-foreground border-t-transparent" />
        </div>
      )
    );
  }

  if (
    user === null ||
    (requiredRoles && !hasRole(user.roles, ...requiredRoles))
  ) {
    return fallback ?? null;
  }

  return <>{children}</>;
}
