"use client";

import { useEffect, useState } from "react";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";

interface MailboxOut {
  id: string;
  name: string;
  display_name: string | null;
  imap_host: string;
  imap_port: number;
  imap_user: string;
  imap_ssl: boolean;
  imap_folder: string;
  smtp_host: string | null;
  smtp_port: number | null;
  smtp_user: string | null;
  smtp_use_tls: boolean;
  smtp_from_address: string | null;
  smtp_from_name: string | null;
  default_doc_type: string | null;
  assigned_role: string | null;
  is_active: boolean;
  last_sync_at: string | null;
  sync_error: string | null;
  auth_method: string;
  oauth_provider: string | null;
  oauth_email: string | null;
  oauth_connected: boolean;
}

interface MailboxPreset {
  id: string;
  label: string;
  imap_host: string;
  imap_port: number;
  imap_ssl: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_use_tls: boolean;
  auth_methods: string[];
  oauth_provider: string | null;
  oauth_configured: boolean;
  hint: string;
}

const OAUTH_PROVIDER_LABEL: Record<string, string> = {
  google: "Google",
  microsoft: "Microsoft",
  yandex: "Yandex",
  mailru: "Mail.ru",
};

interface MailboxForm {
  name: string;
  display_name: string;
  imap_host: string;
  imap_port: string;
  imap_user: string;
  imap_password: string;
  imap_ssl: boolean;
  imap_folder: string;
  smtp_host: string;
  smtp_port: string;
  smtp_user: string;
  smtp_password: string;
  smtp_use_tls: boolean;
  smtp_from_address: string;
  smtp_from_name: string;
  default_doc_type: string;
  assigned_role: string;
  is_active: boolean;
}

const EMPTY_FORM: MailboxForm = {
  name: "",
  display_name: "",
  imap_host: "",
  imap_port: "993",
  imap_user: "",
  imap_password: "",
  imap_ssl: true,
  imap_folder: "INBOX",
  smtp_host: "",
  smtp_port: "587",
  smtp_user: "",
  smtp_password: "",
  smtp_use_tls: true,
  smtp_from_address: "",
  smtp_from_name: "",
  default_doc_type: "",
  assigned_role: "",
  is_active: true,
};

const inputCls =
  "w-full rounded border border-slate-600 bg-slate-900 px-3 py-1.5 text-sm text-slate-200 focus:outline-none focus:ring-1 focus:ring-blue-500";

