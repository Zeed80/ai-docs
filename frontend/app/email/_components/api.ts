import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";
import type {
  EmailDraft,
  EmailLabel,
  EmailMessage,
  EmailThread,
  MailboxChip,
} from "./types";

const API = getApiBaseUrl();

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    throw new Error(d.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const emailApi = {
  mailboxes: () => apiFetch(`${API}/api/email/mailboxes`).then((r) => j<MailboxChip[]>(r)),

  labels: () => apiFetch(`${API}/api/email/labels`).then((r) => j<EmailLabel[]>(r)),

  createLabel: (name: string, color?: string) =>
    mutFetch(`${API}/api/email/labels`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, color }),
    }).then((r) => j<EmailLabel>(r)),

  deleteLabel: (id: string) =>
    mutFetch(`${API}/api/email/labels/${id}`, { method: "DELETE" }),

  threads: (params: {
    mailbox?: string;
    folder?: string;
    label_id?: string;
    is_unread?: boolean;
    is_starred?: boolean;
    limit?: number;
    cursor?: string;
  }) => {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "" && v !== null) q.set(k, String(v));
    });
    return apiFetch(`${API}/api/email/threads?${q}`).then((r) =>
      j<{ items: EmailThread[]; total: number; next_cursor: string | null }>(r),
    );
  },

  thread: (id: string) =>
    apiFetch(`${API}/api/email/threads/${id}`).then((r) => j<EmailThread>(r)),

  search: (body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) =>
      j<{ results: EmailMessage[]; total: number; next_cursor: string | null }>(r),
    ),

  bulkAction: (
    thread_ids: string[],
    action: string,
    extra: { folder?: string; label_id?: string } = {},
  ) =>
    mutFetch(`${API}/api/email/threads/actions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thread_ids, action, ...extra }),
    }).then((r) => j<{ updated: number }>(r)),

  contacts: (q: string, limit = 8) =>
    apiFetch(`${API}/api/email/contacts?q=${encodeURIComponent(q)}&limit=${limit}`).then((r) =>
      j<{
        email: string;
        name: string | null;
        organization?: string | null;
        source?: string;
        is_favorite?: boolean;
        id?: string | null;
      }[]>(r),
    ),

  contactBook: (params: { q?: string; favorites?: boolean; tag?: string }) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.favorites) qs.set("favorites", "1");
    if (params.tag) qs.set("tag", params.tag);
    return apiFetch(`${API}/api/email/contacts/book?${qs}`).then((r) =>
      j<
        {
          id: string;
          email: string;
          name: string | null;
          organization: string | null;
          phone: string | null;
          notes: string | null;
          tags: string[];
          is_favorite: boolean;
          source: string;
          use_count: number;
        }[]
      >(r),
    );
  },

  contactTags: () =>
    apiFetch(`${API}/api/email/contacts/tags`).then((r) => j<string[]>(r)),

  createContact: (body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/contacts/book`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<{ id: string }>(r)),

  updateContact: (id: string, body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/contacts/book/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<unknown>(r)),

  deleteContact: (id: string) =>
    mutFetch(`${API}/api/email/contacts/book/${id}`, { method: "DELETE" }),

  importContacts: (csv: string) =>
    mutFetch(`${API}/api/email/contacts/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ csv }),
    }).then((r) =>
      j<{
        added: number;
        updated: number;
        skipped: number;
        skipped_rows: { line: number; value: string; reason: string }[];
      }>(r),
    ),

  exportContactsUrl: () => `${API}/api/email/contacts/export`,

  folderCounts: (mailbox?: string) =>
    apiFetch(
      `${API}/api/email/folder-counts${mailbox ? `?mailbox=${encodeURIComponent(mailbox)}` : ""}`,
    ).then((r) => j<{ folder: string; total: number; unread: number }[]>(r)),

  resolveSignature: (mailbox?: string) =>
    apiFetch(
      `${API}/api/email/signatures/resolve${mailbox ? `?mailbox=${encodeURIComponent(mailbox)}` : ""}`,
    ).then(async (r) => (r.ok ? ((await r.json()) as { body_html: string } | null) : null)),

  drafts: (mailbox?: string) =>
    apiFetch(
      `${API}/api/email/drafts${mailbox ? `?mailbox=${encodeURIComponent(mailbox)}` : ""}`,
    ).then((r) => j<EmailDraft[]>(r)),

  draft: (id: string) =>
    apiFetch(`${API}/api/email/drafts/${id}`).then((r) => j<EmailDraft>(r)),

  deleteDraft: (id: string) =>
    mutFetch(`${API}/api/email/drafts/${id}`, { method: "DELETE" }).then((r) => {
      if (!r.ok && r.status !== 204) throw new Error(`HTTP ${r.status}`);
    }),

  createDraft: (body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/drafts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<EmailDraft>(r)),

  updateDraft: (id: string, body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/drafts/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<EmailDraft>(r)),

  send: (body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) =>
      j<{
        draft_id: string;
        status: "queued" | "blocked";
        blocked_by: { code: string; message: string }[];
        warnings: { code: string; message: string }[];
        undo_seconds: number;
      }>(r),
    ),

  cancelSend: (draftId: string) =>
    mutFetch(`${API}/api/email/drafts/${draftId}/cancel-send`, { method: "POST" }).then(
      (r) => j<{ status: string }>(r),
    ),

  sendDraft: (id: string, expectedDigest: string) =>
    mutFetch(`${API}/api/email/drafts/${id}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Без expected_digest сервер отвечает 400 digest_required: подтверждают
      // конкретный текст письма, а не его идентификатор.
      body: JSON.stringify({ expected_digest: expectedDigest }),
    }).then((r) => j<{ status: string }>(r)),

  riskCheckDraft: (id: string) =>
    mutFetch(`${API}/api/email/drafts/${id}/risk-check`, { method: "POST" }).then((r) =>
      j<{ is_safe: boolean; flags: { code: string; severity: string; message: string }[] }>(r),
    ),

  templates: () =>
    apiFetch(`${API}/api/email-templates/`).then((r) =>
      j<{ id: string; name: string; subject: string | null; category?: string | null }[]>(r),
    ),

  renderTemplate: (id: string, variables: Record<string, string>) =>
    mutFetch(`${API}/api/email-templates/${id}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variables }),
    }).then((r) => j<{ subject: string; body_html: string; body_text: string }>(r)),

  uploadAttachment: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return mutFetch(`${API}/api/email/attachments/upload`, {
      method: "POST",
      body: fd,
    }).then((r) => j<{ id: string; filename: string; size: number | null }>(r));
  },

  startComposeAssist: (body: Record<string, unknown>) =>
    mutFetch(`${API}/api/email/compose/assist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<{ task_id: string }>(r)),

  pollComposeAssist: (taskId: string) =>
    apiFetch(`${API}/api/email/compose/assist/${taskId}`).then((r) =>
      j<{
        status: "pending" | "done" | "error";
        error?: string;
        progress?: string[];
        result?: {
          subject: string;
          body_html: string;
          body_text: string;
          diff: { op: string; text: string }[];
          notes: string[];
          tone: string;
        };
      }>(r),
    ),

  /** Real task state, so "синхронизирую" stops when the sync actually does. */
  taskStatus: (taskId: string) =>
    apiFetch(`${API}/api/tasks/${taskId}`).then((r) =>
      j<{ status: string; result?: unknown }>(r),
    ),

  syncMailbox: (mailbox: string | null) =>
    mutFetch(`${API}/api/email/fetch`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mailbox }),
    }),

  processAttachment: (messageId: string, filename: string, target: "document" | "drawing") =>
    mutFetch(`${API}/api/email/messages/${messageId}/attachments/process`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename, target }),
    }).then((r) => j<{ document_id: string; task_id: string | null }>(r)),

  /** Ф5.3 — "показать оригинал": the raw RFC822 source of one message. */
  /** Ф6.4/6.8 — a human disagreeing with the classification is the training signal. */
  correctTriage: (messageId: string, category: string) =>
    mutFetch(`${API}/api/email/messages/${messageId}/triage/correct`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ category }),
    }).then((r) => j<NonNullable<EmailMessage["triage"]>>(r)),

  rawUrl: (messageId: string) => `${API}/api/email/messages/${messageId}/raw`,

  /** Ф1.4 — настройки чтения почты этого пользователя. */
  preferences: () =>
    apiFetch(`${API}/api/email/preferences`).then((r) =>
      j<{ always_show_images: boolean }>(r),
    ),

  setPreferences: (body: { always_show_images: boolean }) =>
    mutFetch(`${API}/api/email/preferences`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => j<{ always_show_images: boolean }>(r)),

  /** Ф1.4 — «всегда показывать картинки этого отправителя». */
  trustSenderImages: (email: string, name: string | null, trust = true) =>
    mutFetch(`${API}/api/email/contacts/trust-images`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, name, trust }),
    }).then((r) => j<{ id: string; email: string }>(r)),

  /** Ф5.3 — все вложения письма одним архивом. */
  attachmentsArchiveUrl: (messageId: string) =>
    `${API}/api/email/messages/${messageId}/attachments/archive`,

  attachmentUrl: (messageId: string, filename: string) =>
    `${API}/api/email/messages/${messageId}/attachments/${encodeURIComponent(filename)}/content`,
};
