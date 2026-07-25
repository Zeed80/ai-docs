"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getApiBaseUrl } from "@/lib/api-base";
import { csrfHeaders } from "@/lib/auth";
import { ProtectedRoute } from "@/components/auth/protected-route";

const API = getApiBaseUrl();

const ROLE_LABELS: Record<string, string> = {
  admin: "Администратор",
  manager: "Менеджер",
  accountant: "Бухгалтер",
  buyer: "Закупщик",
  engineer: "Инженер",
  technologist: "Технолог",
  viewer: "Наблюдатель",
};

interface UserOut {
  sub: string;
  email: string;
  name: string;
  preferred_username: string;
  role: string;
  is_active: boolean;
  last_seen_at: string | null;
  created_at: string;
  department_id: string | null;
  manager_sub: string | null;
  title: string | null;
  section_access: string[] | null;
}

interface DepartmentOut {
  id: string;
  name: string;
  code: string;
}

interface SectionCatalogItem {
  key: string;
  label: string;
  href: string;
}
interface SectionCatalogGroup {
  key: string;
  label: string;
  items: SectionCatalogItem[];
}

function SectionAccessSection({
  userSub,
  initial,
  isAdminUser,
}: {
  userSub: string;
  initial: string[] | null;
  isAdminUser: boolean;
}) {
  const [catalog, setCatalog] = useState<SectionCatalogGroup[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set(initial ?? []));
  const [baseline, setBaseline] = useState<Set<string>>(new Set(initial ?? []));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/admin/sections/catalog`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { groups: [] }))
      .then((d) => setCatalog(d.groups ?? []))
      .catch(() => setCatalog([]));
  }, []);

  function toggle(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleGroup(group: SectionCatalogGroup, checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const it of group.items) {
        if (checked) next.add(it.key);
        else next.delete(it.key);
      }
      return next;
    });
  }

  const isDirty =
    selected.size !== baseline.size ||
    [...selected].some((k) => !baseline.has(k));

  async function save() {
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({ section_access: [...selected] }),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const updated: UserOut = await res.json();
      setBaseline(new Set(updated.section_access ?? []));
      setSelected(new Set(updated.section_access ?? []));
      setSuccess(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <div>
        <h3 className="text-sm font-medium">Доступ к разделам</h3>
        <p className="text-[10px] text-muted-foreground mt-0.5">
          Отмеченные разделы видны и доступны пользователю. Неотмеченные скрыты
          из меню и заблокированы.
        </p>
      </div>

      {isAdminUser && (
        <p className="text-xs text-amber-600">
          Администратор видит все разделы независимо от этих настроек.
        </p>
      )}

      <div className="space-y-3">
        {catalog.map((group) => {
          const checkedCount = group.items.filter((it) =>
            selected.has(it.key),
          ).length;
          const allChecked = checkedCount === group.items.length;
          return (
            <div key={group.key} className="space-y-1">
              <label className="flex items-center gap-2 text-xs font-semibold">
                <input
                  type="checkbox"
                  checked={allChecked}
                  ref={(el) => {
                    if (el) el.indeterminate = checkedCount > 0 && !allChecked;
                  }}
                  onChange={(e) => toggleGroup(group, e.target.checked)}
                />
                {group.label}
              </label>
              <div className="pl-5 grid grid-cols-2 gap-x-3 gap-y-1">
                {group.items.map((it) => (
                  <label
                    key={it.key}
                    className="flex items-center gap-2 text-xs text-muted-foreground"
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(it.key)}
                      onChange={() => toggle(it.key)}
                    />
                    {it.label}
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {error && <p className="text-xs text-destructive">{error}</p>}
      {success && (
        <p className="text-xs text-green-600">Доступ к разделам сохранён</p>
      )}

      <button
        onClick={save}
        disabled={saving || !isDirty}
        className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
      >
        {saving ? "Сохранение..." : "Сохранить разделы"}
      </button>
    </div>
  );
}

interface UserMailboxOut {
  address: string | null;
  is_active: boolean | null;
  webmail_url: string | null;
  last_sync_at: string | null;
  sync_error: string | null;
  sweep_enabled: boolean | null;
  quota_mb: number | null;
}

interface UserMailboxProvisionedOut {
  address: string;
  generated_password: string;
}

function MailboxSection({ userSub }: { userSub: string }) {
  const [mailbox, setMailbox] = useState<UserMailboxOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [localPart, setLocalPart] = useState("");
  const [quotaMb, setQuotaMb] = useState("");
  const [busy, setBusy] = useState(false);
  // Destroying a mailbox destroys all of its mail, so the address must be typed
  // out — a one-click confirm is not a proportionate guard for that.
  const [deleteMode, setDeleteMode] = useState(false);
  const [confirmAddress, setConfirmAddress] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [revealed, setRevealed] = useState<UserMailboxProvisionedOut | null>(
    null,
  );

  function load() {
    setLoading(true);
    fetch(`${API}/api/admin/users/${encodeURIComponent(userSub)}/mailbox`, {
      credentials: "include",
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d: UserMailboxOut | null) => setMailbox(d))
      .catch(() => setMailbox(null))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userSub]);

  async function provision() {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}/mailbox`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({
            local_part: localPart.trim() || null,
            quota_mb: quotaMb.trim() ? Number(quotaMb) : null,
          }),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const d: UserMailboxProvisionedOut = await res.json();
      setRevealed(d);
      setLocalPart("");
      load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function resetPassword() {
    if (!window.confirm("Сбросить пароль почтового ящика?")) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}/mailbox/reset-password`,
        { method: "POST", credentials: "include", headers: csrfHeaders() },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const d: UserMailboxProvisionedOut = await res.json();
      setRevealed(d);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(deleteOnServer: boolean) {
    if (!deleteOnServer) {
      if (
        !window.confirm(
          `Отключить ящик ${mailbox?.address}? Приём почты и сбор агентом остановятся, письма сохранятся.`,
        )
      )
        return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}/mailbox/revoke`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({
            delete_on_server: deleteOnServer,
            confirm_address: deleteOnServer ? confirmAddress.trim() : null,
          }),
        },
      );
      if (!res.ok && res.status !== 204) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setRevealed(null);
      setDeleteMode(false);
      setConfirmAddress("");
      load();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function toggleSweep(enabled: boolean) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}/mailbox/sweep`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({ sweep_enabled: enabled }),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setMailbox(await res.json());
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <h3 className="text-sm font-medium">Корпоративная почта</h3>

      {loading ? (
        <p className="text-xs text-muted-foreground">Загрузка...</p>
      ) : mailbox?.address ? (
        <div className="space-y-2">
          <p className="text-sm">
            <span className="font-mono">{mailbox.address}</span>{" "}
            {mailbox.is_active === false && (
              <span className="text-amber-600 text-xs">(отозван)</span>
            )}
          </p>
          {mailbox.webmail_url && (
            <a
              href={mailbox.webmail_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-primary hover:underline"
            >
              Открыть вебмейл ↗
            </a>
          )}
          {mailbox.quota_mb ? (
            <p className="text-xs text-muted-foreground">
              Квота: {mailbox.quota_mb} МБ
            </p>
          ) : null}
          {mailbox.sync_error && (
            <p className="text-xs text-destructive">
              Ошибка синхронизации: {mailbox.sync_error}
            </p>
          )}

          <label className="flex items-start gap-2 text-xs">
            <input
              type="checkbox"
              checked={!!mailbox.sweep_enabled}
              disabled={busy}
              onChange={(e) => toggleSweep(e.target.checked)}
              className="mt-0.5"
            />
            <span>
              Разрешить ИИ-сотруднику читать этот ящик
              <span className="block text-muted-foreground">
                Письма и вложения попадут в обработку (распознавание счетов и
                т.п.). Личная переписка остаётся видимой только владельцу; сам
                сотрудник может отключить это в своих настройках.
              </span>
            </span>
          </label>

          <div className="flex flex-wrap gap-2 pt-1">
            <button
              onClick={resetPassword}
              disabled={busy}
              className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted disabled:opacity-50"
            >
              Сбросить пароль
            </button>
            <button
              onClick={() => revoke(false)}
              disabled={busy}
              className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted disabled:opacity-50"
              title="Ящик перестаёт принимать почту, письма сохраняются"
            >
              Отключить ящик
            </button>
            <button
              onClick={() => setDeleteMode((v) => !v)}
              disabled={busy}
              className="px-3 py-1.5 rounded border border-destructive text-destructive text-sm hover:bg-destructive/10 disabled:opacity-50"
            >
              Удалить безвозвратно…
            </button>
          </div>

          {deleteMode && (
            <div className="rounded border border-destructive p-3 space-y-2">
              <p className="text-xs text-destructive">
                Ящик и <strong>вся переписка</strong> будут удалены на почтовом
                сервере. Восстановить можно только из бэкапа. Для подтверждения
                введите адрес полностью:
              </p>
              <input
                type="text"
                value={confirmAddress}
                onChange={(e) => setConfirmAddress(e.target.value)}
                placeholder={mailbox.address ?? ""}
                className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
              />
              <div className="flex gap-2">
                <button
                  onClick={() => revoke(true)}
                  disabled={
                    busy ||
                    confirmAddress.trim().toLowerCase() !==
                      (mailbox.address ?? "").toLowerCase()
                  }
                  className="px-3 py-1.5 rounded bg-destructive text-white text-sm disabled:opacity-50"
                >
                  Удалить ящик и почту
                </button>
                <button
                  onClick={() => {
                    setDeleteMode(false);
                    setConfirmAddress("");
                  }}
                  className="px-3 py-1.5 rounded border border-border text-sm hover:bg-muted"
                >
                  Отмена
                </button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            У пользователя нет личного почтового ящика.
          </p>
          <input
            type="text"
            value={localPart}
            onChange={(e) => setLocalPart(e.target.value)}
            placeholder="Локальная часть адреса (не заполните — предложим сами)"
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
          />
          <input
            type="number"
            min={0}
            value={quotaMb}
            onChange={(e) => setQuotaMb(e.target.value)}
            placeholder="Квота, МБ (пусто — значение по умолчанию из настроек интеграции)"
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          />
          <p className="text-xs text-muted-foreground">
            Сбор писем агентом при создании выключен — включите его выше после
            согласия сотрудника.
          </p>
          <button
            onClick={provision}
            disabled={busy}
            className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
          >
            {busy ? "Создание..." : "Создать почтовый ящик"}
          </button>
        </div>
      )}

      {error && <p className="text-xs text-destructive">{error}</p>}

      {revealed && (
        <div className="rounded border border-amber-500 bg-amber-50 dark:bg-amber-950 p-3 space-y-1">
          <p className="text-xs font-medium text-amber-800 dark:text-amber-200">
            Пароль показывается один раз — скопируйте и передайте сотруднику:
          </p>
          <p className="text-sm">
            <span className="font-mono">{revealed.address}</span>
          </p>
          <p className="font-mono text-sm select-all bg-background rounded px-2 py-1 border border-border">
            {revealed.generated_password}
          </p>
          <button
            onClick={() => setRevealed(null)}
            className="text-xs text-muted-foreground hover:underline"
          >
            Скрыть
          </button>
        </div>
      )}
    </div>
  );
}

function SetPasswordSection({ userSub }: { userSub: string }) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (password !== confirm) {
      setError("Пароли не совпадают");
      return;
    }
    if (password.length < 8) {
      setError("Минимум 8 символов");
      return;
    }
    setSaving(true);
    setError(null);
    setSuccess(false);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(userSub)}/set-password`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify({ password }),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      setPassword("");
      setConfirm("");
      setSuccess(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-border p-4 space-y-3">
      <h3 className="text-sm font-medium">Установить пароль</h3>
      <form onSubmit={submit} className="space-y-2">
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Новый пароль (мин. 8 символов)"
          minLength={8}
          required
          className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
        />
        <input
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="Повторите пароль"
          required
          className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
        />
        {error && <p className="text-xs text-destructive">{error}</p>}
        {success && (
          <p className="text-xs text-green-600">Пароль успешно изменён</p>
        )}
        <button
          type="submit"
          disabled={saving || !password || !confirm}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Сохранение..." : "Установить пароль"}
        </button>
      </form>
    </div>
  );
}