export function MailboxSection() {
  const [mailboxes, setMailboxes] = useState<MailboxOut[]>([]);
  const [presets, setPresets] = useState<MailboxPreset[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<MailboxForm>(EMPTY_FORM);
  const [presetId, setPresetId] = useState("custom");
  const [authMethod, setAuthMethod] = useState<"password" | "oauth2">(
    "password",
  );
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<{
    imap_ok: boolean;
    smtp_ok: boolean | null;
    imap_error?: string;
    smtp_error?: string;
    message_count?: number;
  } | null>(null);
  const [testing, setTesting] = useState(false);

  // OAuth2 connect popup — see app/api/oauth.py. `oauthSession` is the
  // not-yet-attached-to-any-mailbox result (new mailbox flow); an existing
  // mailbox's connection is written straight to its row by the callback, so
  // there we just reload the list once the popup reports success.
  const [oauthBusy, setOauthBusy] = useState(false);
  const [oauthError, setOauthError] = useState<string | null>(null);
  const [oauthSession, setOauthSession] = useState<string | null>(null);
  const [oauthEmail, setOauthEmail] = useState<string | null>(null);

  const base = getApiBaseUrl();
  const preset = presets.find((p) => p.id === presetId);

  async function load() {
    setLoading(true);
    try {
      const res = await fetch(`${base}/api/mailbox/configs`);
      if (res.ok) setMailboxes(await res.json());
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    fetch(`${base}/api/mailbox/presets`)
      .then((r) => (r.ok ? r.json() : []))
      .then(setPresets)
      .catch(() => {});
  }, []);

  // Popup → parent window handoff (see app/api/oauth.py's _popup_page).
  useEffect(() => {
    function handler(ev: MessageEvent) {
      const d = ev.data;
      if (!d || typeof d !== "object" || !d.type) return;
      if (d.type === "oauth_complete") {
        setOauthBusy(false);
        setOauthError(null);
        if (d.mailbox_id) {
          // The callback already wrote the tokens straight to this
          // mailbox's row — refresh the list, and if its edit form happens
          // to be open right now, update its connected-state badge too
          // instead of leaving it stuck on "не подключено" until reopened.
          fetch(`${base}/api/mailbox/configs/${d.mailbox_id}`)
            .then((r) => (r.ok ? r.json() : null))
            .then((mb) => {
              if (!mb) return;
              setMailboxes((prev) =>
                prev.map((m) => (m.id === mb.id ? mb : m)),
              );
              if (editing === mb.id) setOauthEmail(mb.oauth_email);
            })
            .catch(() => {});
        } else if (d.session) {
          setOauthSession(d.session);
          fetch(`${base}/api/oauth/pending/${d.session}`)
            .then((r) => (r.ok ? r.json() : null))
            .then((p) => p && setOauthEmail(p.email))
            .catch(() => {});
        }
      } else if (d.type === "oauth_error") {
        setOauthBusy(false);
        setOauthError(d.detail || "Ошибка подключения");
      }
    }
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [editing, base]);

  // Once the popup reports the connected address, use it to fill in the
  // fields the person would otherwise have to type themselves.
  useEffect(() => {
    if (!oauthEmail) return;
    setForm((f) => ({
      ...f,
      name: f.name || oauthEmail.split("@")[0],
      imap_user: f.imap_user || oauthEmail,
      smtp_user: f.smtp_user || oauthEmail,
      smtp_from_address: f.smtp_from_address || oauthEmail,
    }));
  }, [oauthEmail]);

  function applyPreset(id: string) {
    setPresetId(id);
    const p = presets.find((x) => x.id === id);
    if (!p) return;
    setForm((f) => ({
      ...f,
      imap_host: p.imap_host || f.imap_host,
      imap_port: String(p.imap_port),
      imap_ssl: p.imap_ssl,
      smtp_host: p.smtp_host || f.smtp_host,
      smtp_port: String(p.smtp_port),
      smtp_use_tls: p.smtp_use_tls,
    }));
    setAuthMethod(
      p.auth_methods.includes("oauth2") && p.oauth_configured
        ? "oauth2"
        : "password",
    );
    setOauthSession(null);
    setOauthEmail(null);
    setOauthError(null);
  }

  function openNew() {
    setEditing(null);
    setForm(EMPTY_FORM);
    setPresetId("custom");
    setAuthMethod("password");
    setOauthSession(null);
    setOauthEmail(null);
    setOauthError(null);
    setTestResult(null);
    setShowForm(true);
  }

  function openEdit(mb: MailboxOut) {
    setEditing(mb.id);
    setForm({
      name: mb.name,
      display_name: mb.display_name || "",
      imap_host: mb.imap_host,
      imap_port: String(mb.imap_port),
      imap_user: mb.imap_user,
      imap_password: "",
      imap_ssl: mb.imap_ssl,
      imap_folder: mb.imap_folder,
      smtp_host: mb.smtp_host || "",
      smtp_port: String(mb.smtp_port || 587),
      smtp_user: mb.smtp_user || "",
      smtp_password: "",
      smtp_use_tls: mb.smtp_use_tls,
      smtp_from_address: mb.smtp_from_address || "",
      smtp_from_name: mb.smtp_from_name || "",
      default_doc_type: mb.default_doc_type || "",
      assigned_role: mb.assigned_role || "",
      is_active: mb.is_active,
    });
    // Restore the provider preset so the OAuth controls (reconnect / refresh
    // token) render on edit: match by stored oauth_provider first, then by IMAP
    // host, else "custom". Without this an OAuth mailbox could only be viewed,
    // never re-authorised, from the edit form.
    const matched =
      (mb.oauth_provider &&
        presets.find((p) => p.oauth_provider === mb.oauth_provider)) ||
      presets.find((p) => p.imap_host && p.imap_host === mb.imap_host) ||
      null;
    setPresetId(matched?.id ?? "custom");
    setAuthMethod(mb.auth_method === "oauth2" ? "oauth2" : "password");
    setOauthSession(null);
    setOauthEmail(mb.oauth_email);
    setOauthError(null);
    setTestResult(null);
    setShowForm(true);
  }

  async function connectOAuth(provider: string, mailboxId?: string) {
    setOauthBusy(true);
    setOauthError(null);
    try {
      const res = await mutFetch(`${base}/api/oauth/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, mailbox_id: mailboxId ?? null }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.detail ?? `HTTP ${res.status}`);
      const popup = window.open(
        d.authorize_url,
        "oauth_connect_mailbox",
        "width=520,height=680",
      );
      if (!popup) {
        setOauthError(
          "Браузер заблокировал всплывающее окно — разрешите его для этого сайта и попробуйте снова",
        );
        setOauthBusy(false);
      }
    } catch (e: unknown) {
      setOauthError(e instanceof Error ? e.message : String(e));
      setOauthBusy(false);
    }
  }

  async function save() {
    setSaving(true);
    try {
      const body: Record<string, unknown> = {
        ...form,
        imap_port: parseInt(form.imap_port) || 993,
        smtp_port: parseInt(form.smtp_port) || 587,
        imap_password:
          authMethod === "oauth2" ? undefined : form.imap_password || undefined,
        smtp_password:
          authMethod === "oauth2" ? undefined : form.smtp_password || undefined,
        smtp_host: form.smtp_host || undefined,
        smtp_user: form.smtp_user || undefined,
        smtp_from_address: form.smtp_from_address || undefined,
        smtp_from_name: form.smtp_from_name || undefined,
        display_name: form.display_name || undefined,
        default_doc_type: form.default_doc_type || undefined,
        assigned_role: form.assigned_role || undefined,
      };
      if (!editing) {
        // New mailbox: OAuth tokens (if any) travel via the one-time session
        // from the connect popup — see app/api/mailbox.py's create_mailbox.
        if (authMethod === "oauth2" && oauthSession)
          body.oauth_session = oauthSession;
      } else if (authMethod === "password") {
        // Editing and switched back to password — tell the backend to drop
        // the now-stale OAuth tokens (app/api/mailbox.py's update_mailbox).
        body.auth_method = "password";
      }
      const res = editing
        ? await mutFetch(`${base}/api/mailbox/configs/${editing}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          })
        : await mutFetch(`${base}/api/mailbox/configs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
      if (res.ok) {
        setShowForm(false);
        await load();
      }
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: string) {
    if (!confirm("Удалить почтовый ящик?")) return;
    await mutFetch(`${base}/api/mailbox/configs/${id}`, { method: "DELETE" });
    await load();
  }

  async function testConnection(id: string) {
    setTesting(true);
    setTestResult(null);
    try {
      const res = await mutFetch(`${base}/api/mailbox/configs/${id}/test`, {
        method: "POST",
      });
      if (res.ok) setTestResult(await res.json());
    } finally {
      setTesting(false);
    }
  }

  const f = (key: keyof MailboxForm, value: string | boolean) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const oauthSupported = !!preset?.auth_methods.includes("oauth2");
  const oauthProvider =
    preset?.oauth_provider ||
    (editing
      ? (mailboxes.find((m) => m.id === editing)?.oauth_provider ?? null)
      : null);
  // New mailbox needs a completed popup session; an existing one that's
  // already connected (or being reconnected) doesn't block Save on it.
  const oauthBlocksSave = authMethod === "oauth2" && !editing && !oauthSession;

  return (
    <div className="rounded-xl border border-slate-700 bg-slate-800 p-5 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-base font-semibold text-slate-100">
            Почтовые ящики
          </h3>
          <p className="text-xs text-slate-400 mt-0.5">
            IMAP/SMTP ящики для получения и отправки писем
          </p>
        </div>
        <button
          onClick={openNew}
          className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg"
        >
          + Добавить
        </button>
      </div>

      {loading ? (
        <p className="text-slate-500 text-sm">Загрузка...</p>
      ) : mailboxes.length === 0 ? (
        <p className="text-slate-500 text-sm">Ящики не настроены</p>
      ) : (
        <div className="space-y-2">
          {mailboxes.map((mb) => (
            <div
              key={mb.id}
              className="flex items-center gap-3 rounded-lg border border-slate-700 bg-slate-900 px-3 py-2"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-slate-200">
                    {mb.display_name || mb.name}
                  </span>
                  <span
                    className={`text-[10px] px-1.5 py-0.5 rounded-full ${mb.is_active ? "bg-emerald-900/40 text-emerald-400" : "bg-slate-700 text-slate-400"}`}
                  >
                    {mb.is_active ? "активен" : "неактивен"}
                  </span>
                  {mb.auth_method === "oauth2" ? (
                    <span
                      className={`text-[10px] px-1.5 py-0.5 rounded-full ${mb.oauth_connected ? "bg-blue-900/40 text-blue-400" : "bg-amber-900/40 text-amber-400"}`}
                    >
                      OAuth2{mb.oauth_connected ? " ✓" : " — не подключено"}
                    </span>
                  ) : null}
                  {mb.sync_error && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-red-900/40 text-red-400">
                      ошибка
                    </span>
                  )}
                </div>
                <p className="text-xs text-slate-500 truncate">
                  {mb.oauth_email || mb.imap_user} @ {mb.imap_host}
                  {mb.last_sync_at && (
                    <span className="ml-2">
                      синхр. {new Date(mb.last_sync_at).toLocaleString("ru-RU")}
                    </span>
                  )}
                </p>
              </div>
              <div className="flex items-center gap-1 shrink-0">
                <button
                  onClick={() => testConnection(mb.id)}
                  disabled={testing}
                  className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded"
                >
                  Тест
                </button>
                <button
                  onClick={() => openEdit(mb)}
                  className="px-2 py-1 text-xs bg-slate-700 hover:bg-slate-600 text-slate-300 rounded"
                >
                  Изм.
                </button>
                <button
                  onClick={() => remove(mb.id)}
                  className="px-2 py-1 text-xs bg-red-900/30 hover:bg-red-900/50 text-red-400 rounded"
                >
                  ✕
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {testResult && (
        <div className="rounded border border-slate-600 bg-slate-900 p-3 text-xs space-y-1">
          <div
            className={testResult.imap_ok ? "text-emerald-400" : "text-red-400"}
          >
            IMAP: {testResult.imap_ok ? "✓ ОК" : "✗ ошибка"}
            {testResult.imap_ok &&
              testResult.message_count != null &&
              ` (${testResult.message_count} сообщений)`}
            {testResult.imap_error && ` — ${testResult.imap_error}`}
          </div>
          {testResult.smtp_ok !== null && testResult.smtp_ok !== undefined && (
            <div
              className={
                testResult.smtp_ok ? "text-emerald-400" : "text-red-400"
              }
            >
              SMTP: {testResult.smtp_ok ? "✓ ОК" : "✗ ошибка"}
              {testResult.smtp_error && ` — ${testResult.smtp_error}`}
            </div>
          )}
        </div>
      )}

      {showForm && (
        <div className="rounded-lg border border-slate-600 bg-slate-900 p-4 space-y-4">
          <h4 className="text-sm font-semibold text-slate-200">
            {editing ? "Редактировать ящик" : "Новый ящик"}
          </h4>

          <div>
            <label className="block text-xs text-slate-400 mb-1">
              Тип почты
            </label>
            <select
              className={inputCls}
              value={presetId}
              onChange={(e) => applyPreset(e.target.value)}
            >
              <option value="custom">Другой / корпоративный сервер</option>
              {presets
                .filter((p) => p.id !== "custom")
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
            </select>
            {preset && (
              <p className="mt-1.5 text-xs text-slate-400 leading-relaxed">
                {preset.hint}
              </p>
            )}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Имя (уникальное)
              </label>
              <input
                className={inputCls}
                value={form.name}
                onChange={(e) => f("name", e.target.value)}
                placeholder="procurement"
                disabled={!!editing}
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 mb-1">
                Отображаемое имя
              </label>
              <input
                className={inputCls}
                value={form.display_name}
                onChange={(e) => f("display_name", e.target.value)}
                placeholder="Отдел закупок"
              />
            </div>
          </div>

          {oauthSupported && (
            <div className="rounded border border-slate-700 bg-slate-800/60 p-3 space-y-2">
              <div className="flex items-center gap-4 text-xs">
                <label className="flex items-center gap-1.5 text-slate-300 cursor-pointer">
                  <input
                    type="radio"
                    checked={authMethod === "oauth2"}
                    onChange={() => setAuthMethod("oauth2")}
                    disabled={!preset?.oauth_configured}
                  />
                  OAuth2 (рекомендуется)
                  {!preset?.oauth_configured && (
                    <span className="text-slate-500">
                      — не настроено администратором
                    </span>
                  )}
                </label>
                <label className="flex items-center gap-1.5 text-slate-300 cursor-pointer">
                  <input
                    type="radio"
                    checked={authMethod === "password"}
                    onChange={() => setAuthMethod("password")}
                  />
                  Пароль / пароль приложения
                </label>
              </div>

              {authMethod === "oauth2" && oauthProvider && (
                <div className="space-y-1.5">
                  {oauthEmail ? (
                    <p className="text-xs text-emerald-400">
                      ✓ Подключено: {oauthEmail}
                    </p>
                  ) : (
                    <p className="text-xs text-amber-400">
                      Ящик ещё не подключён — нажмите кнопку ниже.
                    </p>
                  )}
                  <button
                    type="button"
                    onClick={() =>
                      connectOAuth(oauthProvider, editing ?? undefined)
                    }
                    disabled={oauthBusy || !preset?.oauth_configured}
                    className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-xs rounded-lg"
                  >
                    {oauthBusy
                      ? "Ждём подтверждения..."
                      : oauthEmail
                        ? `Переподключить через ${OAUTH_PROVIDER_LABEL[oauthProvider] ?? oauthProvider}`
                        : `Войти через ${OAUTH_PROVIDER_LABEL[oauthProvider] ?? oauthProvider}`}
                  </button>
                  {oauthError && (
                    <p className="text-xs text-red-400">{oauthError}</p>
                  )}
                </div>
              )}
            </div>
          )}

          <div>
            <p className="text-xs font-medium text-slate-300 mb-2">
              IMAP (входящие)
            </p>
            <div className="grid grid-cols-3 gap-3">
              <div className="col-span-2">
                <label className="block text-xs text-slate-400 mb-1">
                  Сервер
                </label>
                <input
                  className={inputCls}
                  value={form.imap_host}
                  onChange={(e) => f("imap_host", e.target.value)}
                  placeholder="imap.example.com"
                />
              </div>
              <div>
                <label className="block text-xs text-slate-400 mb-1">
                  Порт
                </label>
                <input
                  className={inputCls}
                  value={form.imap_port}
                  onChange={(e) => f("imap_port", e.target.value)}
                  placeholder="993"
                />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 mt-2">
              <div>
                <label className="block text-xs text-slate-400 mb-1">
                  Логин
                </label>
                <input
                  className={inputCls}
                  value={form.imap_user}
                  onChange={(e) => f("imap_user", e.target.value)}
                  placeholder="user@example.com"
                />
              </div>
              {authMethod !== "oauth2" && (
                <div>
                  <label className="block text-xs text-slate-400 mb-1">
                    Пароль {editing && "(пусто = не менять)"}
                  </label>
                  <input
                    className={inputCls}
                    type="password"
                    value={form.imap_password}
                    onChange={(e) => f("imap_password", e.target.value)}
                    placeholder="••••••••"
                  />
                </div>
              )}
            </div>
            <div className="flex items-center gap-4 mt-2">
              <label className="flex items-center gap-1.5 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.imap_ssl}
                  onChange={(e) => f("imap_ssl", e.target.checked)}
                  className="rounded"
                />
                SSL
              </label>
              <div className="flex-1">
                <input
                  className={inputCls}
                  value={form.imap_folder}
                  onChange={(e) => f("imap_folder", e.target.value)}
                  placeholder="Папка (INBOX)"
                />
              </div>
            </div>
          </div>

          <details className="group">
            <summary className="text-xs font-medium text-slate-400 cursor-pointer hover:text-slate-200">
              SMTP (исходящие) — необязательно
            </summary>
            <div className="mt-3 space-y-3">
              <div className="grid grid-cols-3 gap-3">
                <div className="col-span-2">
                  <label className="block text-xs text-slate-400 mb-1">
                    SMTP сервер
                  </label>
                  <input
                    className={inputCls}
                    value={form.smtp_host}
                    onChange={(e) => f("smtp_host", e.target.value)}
                    placeholder="smtp.example.com"
                  />
                </div>
                <div>
                  <label className="block text-xs text-slate-400 mb-1">
                    Порт
                  </label>
                  <input
                    className={inputCls}
                    value={form.smtp_port}
                    onChange={(e) => f("smtp_port", e.target.value)}
                    placeholder="587"
                  />
                </div>
              </div>
              <label className="flex items-center gap-1.5 text-xs text-slate-300 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.smtp_use_tls}
                  onChange={(e) => f("smtp_use_tls", e.target.checked)}
                  className="rounded"
                />
                STARTTLS (обычно порт 587)
                <span className="text-slate-500">
                  — выключите для порта 465 (неявный TLS/SSL)
                </span>
              </label>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-slate-400 mb-1">
                    Логин SMTP
                  </label>
                  <input
                    className={inputCls}
                    value={form.smtp_user}
                    onChange={(e) => f("smtp_user", e.target.value)}
                  />
                </div>
                {authMethod !== "oauth2" && (
                  <div>
                    <label className="block text-xs text-slate-400 mb-1">
                      Пароль SMTP
                    </label>
                    <input
                      className={inputCls}
                      type="password"
                      value={form.smtp_password}
                      onChange={(e) => f("smtp_password", e.target.value)}
                      placeholder="••••••••"
                    />
                  </div>
                )}
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs text-slate-400 mb-1">
                    От кого (email)
                  </label>
                  <input
                    className={inputCls}
                    value={form.smtp_from_address}
                    onChange={(e) => f("smtp_from_address", e.target.value)}
                    placeholder="noreply@example.com"
                  />
                </div>
                <div>
                  <label className="block text-xs text-slate-400 mb-1">
                    От кого (имя)
                  </label>
                  <input
                    className={inputCls}
                    value={form.smtp_from_name}
                    onChange={(e) => f("smtp_from_name", e.target.value)}
                    placeholder="Отдел закупок"
                  />
                </div>
              </div>
            </div>
          </details>

          <div className="flex items-center gap-2 pt-2">
            <button
              onClick={save}
              disabled={
                saving ||
                !form.name ||
                !form.imap_host ||
                !form.imap_user ||
                oauthBlocksSave
              }
              title={
                oauthBlocksSave
                  ? "Сначала подключите аккаунт кнопкой выше"
                  : undefined
              }
              className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm rounded-lg"
            >
              {saving ? "Сохранение..." : "Сохранить"}
            </button>
            <button
              onClick={() => setShowForm(false)}
              className="px-4 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-200 text-sm rounded-lg"
            >
              Отмена
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
