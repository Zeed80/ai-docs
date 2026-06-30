"use client";

import { useCallback, useEffect, useState } from "react";

import ModelsSection from "@/components/studio/ModelsSection";
import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();
const ADMIN = `${API}/api/comfyui-admin`;

interface Status {
  configured: boolean;
  base_url?: string;
  is_local?: boolean;
  online?: boolean;
  managed_local?: boolean;
  container_state?: string;
  devices?: { name?: string; vram_total?: number }[];
  queue?: { running: number; pending: number };
  error?: string;
}

interface Found {
  base_url: string;
  devices?: { name?: string }[];
}

export default function ComfyUISettingsPage() {
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [found, setFound] = useState<Found[]>([]);
  const [cidr, setCidr] = useState("");
  const [manualUrl, setManualUrl] = useState("");

  const loadStatus = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch(`${ADMIN}/status`);
      setStatus(await res.json());
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  async function action(label: string, fn: () => Promise<Response>) {
    setBusy(label);
    setMsg(null);
    setErr(null);
    try {
      const res = await fn();
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      if (body?.message) setMsg(body.message);
      await loadStatus();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(null);
    }
  }

  async function discover(scanNetwork: boolean) {
    setBusy(scanNetwork ? "scan" : "discover");
    setErr(null);
    setMsg(null);
    setFound([]);
    try {
      const res = await mutFetch(`${ADMIN}/discover`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          scan_network: scanNetwork,
          cidr: cidr.trim() || null,
        }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body?.detail || `HTTP ${res.status}`);
      setFound(body.found || []);
      if ((body.found || []).length === 0)
        setMsg("ComfyUI не найден среди проверенных адресов.");
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(null);
    }
  }

  async function registerNode(baseUrl: string) {
    await action("register", () =>
      mutFetch(`${API}/api/providers`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind: "comfyui",
          name: `ComfyUI (${baseUrl})`,
          base_url: baseUrl,
          is_local: true,
          enabled: true,
        }),
      }),
    );
    setMsg(`Узел ${baseUrl} зарегистрирован.`);
  }

  const online = status?.online;

  return (
    <div className="space-y-6 max-w-3xl">
      <section>
        <h2 className="text-lg font-semibold mb-1">
          ComfyUI — генерация изображений
        </h2>
        <p className="text-sm text-muted-foreground">
          Внешний сервер ComfyUI для создания и редактирования чертежей. Сервер
          может быть на этом хосте (управляемый) или на другом —
          confidential-чертежи отправляются только на локальные узлы.
        </p>
      </section>

      {/* Status */}
      <section className="rounded-lg border border-border p-4">
        <div className="flex items-center justify-between">
          <h3 className="font-medium">Статус</h3>
          <button
            onClick={loadStatus}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            Обновить
          </button>
        </div>
        {loading ? (
          <p className="text-sm text-muted-foreground mt-2">Загрузка…</p>
        ) : !status?.configured ? (
          <p className="text-sm text-amber-500 mt-2">
            Узел не настроен. {status?.error}
          </p>
        ) : (
          <div className="mt-2 text-sm space-y-1">
            <div>
              Состояние:{" "}
              <span className={online ? "text-emerald-500" : "text-red-500"}>
                {online ? "онлайн" : "недоступен"}
              </span>
            </div>
            <div className="text-muted-foreground">
              Адрес: {status.base_url}
            </div>
            {status.managed_local && (
              <div className="text-muted-foreground">
                Локальный контейнер: {status.container_state ?? "—"}
              </div>
            )}
            {status.devices && status.devices.length > 0 && (
              <div className="text-muted-foreground">
                GPU:{" "}
                {status.devices
                  .map((d) => d.name)
                  .filter(Boolean)
                  .join(", ")}
              </div>
            )}
            {status.queue && (
              <div className="text-muted-foreground">
                Очередь: {status.queue.running} выполняется,{" "}
                {status.queue.pending} ждёт
              </div>
            )}
          </div>
        )}

        {/* Managed-local lifecycle */}
        {status?.managed_local && (
          <div className="mt-3 flex flex-wrap gap-2">
            {["start", "restart", "stop"].map((a) => (
              <button
                key={a}
                disabled={!!busy}
                onClick={() =>
                  action(a, () =>
                    mutFetch(`${ADMIN}/server/${a}`, { method: "POST" }),
                  )
                }
                className="px-3 py-1.5 rounded bg-secondary hover:bg-secondary/80 text-sm disabled:opacity-50"
              >
                {a === "start"
                  ? "Запустить"
                  : a === "restart"
                    ? "Перезапустить"
                    : "Остановить"}
              </button>
            ))}
            <button
              disabled={!!busy}
              onClick={() =>
                action("update", () =>
                  mutFetch(`${ADMIN}/update`, { method: "POST" }),
                )
              }
              className="px-3 py-1.5 rounded bg-secondary hover:bg-secondary/80 text-sm disabled:opacity-50"
            >
              Обновить образ
            </button>
          </div>
        )}
        {!status?.managed_local && status?.configured && (
          <div className="mt-3">
            <button
              disabled={!!busy}
              onClick={() =>
                action("install", () =>
                  mutFetch(`${ADMIN}/install`, { method: "POST" }),
                )
              }
              className="px-3 py-1.5 rounded bg-secondary hover:bg-secondary/80 text-sm disabled:opacity-50"
            >
              Установить локально
            </button>
          </div>
        )}
      </section>

      {/* Discovery */}
      <section className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="font-medium">Найти ComfyUI</h3>
        <div className="flex flex-wrap gap-2">
          <button
            disabled={!!busy}
            onClick={() => discover(false)}
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary/90 text-sm disabled:opacity-50"
          >
            {busy === "discover" ? "Поиск…" : "Найти локально / в Docker"}
          </button>
          <input
            value={cidr}
            onChange={(e) => setCidr(e.target.value)}
            placeholder="192.168.1.0/24 (для сети)"
            className="px-2 py-1.5 rounded bg-background border border-border text-sm w-56"
          />
          <button
            disabled={!!busy}
            onClick={() => discover(true)}
            className="px-3 py-1.5 rounded bg-secondary hover:bg-secondary/80 text-sm disabled:opacity-50"
          >
            {busy === "scan" ? "Сканирование…" : "Искать в сети"}
          </button>
        </div>
        {found.length > 0 && (
          <ul className="space-y-1">
            {found.map((f) => (
              <li
                key={f.base_url}
                className="flex items-center justify-between rounded border border-border px-3 py-2 text-sm"
              >
                <span>
                  {f.base_url}
                  {f.devices?.[0]?.name ? ` · ${f.devices[0].name}` : ""}
                </span>
                <button
                  disabled={!!busy}
                  onClick={() => registerNode(f.base_url)}
                  className="px-2.5 py-1 rounded bg-primary text-primary-foreground text-xs disabled:opacity-50"
                >
                  Использовать
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Manual node */}
      <section className="rounded-lg border border-border p-4 space-y-2">
        <h3 className="font-medium">Добавить узел вручную</h3>
        <div className="flex flex-wrap gap-2">
          <input
            value={manualUrl}
            onChange={(e) => setManualUrl(e.target.value)}
            placeholder="http://comfyui-host:8188"
            className="px-2 py-1.5 rounded bg-background border border-border text-sm flex-1 min-w-56"
          />
          <button
            disabled={!!busy || !manualUrl.trim()}
            onClick={() => registerNode(manualUrl.trim())}
            className="px-3 py-1.5 rounded bg-secondary hover:bg-secondary/80 text-sm disabled:opacity-50"
          >
            Добавить
          </button>
        </div>
        <p className="text-xs text-muted-foreground">
          Узел также доступен в разделе{" "}
          <a href="/settings/models" className="text-primary hover:underline">
            Модели
          </a>{" "}
          (провайдеры). Воркфлоу настраиваются в{" "}
          <a href="/studio" className="text-primary hover:underline">
            Графической студии
          </a>
          .
        </p>
      </section>

      {/* Models: installed + recommended downloads (24GB-friendly) */}
      <ModelsSection />

      {msg && <div className="text-sm text-emerald-500">{msg}</div>}
      {err && <div className="text-sm text-red-500">{err}</div>}
    </div>
  );
}