/**
 * useParams() returns the raw URL segment, which for subs containing ":" (all
 * SSO `authentik:N` and pre-provisioned `local:uuid` users) is still
 * percent-encoded (e.g. "local%3A…"). Encoding it again when building the API
 * URL produced a double-encoded "%253A", so the backend never found the user
 * (404/403). Decode once here so every downstream `encodeURIComponent(sub)`
 * yields a single, correct encoding. Safe for already-decoded values — our subs
 * never contain a literal "%".
 */
function decodeSub(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function UserEditContent() {
  const { sub: rawSub } = useParams<{ sub: string }>();
  const sub = decodeSub(rawSub);
  const router = useRouter();
  const [user, setUser] = useState<UserOut | null>(null);
  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [deptId, setDeptId] = useState("");
  const [title, setTitle] = useState("");
  const [managerSub, setManagerSub] = useState("");
  const [departments, setDepartments] = useState<DepartmentOut[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    fetch(`${API}/api/admin/users/${encodeURIComponent(sub)}`, {
      credentials: "include",
    })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((u: UserOut) => {
        setUser(u);
        setRole(u.role);
        setName(u.name);
        setDeptId(u.department_id ?? "");
        setTitle(u.title ?? "");
        setManagerSub(u.manager_sub ?? "");
      })
      .catch((e) => setError(e.message));

    fetch(`${API}/api/admin/departments`, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : { items: [] }))
      .then((d) => setDepartments(d.items ?? []))
      .catch(() => setDepartments([]));
  }, [sub]);

  async function save() {
    setSaving(true);
    setSuccess(false);
    setError(null);
    try {
      const changes: Record<string, unknown> = {};
      if (role !== user!.role) changes.role = role;
      if (name.trim() && name.trim() !== user!.name) changes.name = name.trim();
      // Org fields: send explicit null to clear. Compare against current values.
      if (deptId !== (user!.department_id ?? ""))
        changes.department_id = deptId || null;
      if (title !== (user!.title ?? "")) changes.title = title.trim() || null;
      if (managerSub !== (user!.manager_sub ?? ""))
        changes.manager_sub = managerSub.trim() || null;
      if (Object.keys(changes).length === 0) return;

      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(sub)}`,
        {
          method: "PATCH",
          credentials: "include",
          headers: { "Content-Type": "application/json", ...csrfHeaders() },
          body: JSON.stringify(changes),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      const updated: UserOut = await res.json();
      setUser(updated); // refresh baseline so isDirty resets
      setSuccess(true);
      router.refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function toggleActive() {
    if (!user) return;
    const res = await fetch(
      `${API}/api/admin/users/${encodeURIComponent(sub)}`,
      {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        body: JSON.stringify({ is_active: !user.is_active }),
      },
    );
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      setError(d.detail ?? `HTTP ${res.status}`);
      return;
    }
    const updated: UserOut = await res.json();
    setUser(updated);
  }

  async function deleteUser() {
    setDeleting(true);
    try {
      const res = await fetch(
        `${API}/api/admin/users/${encodeURIComponent(sub)}/deactivate`,
        {
          method: "POST",
          credentials: "include",
          headers: csrfHeaders(),
        },
      );
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail ?? `HTTP ${res.status}`);
      }
      router.push("/admin/users");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
      setDeleteConfirm(false);
    }
  }

  if (error && !user)
    return <p className="text-sm text-destructive">Ошибка: {error}</p>;
  if (!user)
    return <p className="text-sm text-muted-foreground">Загрузка...</p>;

  const isDirty =
    role !== user.role ||
    (name.trim() && name.trim() !== user.name) ||
    deptId !== (user.department_id ?? "") ||
    title !== (user.title ?? "") ||
    managerSub !== (user.manager_sub ?? "");

  return (
    <div className="max-w-md space-y-4">
      <button
        onClick={() => router.push("/admin/users")}
        className="text-xs text-muted-foreground hover:underline block"
      >
        ← Назад к пользователям
      </button>

      <div>
        <h2 className="text-base font-semibold">{user.name}</h2>
        <p className="text-sm text-muted-foreground">{user.email}</p>
        <p className="text-xs text-muted-foreground mt-0.5">
          sub: <code className="font-mono">{user.sub}</code>
        </p>
      </div>

      {/* Profile */}
      <div className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="text-sm font-medium">Профиль</h3>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Имя
          </label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          />
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Роль
          </label>
          <select
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          >
            {Object.entries(ROLE_LABELS).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Отдел
          </label>
          <select
            value={deptId}
            onChange={(e) => setDeptId(e.target.value)}
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          >
            <option value="">— не задан —</option>
            {departments.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Должность
          </label>
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Ведущий инженер"
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background"
          />
        </div>

        <div>
          <label className="text-xs text-muted-foreground block mb-1">
            Руководитель (sub)
          </label>
          <input
            type="text"
            value={managerSub}
            onChange={(e) => setManagerSub(e.target.value)}
            placeholder="sub руководителя"
            className="w-full border border-border rounded px-3 py-1.5 text-sm bg-background font-mono"
          />
          <p className="text-[10px] text-muted-foreground mt-0.5">
            Согласования по умолчанию уходят руководителю отдела
          </p>
        </div>

        <div className="text-xs text-muted-foreground space-y-1">
          <p>
            Статус:{" "}
            {user.is_active ? (
              <span className="text-green-600">Активен</span>
            ) : (
              <span className="text-red-500">Деактивирован</span>
            )}
          </p>
          <p>
            Последний вход:{" "}
            {user.last_seen_at
              ? new Date(user.last_seen_at).toLocaleString("ru")
              : "—"}
          </p>
          <p>
            Зарегистрирован:{" "}
            {new Date(user.created_at).toLocaleDateString("ru")}
          </p>
        </div>

        {error && <p className="text-xs text-destructive">{error}</p>}
        {success && (
          <p className="text-xs text-green-600">Изменения сохранены</p>
        )}

        <button
          onClick={save}
          disabled={saving || !isDirty}
          className="px-3 py-1.5 rounded bg-primary text-primary-foreground text-sm disabled:opacity-50"
        >
          {saving ? "Сохранение..." : "Сохранить"}
        </button>
      </div>

      {/* Section access */}
      <SectionAccessSection
        userSub={sub}
        initial={user.section_access}
        isAdminUser={user.role === "admin"}
      />

      {/* Personal mailbox */}
      <MailboxSection userSub={sub} />

      {/* Password */}
      <SetPasswordSection userSub={sub} />

      {/* Activate / deactivate */}
      <div className="rounded-lg border border-border p-4 space-y-3">
        <h3 className="text-sm font-medium">Доступ</h3>
        <button
          onClick={toggleActive}
          className={`px-3 py-1.5 rounded text-sm ${
            user.is_active
              ? "bg-amber-100 text-amber-700 hover:bg-amber-200"
              : "bg-green-100 text-green-700 hover:bg-green-200"
          }`}
        >
          {user.is_active ? "Деактивировать" : "Активировать"}
        </button>
        <p className="text-[10px] text-muted-foreground">
          Деактивированный пользователь не может войти, но данные сохраняются
        </p>
      </div>

      {/* Danger zone */}
      <div className="rounded-lg border border-destructive/40 p-4 space-y-3">
        <h3 className="text-sm font-medium text-destructive">Опасная зона</h3>
        {!deleteConfirm ? (
          <button
            onClick={() => setDeleteConfirm(true)}
            className="px-3 py-1.5 rounded border border-destructive text-destructive text-sm hover:bg-destructive/10"
          >
            Удалить пользователя
          </button>
        ) : (
          <div className="space-y-2">
            <p className="text-xs text-destructive">
              Пользователь будет деактивирован. Подтвердите:
            </p>
            <div className="flex gap-2">
              <button
                onClick={deleteUser}
                disabled={deleting}
                className="px-3 py-1.5 rounded bg-destructive text-destructive-foreground text-sm disabled:opacity-50"
              >
                {deleting ? "Удаление..." : "Да, удалить"}
              </button>
              <button
                onClick={() => setDeleteConfirm(false)}
                className="px-3 py-1.5 rounded border border-border text-sm"
              >
                Отмена
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function AdminUserEditPage() {
  return (
    <ProtectedRoute requiredRoles={["admin"]}>
      <UserEditContent />
    </ProtectedRoute>
  );
}
