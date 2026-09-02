export interface MailboxChip {
  name: string;
  display_name: string | null;
  is_personal: boolean;
  thread_count: number;
  message_count: number;
  unread_count: number;
  last_sync_at: string | null;
  sync_error: string | null;
}

export interface EmailLabel {
  id: string;
  name: string;
  color: string | null;
  is_system: boolean;
  thread_count: number;
}

export interface EmailAttachment {
  id: string;
  filename: string;
  content_type: string | null;
  size: number | null;
  is_inline: boolean;
  document_id: string | null;
}

export interface EmailMessage {
  id: string;
  thread_id: string | null;
  message_id_header: string | null;
  mailbox: string;
  from_address: string;
  to_addresses: string[] | null;
  cc_addresses: string[] | null;
  subject: string | null;
  body_text: string | null;
  body_html: string | null;
  body_html_sanitized: string | null;
  // Ф1.2 — headers the parser used to discard.
  reply_to?: string | null;
  body_text_derived?: boolean;
  // Ф6.4 — what the agent understood about this letter and what it did.
  triage?: {
    category: string;
    category_label: string;
    confidence: number | null;
    summary: string | null;
    entities: Record<string, unknown>;
    performed: { type: string; [k: string]: unknown }[];
    proposed: { type: string; hint?: string; [k: string]: unknown }[];
    model_name: string | null;
    corrected_category: string | null;
    status: string;
  } | null;
  derived_invoices?: {
    invoice_id: string;
    document_id: string;
    invoice_number: string | null;
    total_amount: number | null;
    currency: string | null;
    status: string;
    supplier_name: string | null;
    supplier_matched_by: string | null;
  }[];
  headers_meta?: {
    auth?: { spf?: string; dkim?: string; dmarc?: string };
    list_unsubscribe?: string;
    list_id?: string;
  } | null;
  sent_at: string | null;
  received_at: string | null;
  has_attachments: boolean;
  attachment_count: number;
  attachments: EmailAttachment[];
  is_inbound: boolean;
  is_read: boolean;
  is_starred: boolean;
  folder: string;
  snippet: string | null;
  /** Ф1.4 — картинки этого отправителя разрешено показать сразу. */
  images_trusted?: boolean;
}

export interface EmailThread {
  id: string;
  subject: string;
  mailbox: string;
  message_count: number;
  last_message_at: string | null;
  created_at: string;
  is_read: boolean;
  is_starred: boolean;
  has_attachments: boolean;
  folder: string;
  last_snippet: string | null;
  unread_count: number;
  labels: EmailLabel[];
  sender: string | null;
  messages: EmailMessage[];
}

export interface EmailDraft {
  id: string;
  to_addresses: string[];
  cc_addresses: string[] | null;
  bcc_addresses: string[] | null;
  subject: string;
  body_html: string | null;
  body_text: string | null;
  thread_id: string | null;
  mailbox: string | null;
  status: string;
  risk_flags: { code: string; severity: string; message: string; can_override?: boolean }[];
  attachment_ids: string[];
  // Отпечаток содержимого письма: обязателен при отправке существующего
  // черновика — он привязывает подтверждение к тексту, а не к идентификатору.
  content_digest: string | null;
  created_at: string;
}

export type ComposeMode =
  // attachmentIds: files already staged elsewhere (mobile "Поделиться" flow).
  | { kind: "new"; to?: string[]; attachmentIds?: string[] }
  // assist: сразу попросить агента подготовить ответ («Света, ответь»).
  | { kind: "reply"; message: EmailMessage; all?: boolean; assist?: string }
  | { kind: "forward"; message: EmailMessage }
  | { kind: "draft"; draft: EmailDraft };
