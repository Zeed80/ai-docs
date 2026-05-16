"use client";

import { Suspense, useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { loginUrl } from "@/lib/auth";

function LoginRedirect() {
  const params = useSearchParams();

  useEffect(() => {
    const next = params.get("next") ?? "/inbox";
    window.location.href = loginUrl(next);
  }, [params]);

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

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center bg-slate-50">
          <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
        </div>
      }
    >
      <LoginRedirect />
    </Suspense>
  );
}
