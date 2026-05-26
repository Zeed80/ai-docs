"use client";

import { useEffect, useState, useCallback } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders, mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

// ── Types ──────────────────────────────────────────────────────────────────

interface LlamaCppStatus {
  running: boolean;
  url: string;
  model_loaded: string | null;
  ctx_size: number | null;
  slots_idle: number | null;
  slots_processing: number | null;
  version: string | null;
}

interface LlamaCppConfig {
  url: string;
  model: string;
  ctx_size: number;
  kv_cache_type: string;
  n_gpu_layers: number;
  parallel: number;
  flash_attn: boolean;
}

interface GgufModel {
  name: string;
  path: string;
  size_bytes: number;
  size_human: string;
}

const KV_CACHE_OPTIONS = [
  { value: "f16", label: "f16 — полная точность" },
  { value: "q8_0", label: "q8_0 — TurboQuant (рекомендуется)" },
  { value: "q4_0", label: "q4_0 — максимальная компрессия" },
];

// ── Page ───────────────────────────────────────────────────────────────────

export default function LlamaCppSettingsPage() {
  const [status, setStatus] = useState<LlamaCppStatus | null>(null);
  const [config, setConfig] = useState<LlamaCppConfig | null>(null);
  const [models, setModels] = useState<GgufModel[]>([]);
  const [draft, setDraft] = useState<Partial<LlamaCppConfig>>({});
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ text: string; ok: boolean } | null>(
    null,
  );
  const [loadingStatus, setLoadingStatus] = useState(true);

  const loadAll = useCallback(async () => {
    setLoadingStatus(true);
    try {
      const [st, cfg, mdl] = await Promise.all([
        fetch(`${API}/api/llamacpp/status`).then((r) => r.json()),
        fetch(`${API}/api/llamacpp/config`).then((r) => r.json()),
        fetch(`${API}/api/llamacpp/models`).then((r) => r.json()),
      ]);
      setStatus(st);
      setConfig(cfg);
      setDraft(cfg);
      setModels(mdl);
    } catch {
      setMessage({ text: "Ошибка загрузки данных", ok: false });
    } finally {
      setLoadingStatus(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
    const interval = setInterval(() => {
      fetch(`${API}/api/llamacpp/status`)
        .then((r) => r.json())
        .then(setStatus)
        .catch(() => {});
    }, 10000);
    return () => clearInterval(interval);
  }, [loadAll]);

  async function saveConfig() {
    if (!config) return;
    setSaving(true);
    setMessage(null);
    try {
      const changes: Partial<LlamaCppConfig> = {};
      for (const key of Object.keys(draft) as Array<keyof LlamaCppConfig>) {
        if (draft[key] !== config[key]) {
          (changes as Record<string, unknown>)[key] = draft[key];
        }
      }
      if (Object.keys(changes).length === 0) {
        setMessage({ text: "Изменений нет", ok: true });
        return;
      }
      const updated = await mutFetch(`${API}/api/llamacpp/config`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          ...(await csrfHeaders()),
        },
        body: JSON.stringify(changes),
      });
      const data: LlamaCppConfig = await updated.json();
      setConfig(data);
      setDraft(data);
      setMessage({
        text: "Конфигурация сохранена. Перезапустите llama-server для применения.",
        ok: true,
      });
    } catch {
      setMessage({ text: "Ошибка сохранения конфигурации", ok: false });
    } finally {
      setSaving(false);
    }
  }

  function set(field: keyof LlamaCppConfig, value: unknown) {
    setDraft((d) => ({ ...d, [field]: value }));
  }

  const hasChanges = config
    ? Object.keys(draft).some(
        (k) =>
          draft[k as keyof LlamaCppConfig] !==
          config[k as keyof LlamaCppConfig],
      )
    : false;

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-8">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold">llama.cpp сервер</h1>
        <p className="text-sm text-gray-500 mt-1">
          Локальный вывод через llama.cpp — высокая скорость, TurboQuant KV-кэш,
          MTP-модели.
        </p>
      </div>

      {/* Status card */}
      <section className="border rounded-lg p-5 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="font-semibold text-lg">Статус сервера</h2>
          <button
            onClick={loadAll}
            className="text-sm text-blue-600 hover:underline"
            disabled={loadingStatus}
          >
            {loadingStatus ? "Обновление..." : "↺ Обновить"}
          </button>
        </div>

        {status && (
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="flex items-center gap-2">
              <span
                className={`inline-block w-2.5 h-2.5 rounded-full ${
                  status.running ? "bg-green-500" : "bg-red-400"
                }`}
              />
              <span className="font-medium">
                {status.running ? "Запущен" : "Недоступен"}
              </span>
            </div>
            <div className="text-gray-500">{status.url}</div>

            {status.running && (
              <>
                <div className="text-gray-500">Модель</div>
                <div
                  className="font-mono text-xs truncate"
                  title={status.model_loaded ?? ""}
                >
                  {status.model_loaded
                    ? status.model_loaded.split("/").pop()
                    : "—"}
                </div>

                <div className="text-gray-500">Контекст</div>
                <div>
                  {status.ctx_size
                    ? `${status.ctx_size.toLocaleString()} токенов`
                    : "—"}
                </div>

                <div className="text-gray-500">Слоты (idle / active)</div>
                <div>
                  {status.slots_idle ?? "—"} / {status.slots_processing ?? "—"}
                </div>

                {status.version && (
                  <>
                    <div className="text-gray-500">Версия</div>
                    <div>{status.version}</div>
                  </>
                )}
              </>
            )}

            {!status.running && (
              <div className="col-span-2 bg-yellow-50 border border-yellow-200 rounded p-3 text-xs text-yellow-800">
                Сервер не запущен. Запустите стэк с профилем:
                <code className="ml-1 font-mono bg-yellow-100 px-1 rounded">
                  docker compose --profile embedded-llamacpp up -d
                </code>
              </div>
            )}
          </div>
        )}
      </section>

      {/* GGUF Models */}
      <section className="border rounded-lg p-5 space-y-3">
        <h2 className="font-semibold text-lg">Модели (GGUF)</h2>
        {models.length === 0 ? (
          <div className="text-sm text-gray-500 space-y-1">
            <p>GGUF-файлы не найдены в директории моделей.</p>
            <p className="text-xs">
              Скопируйте файл модели в Docker-том{" "}
              <code className="font-mono bg-gray-100 px-1 rounded">
                llamacpp_models
              </code>{" "}
              или укажите путь вручную в конфигурации ниже.
            </p>
            <p className="text-xs mt-2 text-gray-400">
              Пример:{" "}
              <code className="font-mono">
                docker run --rm -v llamacpp_models:/models ...
              </code>
            </p>
          </div>
        ) : (
          <div className="space-y-2">
            {models.map((m) => (
              <div
                key={m.path}
                className="flex items-center justify-between p-3 border rounded hover:bg-gray-50 cursor-pointer"
                onClick={() => set("model", m.path)}
                title="Нажмите, чтобы выбрать модель"
              >
                <div>
                  <div className="font-mono text-sm font-medium">{m.name}</div>
                  <div className="text-xs text-gray-400">{m.path}</div>
                </div>
                <div className="flex items-center gap-3">
                  <span className="text-sm text-gray-500">{m.size_human}</span>
                  {draft.model === m.path && (
                    <span className="text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded">
                      выбрана
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* Configuration */}
      {draft && (
        <section className="border rounded-lg p-5 space-y-5">
          <h2 className="font-semibold text-lg">Конфигурация</h2>

          {/* URL */}
          <div className="space-y-1">
            <label className="text-sm font-medium">URL сервера</label>
            <input
              type="text"
              value={draft.url ?? ""}
              onChange={(e) => set("url", e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono"
              placeholder="http://llama-server:8080"
            />
            <p className="text-xs text-gray-400">
              Внутренний адрес llama-server в Docker-сети. Для внешнего сервера
              укажите его URL.
            </p>
          </div>

          {/* Model path */}
          <div className="space-y-1">
            <label className="text-sm font-medium">Путь к модели</label>
            <input
              type="text"
              value={draft.model ?? ""}
              onChange={(e) => set("model", e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm font-mono"
              placeholder="/models/model.gguf"
            />
          </div>

          {/* Context size */}
          <div className="space-y-2">
            <label className="text-sm font-medium">
              Размер контекста:{" "}
              <span className="font-mono">
                {(draft.ctx_size ?? 8192).toLocaleString()} токенов
              </span>
            </label>
            <input
              type="range"
              min={512}
              max={131072}
              step={512}
              value={draft.ctx_size ?? 8192}
              onChange={(e) => set("ctx_size", Number(e.target.value))}
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-400">
              <span>512</span>
              <span>8K</span>
              <span>32K</span>
              <span>64K</span>
              <span>128K</span>
            </div>
          </div>

          {/* KV cache type */}
          <div className="space-y-1">
            <label className="text-sm font-medium">
              Тип KV-кэша (TurboQuant)
            </label>
            <select
              value={draft.kv_cache_type ?? "q8_0"}
              onChange={(e) => set("kv_cache_type", e.target.value)}
              className="w-full border rounded px-3 py-2 text-sm"
            >
              {KV_CACHE_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <p className="text-xs text-gray-400">
              q8_0 сокращает память KV-кэша в 2× при минимальных потерях
              качества (TurboQuant). q4_0 сокращает в 4×, но может влиять на
              качество длинных контекстов.
            </p>
          </div>

          {/* GPU layers */}
          <div className="space-y-2">
            <label className="text-sm font-medium">
              GPU-слои:{" "}
              <span className="font-mono">
                {draft.n_gpu_layers === -1 ? "все (-1)" : draft.n_gpu_layers}
              </span>
            </label>
            <input
              type="range"
              min={-1}
              max={128}
              step={1}
              value={draft.n_gpu_layers ?? -1}
              onChange={(e) => set("n_gpu_layers", Number(e.target.value))}
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-400">
              <span>CPU (-1 = все)</span>
              <span>32</span>
              <span>64</span>
              <span>128</span>
            </div>
          </div>

          {/* Parallel slots */}
          <div className="space-y-2">
            <label className="text-sm font-medium">
              Параллельных слотов:{" "}
              <span className="font-mono">{draft.parallel ?? 4}</span>
            </label>
            <input
              type="range"
              min={1}
              max={16}
              step={1}
              value={draft.parallel ?? 4}
              onChange={(e) => set("parallel", Number(e.target.value))}
              className="w-full"
            />
            <div className="flex justify-between text-xs text-gray-400">
              <span>1</span>
              <span>4</span>
              <span>8</span>
              <span>16</span>
            </div>
          </div>

          {/* Flash attention */}
          <div className="flex items-center gap-3">
            <input
              id="flash-attn"
              type="checkbox"
              checked={draft.flash_attn ?? true}
              onChange={(e) => set("flash_attn", e.target.checked)}
              className="w-4 h-4"
            />
            <label
              htmlFor="flash-attn"
              className="text-sm font-medium cursor-pointer"
            >
              Flash Attention (--flash-attn)
            </label>
            <span className="text-xs text-gray-400">
              Ускоряет вывод на совместимых GPU (~15–25%)
            </span>
          </div>

          {/* Actions */}
          {message && (
            <div
              className={`text-sm px-3 py-2 rounded ${
                message.ok
                  ? "bg-green-50 text-green-800 border border-green-200"
                  : "bg-red-50 text-red-800 border border-red-200"
              }`}
            >
              {message.text}
            </div>
          )}

          <div className="flex gap-3 pt-2">
            <button
              onClick={saveConfig}
              disabled={saving || !hasChanges}
              className="px-4 py-2 bg-blue-600 text-white rounded text-sm font-medium hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? "Сохранение..." : "Сохранить конфигурацию"}
            </button>
            {hasChanges && (
              <button
                onClick={() => {
                  setDraft(config ?? {});
                  setMessage(null);
                }}
                className="px-4 py-2 border rounded text-sm hover:bg-gray-50"
              >
                Отменить
              </button>
            )}
          </div>
        </section>
      )}

      {/* Usage hints */}
      <section className="border rounded-lg p-5 space-y-3 bg-gray-50">
        <h2 className="font-semibold">Запуск и управление</h2>
        <div className="space-y-2 text-sm text-gray-700">
          <div>
            <p className="font-medium mb-1">Запустить llama-server:</p>
            <code className="block bg-gray-100 rounded px-3 py-2 text-xs font-mono">
              docker compose --profile embedded-llamacpp up -d llama-server
            </code>
          </div>
          <div>
            <p className="font-medium mb-1">Скопировать GGUF-модель в том:</p>
            <code className="block bg-gray-100 rounded px-3 py-2 text-xs font-mono whitespace-pre-wrap">
              {
                "docker run --rm \\\n  -v $(pwd)/model.gguf:/models/model.gguf \\\n  ghcr.io/ggml-org/llama.cpp:server \\\n  --model /models/model.gguf"
              }
            </code>
            <p className="text-xs text-gray-400 mt-1">
              или скопируйте GGUF в Docker-том:{" "}
              <code className="font-mono text-xs">
                docker cp model.gguf &lt;llamacpp_container&gt;:/models/
              </code>
            </p>
          </div>
          <div>
            <p className="font-medium mb-1">Порты:</p>
            <p className="text-xs text-gray-500">
              llama-server доступен на{" "}
              <code className="font-mono">localhost:11436</code> (хост) и{" "}
              <code className="font-mono">llama-server:8080</code> (внутри
              Docker-сети).
            </p>
          </div>
          <div>
            <p className="font-medium mb-1">Рекомендуемые модели:</p>
            <ul className="text-xs text-gray-500 space-y-0.5 list-disc list-inside">
              <li>Qwen3.5-9B-GGUF — быстрый reasoning, 9B, q8_0 ~ 9 GB</li>
              <li>Qwen3.5-27B-GGUF — сбалансированный, 27B, q4_0 ~ 16 GB</li>
              <li>
                gemma-4-4b-GGUF — компактный мультимодальный, 4B, q8_0 ~ 4 GB
              </li>
            </ul>
          </div>
        </div>
      </section>
    </div>
  );
}
