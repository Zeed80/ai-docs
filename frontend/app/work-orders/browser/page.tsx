"use client";

/**
 * T3: браузерная сессия сотрудника, переданная владельцу (код MFA, CAPTCHA).
 *
 * Владелец видит ту же страницу и действует в ней сам: щелчок по снимку,
 * ввод текста, клавиши. Пока сессия у владельца, действия сотрудника в ней
 * отклоняются; «Вернуть сотруднику» отдаёт её обратно. Ввод пишется в журнал
 * как «ввёл владелец» — без самого текста, и сотрудник его не видит.
 */

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";

const API = getApiBaseUrl();
const KEYS = ["Enter", "Tab", "Backspace", "Escape"] as const;

type View = {
  ok: boolean;
  url: string;
  title: string;
  handoff: { reason: string } | null;
  outside_allowlist: boolean;
  viewport: { width: number; height: number };
  screenshot_b64: string;
};

type SessionRow = {
  session_id: string;
  url: string;
  handoff: { reason: string } | null;
};

function detail(body: unknown, status: number): string {
  const d = (body as { detail?: unknown })?.detail;
  return typeof d === "string" ? d : `Ошибка ${status}`;
}

const ERRORS: Record<string, string> = {
  session_not_found: "Сессия закрыта или не найдена.",
  session_with_agent: "Сессия у сотрудника. Нажмите «Взять сессию».",
  navigation_left_allowlist:
    "Страница вне разрешённых сайтов — сессия закрыта, сотруднику её не вернуть.",
  key_not_allowed: "Эта клавиша недоступна.",
};

function BrowserHandoff() {
  const params = useSearchParams();
  const workOrderId = params.get("work_order_id") ?? "";
  const [sessionId, setSessionId] = useState(params.get("session") ?? "");
  const [sessions, setSessions] = useState<SessionRow[] | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);

  const call = useCallback(
    async (op: string, extra: Record<string, unknown> = {}) => {
      if (!sessionId) return;
      const r = await fetch(
        `${API}/api/computer-use/sessions/${sessionId}/human`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({ work_order_id: workOrderId, op, ...extra }),
        },
      );
      const body = await r.json().catch(() => null);
      if (!r.ok) {
        const code = detail(body, r.status);
        setError(ERRORS[code] ?? code);
        return null;
      }
      setError(null);
      setView(body as View);
      return body as View;
    },
    [sessionId, workOrderId],
  );

  useEffect(() => {
    if (!workOrderId) return;
    fetch(`${API}/api/computer-use/sessions?work_order_id=${workOrderId}`, {
      credentials: "include",
    })
      .then(async (r) => {
        const body = await r.json().catch(() => null);
        if (!r.ok) throw new Error(detail(body, r.status));
        const rows = (body?.sessions ?? []) as SessionRow[];
        setSessions(rows);
        if (!sessionId && rows.length) setSessionId(rows[0].session_id);
      })
      .catch((e) => setError(String(e.message ?? e)));
  }, [workOrderId, sessionId]);

  // A live picture while the page is open: the site may change on its own.
  useEffect(() => {
    if (!sessionId) return;
    void call("view");
    const id = setInterval(() => void call("view"), 3000);
    return () => clearInterval(id);
  }, [sessionId, call]);

  async function act(op: string, extra: Record<string, unknown> = {}) {
    setBusy(true);
    try {
      return await call(op, extra);
    } finally {
      setBusy(false);
    }
  }

  function clickShot(e: React.MouseEvent<HTMLImageElement>) {
    const img = imgRef.current;
    if (!img || !view?.handoff) return;
    const rect = img.getBoundingClientRect();
    const x = Math.round(
      ((e.clientX - rect.left) / rect.width) * view.viewport.width,
    );
    const y = Math.round(
      ((e.clientY - rect.top) / rect.height) * view.viewport.height,
    );
    void act("click", { x, y });
  }

  async function typeText(e: React.FormEvent) {
    e.preventDefault();
    if (!text) return;
    const done = await act("type", { text });
    if (done) setText("");
  }

  if (!workOrderId) {
    return (
      <p className="text-sm text-muted-foreground">
        Откройте сессию по ссылке сотрудника.
      </p>
    );
  }
  const held = Boolean(view?.handoff);
  return (
    <div className="space-y-3">
      {sessions && sessions.length > 1 && (
        <div className="flex flex-wrap gap-2" aria-label="Сессии заказа">
          {sessions.map((s) => (
            <button
              key={s.session_id}
              type="button"
              onClick={() => setSessionId(s.session_id)}
              className={`min-w-0 max-w-full truncate rounded-md border px-2 py-1 text-xs ${
                s.session_id === sessionId ? "border-primary" : "border-border"
              }`}
            >
              {s.handoff ? "● " : ""}
              {s.url}
            </button>
          ))}
        </div>
      )}
      {sessions && sessions.length === 0 && (
        <p className="text-sm text-muted-foreground">
          У этой работы нет открытых сессий браузера.
        </p>
      )}
      {error && (
        <p role="alert" className="break-words text-sm text-red-500">
          {error}
        </p>
      )}
      {view && (
        <>
          <div className="min-w-0 break-all text-xs text-muted-foreground">
            {view.title} · {view.url}
            {view.outside_allowlist && " · вне разрешённых сайтов"}
          </div>
          <p className="break-words text-sm">
            {held
              ? `Сессия у вас${view.handoff?.reason ? `: ${view.handoff.reason}` : ""}. Сотрудник ждёт.`
              : "Сессия у сотрудника — вы только смотрите."}
          </p>
          {/* eslint-disable-next-line @next/next/no-img-element -- a live data: screenshot */}
          <img
            ref={imgRef}
            src={`data:image/png;base64,${view.screenshot_b64}`}
            alt="Страница в браузере сотрудника"
            onClick={clickShot}
            className={`w-full rounded-md border border-border ${held ? "cursor-crosshair" : ""}`}
          />
          {held ? (
            <div className="flex flex-wrap items-center gap-2">
              <form
                onSubmit={typeText}
                className="flex min-w-0 flex-1 basis-64 gap-2"
              >
                <input
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                  placeholder="Текст в поле страницы (код, ответ)"
                  autoComplete="off"
                  aria-label="Текст для страницы"
                  className="min-w-0 flex-1 rounded-md border border-border bg-background px-3 py-2 text-sm"
                />
                <button
                  type="submit"
                  disabled={busy || !text}
                  className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
                >
                  Ввести
                </button>
              </form>
              {KEYS.map((key) => (
                <button
                  key={key}
                  type="button"
                  disabled={busy}
                  onClick={() => void act("key", { key })}
                  className="rounded-md border border-border px-2 py-1.5 text-xs disabled:opacity-50"
                >
                  {key}
                </button>
              ))}
              <button
                type="button"
                disabled={busy}
                onClick={() => void act("return")}
                className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
              >
                Вернуть сотруднику
              </button>
            </div>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => void act("take")}
              className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
            >
              Взять сессию
            </button>
          )}
          <p className="break-words text-xs text-muted-foreground">
            Щёлкните по снимку, чтобы нажать в этом месте страницы. Введённое
            пишется в журнал как ваше действие без самого текста; сотрудник его
            не увидит.
          </p>
        </>
      )}
    </div>
  );
}

export default function Page() {
  return (
    <main className="mx-auto max-w-5xl space-y-4 p-4">
      <h1 className="text-xl font-semibold">Браузер сотрудника</h1>
      <Suspense fallback={<p>Загрузка…</p>}>
        <BrowserHandoff />
      </Suspense>
    </main>
  );
}
