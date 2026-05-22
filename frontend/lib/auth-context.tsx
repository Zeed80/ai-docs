"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { fetchMe, type UserInfo } from "./auth";

const AuthContext = createContext<UserInfo | null | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<UserInfo | null | undefined>(undefined);

  useEffect(() => {
    fetchMe().then((u) => {
      if (u) {
        setUser(u);
      } else if (
        typeof window !== "undefined" &&
        !window.location.pathname.startsWith("/auth/")
      ) {
        // Cookie missing or expired — redirect to login preserving the current path
        window.location.href = `/auth/login?next=${encodeURIComponent(window.location.pathname)}`;
      } else {
        setUser(null);
      }
    });
  }, []);

  return <AuthContext.Provider value={user}>{children}</AuthContext.Provider>;
}

/** Returns the current user. undefined = still loading, null = not authenticated. */
export function useCurrentUser(): UserInfo | null | undefined {
  return useContext(AuthContext);
}
