"use client";

export const dynamic = "force-dynamic";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

function CallbackHandler() {
  const params = useSearchParams();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const code = params.get("code");
    const state = params.get("state");
    const errorParam = params.get("error");

    if (errorParam) {
      setError(`Ошибка от провайдера: ${errorParam}`);
      return;
    }

    if (!code) {
      setError("Отсутствует код авторизации");
      return;
    }

    // Redirect the browser directly to the backend callback endpoint.
    // Next.js rewrites /api/* → backend, which sets the httpOnly cookie
    // and redirects to /inbox. The browser follows the chain naturally.
    const qs = new URLSearchParams({ code, state: state ?? "" });
    window.location.replace(`/api/auth/callback?${qs}`);
  }, [params]);

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-slate-50">
        <div className="text-center max-w-sm">
          <div className="w-12 h-12 bg-red-100 rounded-full flex items-center justify-center mx-auto mb-4">
            <svg
              className="w-6 h-6 text-red-600"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-slate-800">Ошибка входа</h2>
          <p className="text-sm text-slate-500 mt-2">{error}</p>
          <a
            href="/auth/login"
            className="mt-4 inline-block px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700"
          >
            Попробовать снова
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <div className="text-center">
        <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin mx-auto mb-3" />
        <p className="text-sm text-slate-500">Выполняется вход...</p>
      </div>
    </div>
  );
}

export default function AuthCallbackPage() {
  return (
    <Suspense
      fallback={
        <div className="min-h-screen flex items-center justify-center bg-slate-50">
          <div className="w-8 h-8 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
        </div>
      }
    >
      <CallbackHandler />
    </Suspense>
  );
}
