"use client";

import { useEffect } from "react";
import { loginUrl } from "@/lib/auth";

export default function LoginPage() {
  useEffect(() => {
    // In dev mode (AUTH_ENABLED=false) the backend /me returns dev user without redirect,
    // so this page is only reached in production. Auto-redirect to Authentik.
    window.location.href = loginUrl();
  }, []);

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <div className="text-center">
        <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin mx-auto mb-3" />
        <p className="text-sm text-slate-500">
          Перенаправление на страницу входа...
        </p>
      </div>
    </div>
  );
}
