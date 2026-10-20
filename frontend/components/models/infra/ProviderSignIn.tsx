"use client";

/**
 * Вход через сайт облачного провайдера — альтернатива ключу API.
 *
 * Часть провайдеров даёт подключить аккаунт с подпиской (ChatGPT, SuperGrok,
 * Kimi Code, MiniMax), а не только платный ключ. Два вида входа:
 * - «device»: сервер получает код, человек подтверждает его на странице
 *   провайдера, страница ждёт подтверждения сама;
 * - «redirect» (OpenRouter): уход на сайт провайдера и возврат в эту панель —
 *   на выходе обычный ключ, привязанный к аккаунту.
 *
 * Токены страница не видит: их хранит и обновляет сервер.
 */

import { useEffect, useRef, useState } from "react";
import { useToast } from "@/components/ui/primitives/Toast";
import { btn, select } from "@/components/ui/primitives/tokens";
import {
  ApiError,
  oauthLogout,
  oauthPoll,
  oauthStart,
  refreshProviderModels,
  testProvider,
} from "@/lib/models/api";
import type {
  OAuthStart,
  ProviderInstance,
  ProviderOAuthKind,
} from "@/lib/models/types";

const btnPrimary = `${btn} bg-blue-600 hover:bg-blue-700 text-white disabled:opacity-50`;
const btnSecondary = `${btn} bg-slate-700 hover:bg-slate-600 text-slate-200 disabled:opacity-50`;

const REGION_LABEL: Record<string, string> = {
  global: "международный (minimax.io)",
  cn: "Китай (minimaxi.com)",
};

const describe = (e: unknown) =>
  e instanceof ApiError ? e.detail : String((e as Error)?.message ?? e);

export function ProviderSignIn({
  inst,
  oauth,
  onChanged,
}: {
  inst: ProviderInstance;
  oauth: ProviderOAuthKind;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [region, setRegion] = useState(oauth.regions[0] ?? "");
  const [pending, setPending] = useState<OAuthStart | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | null>(null);
  const signedIn = inst.auth_mode === "oauth";

  const stop = () => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
  };
  useEffect(() => stop, []);

  const afterSignIn = async () => {
    // Сразу то, что после ключа приходилось делать руками: проверка и каталог.
    try {
      const t = await testProvider(inst.id);
      if (!t.ok) {
        toast.error("Вход выполнен, но провайдер не ответил", t.error ?? "");
        return;
      }
      const m = await refreshProviderModels(inst.id);
      toast.ok("Вход выполнен", `Моделей доступно: ${m.count}`);
    } catch (e) {
      toast.error("Вход выполнен, но модели не загрузились", describe(e));
    } finally {
      onChanged();
    }
  };

  const poll = (flow: OAuthStart) => {
    const interval = Math.max(2, flow.interval ?? 5) * 1000;
    timer.current = window.setTimeout(async () => {
      try {
        const r = await oauthPoll(flow.flow_id);
        if (r.status === "pending") {
          poll(flow);
          return;
        }
        setPending(null);
        if (r.status === "success") {
          setStatus(null);
          if (r.message) toast.ok("Сообщение провайдера", r.message);
          await afterSignIn();
        } else {
          setStatus(r.message || "вход не удался");
        }
      } catch (e) {
        setPending(null);
        setStatus(describe(e));
      }
    }, interval);
  };

  const start = async () => {
    stop();
    setBusy(true);
    setStatus(null);
    try {
      const flow = await oauthStart(inst.id, {
        origin: window.location.origin,
        ...(region ? { region } : {}),
      });
      if (flow.type === "redirect" && flow.authorize_url) {
        window.location.assign(flow.authorize_url);
        return;
      }
      setPending(flow);
      poll(flow);
    } catch (e) {
      setStatus(describe(e));
    } finally {
      setBusy(false);
    }
  };

  const cancel = () => {
    stop();
    setPending(null);
  };

  const logout = async () => {
    setBusy(true);
    try {
      await oauthLogout(inst.id);
      toast.ok(
        "Вход отменён",
        "Провайдер снова работает по ключу API, если он задан",
      );
      onChanged();
    } catch (e) {
      toast.error("Не удалось выйти", describe(e));
    } finally {
      setBusy(false);
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toast.ok("Код скопирован");
    } catch {
      /* код и так виден на экране */
    }
  };

  const until = inst.oauth?.expires_at
    ? new Date(inst.oauth.expires_at * 1000).toLocaleString("ru-RU", {
        dateStyle: "short",
        timeStyle: "short",
      })
    : null;

  return (
    <div className="rounded-md border border-slate-700 bg-slate-900/50 p-3 space-y-2 text-xs">
      <div className="font-medium text-slate-200">
        Вход через сайт — {oauth.label}
      </div>
      {signedIn ? (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-emerald-300 min-w-0 break-all">
            Вход выполнен{inst.oauth?.account ? `: ${inst.oauth.account}` : ""}
          </span>
          {until && (
            <span className="text-slate-400">
              токен до {until}
              {inst.oauth?.refreshable ? ", обновляется сам" : ""}
            </span>
          )}
          <button
            type="button"
            className={btnSecondary}
            disabled={busy}
            onClick={() => void logout()}
          >
            Выйти
          </button>
        </div>
      ) : pending ? (
        <div className="space-y-2">
          <div className="text-slate-300">
            Откройте страницу провайдера, войдите в аккаунт и подтвердите код:
          </div>
          {pending.user_code && (
            <div className="flex flex-wrap items-center gap-2">
              <code
                aria-label="Код подтверждения"
                className="rounded bg-slate-950 px-3 py-1.5 text-lg font-semibold tracking-widest text-slate-100"
              >
                {pending.user_code}
              </code>
              <button
                type="button"
                className={btnSecondary}
                onClick={() => void copy(pending.user_code ?? "")}
              >
                Копировать код
              </button>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {pending.verification_uri && (
              <a
                href={pending.verification_uri}
                target="_blank"
                rel="noopener noreferrer"
                className={btnPrimary}
              >
                Открыть страницу входа
              </a>
            )}
            <button type="button" className={btnSecondary} onClick={cancel}>
              Отмена
            </button>
          </div>
          <div className="text-slate-400" role="status">
            Ждём подтверждения на сайте провайдера…
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {oauth.regions.length > 1 && (
            <label className="flex items-center gap-2 text-slate-300">
              Регион
              <select
                className={`${select} !w-auto`}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
              >
                {oauth.regions.map((r) => (
                  <option key={r} value={r}>
                    {REGION_LABEL[r] ?? r}
                  </option>
                ))}
              </select>
            </label>
          )}
          <button
            type="button"
            className={btnPrimary}
            disabled={busy}
            onClick={() => void start()}
          >
            Войти через сайт
          </button>
        </div>
      )}
      {status && <div className="text-red-400 break-words">{status}</div>}
      <div className="text-slate-500">{oauth.note}</div>
    </div>
  );
}
