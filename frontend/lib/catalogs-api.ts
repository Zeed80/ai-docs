"use client";

import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";

const API = getApiBaseUrl();

export interface CatalogSummary {
  // null for the "Без привязки" pseudo-catalog: positions imported before
  // page-wise parsing have no file behind them.
  document_id: string | null;
  file_name: string;
  file_size: number;
  uploaded_at: string | null;
  supplier_id: string | null;
  supplier_name: string | null;
  party_id: string | null;
  page_count: number;
  pages_ready: number;
  entries_count: number;
  entries_with_image: number;
  status: string;
  current_step: string | null;
  error: string | null;
  progress_done: number;
  progress_total: number;
  cover_url: string | null;
  download_url: string | null;
  is_archive: boolean;
  legacy?: boolean;
  paused?: boolean;
}

export interface CatalogPageInfo {
  page_number: number;
  status: string;
  skip_reason: string | null;
  entries_count: number;
  width: number | null;
  height: number | null;
  thumb_url: string | null;
  image_url: string | null;
}

export interface CatalogEntry {
  id: string;
  part_number: string | null;
  name: string;
  description: string | null;
  tool_type: string;
  diameter_mm: number | null;
  length_mm: number | null;
  material: string | null;
  coating: string | null;
  price_value: number | null;
  price_currency: string;
  unit: string | null;
  supplier_id: string | null;
  supplier_name: string | null;
  catalog_document_id: string | null;
  catalog_name: string | null;
  page_number: number | null;
  image_url: string | null;
  thumb_url: string | null;
  image_kind: string | null;
  image_bbox: { x: number; y: number; w: number; h: number } | null;
  score: number | null;
  legacy: boolean;
}

export interface CatalogFacetValue {
  key: string;
  label: string;
  count: number;
}

export interface CatalogSearchResult {
  items: CatalogEntry[];
  total: number;
  page: number;
  page_size: number;
  facets: {
    suppliers: CatalogFacetValue[];
    catalogs: CatalogFacetValue[];
    tool_types: CatalogFacetValue[];
    with_price: number;
    with_image: number;
  } | null;
  diagnostics: Record<string, unknown>;
}

export interface CatalogSearchParams {
  query?: string;
  party_id?: string;
  supplier_id?: string;
  catalog_document_id?: string;
  catalog_document_ids?: string[];
  supplier_ids?: string[];
  party_ids?: string[];
  page_number?: number;
  tool_type?: string;
  has_price?: boolean;
  has_image?: boolean;
  page?: number;
  page_size?: number;
  include_facets?: boolean;
}

export interface CatalogPageHit {
  page_number: number;
  snippet: string;
  entries_count: number;
  matched_entries: number;
  thumb_url: string | null;
}

export interface CatalogPageSearchResult {
  items: CatalogPageHit[];
  total: number;
  query: string;
  pages_with_text: number;
  page_count: number;
  message?: string | null;
}

export interface CatalogVisualSearchResult {
  items: CatalogEntry[];
  scores: Record<string, number>;
  mode: "image" | "text" | "image+text";
  available: boolean;
  model?: string | null;
  indexed_positions: number;
  report?: { title?: string; message?: string } | null;
}

async function json<T>(response: Response, what: string): Promise<T> {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail =
      typeof body.detail === "string"
        ? body.detail
        : `${what} (${response.status})`;
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export const catalogsApi = {
  async list(params: { party_id?: string; supplier_id?: string }) {
    const search = new URLSearchParams();
    if (params.party_id) search.set("party_id", params.party_id);
    if (params.supplier_id) search.set("supplier_id", params.supplier_id);
    const response = await fetch(`${API}/api/catalogs?${search}`, {
      credentials: "include",
    });
    return json<{ items: CatalogSummary[]; total: number }>(
      response,
      "Не удалось загрузить каталоги",
    );
  },

  async get(documentId: string) {
    const response = await fetch(`${API}/api/catalogs/${documentId}`, {
      credentials: "include",
    });
    return json<CatalogSummary>(response, "Не удалось загрузить каталог");
  },

  async pages(documentId: string, range?: { from?: number; to?: number }) {
    const search = new URLSearchParams();
    if (range?.from) search.set("page_from", String(range.from));
    if (range?.to) search.set("page_to", String(range.to));
    const response = await fetch(
      `${API}/api/catalogs/${documentId}/pages?${search}`,
      { credentials: "include" },
    );
    return json<{
      document_id: string;
      page_count: number;
      items: CatalogPageInfo[];
    }>(response, "Не удалось загрузить страницы каталога");
  },

  async search(params: CatalogSearchParams) {
    const response = await mutFetch(`${API}/api/catalogs/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    return json<CatalogSearchResult>(response, "Поиск по каталогам не удался");
  },

  async remove(documentId: string, mode: "data" | "file" | "all") {
    const response = await mutFetch(
      `${API}/api/catalogs/${documentId}?mode=${mode}`,
      { method: "DELETE" },
    );
    return json<{
      mode: string;
      entries: number;
      pages: number;
      images: number;
      message: string;
    }>(response, "Не удалось удалить каталог");
  },

  async similar(params: {
    entry_id?: string;
    query?: string;
    exclude_same_supplier?: boolean;
    limit?: number;
  }) {
    const response = await mutFetch(`${API}/api/catalogs/similar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    return json<{ items: CatalogEntry[]; message: string }>(
      response,
      "Подбор аналогов не удался",
    );
  },

  /** Поиск ПО ДОКУМЕНТУ: по тексту страниц и по позициям этого каталога.
   *
   * Отвечает страницами, а не позициями: человек, листающий 948 страниц, ищет
   * «где это в каталоге», а не «какая это строка».
   */
  async searchPages(documentId: string, q: string, limit = 50) {
    const search = new URLSearchParams({ q, limit: String(limit) });
    const response = await fetch(
      `${API}/api/catalogs/${documentId}/search-pages?${search}`,
      { credentials: "include" },
    );
    return json<CatalogPageSearchResult>(
      response,
      "Поиск по каталогу не удался",
    );
  },

  /** Готов ли поиск по картинке. Заодно прогревает модель: сайдкар выгружает
   * веса по простою, и без прогрева первый поиск ждал 16 секунд вместо 0.1.
   */
  async visualStatus() {
    const response = await fetch(`${API}/api/catalogs/visual-status`, {
      credentials: "include",
    });
    return json<{
      available: boolean;
      model: string | null;
      device: string | null;
      indexed_positions: number;
    }>(response, "Не удалось проверить поиск по картинке");
  },

  /** Поиск по картинке: фото инструмента, фрагмент чертежа или «похожие на эту».
   *
   * Картинки и слова лежат в одном векторном пространстве, поэтому фото можно
   * уточнить словами. Если сервис распознавания недоступен, ответ придёт с
   * available=false — интерфейс обязан сказать об этом, а не показать обычный
   * текстовый поиск под кнопкой поиска по фото.
   */
  async searchVisual(params: {
    image_base64?: string;
    query?: string;
    entry_id?: string;
    supplier_id?: string;
    catalog_document_id?: string;
    crops_only?: boolean;
    limit?: number;
  }) {
    const response = await mutFetch(`${API}/api/catalogs/search-visual`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(params),
    });
    return json<CatalogVisualSearchResult>(
      response,
      "Поиск по картинке не удался",
    );
  },

  async pause(documentId: string, resume = false) {
    const response = await mutFetch(
      `${API}/api/catalogs/${documentId}/pause?resume=${resume}`,
      { method: "POST" },
    );
    return json<{
      paused: boolean;
      pages_done: number;
      page_count: number;
      message: string;
    }>(response, "Не удалось изменить состояние разбора");
  },

  pageImageUrl(
    documentId: string,
    page: number,
    size: "full" | "thumb" = "full",
  ) {
    return `${API}/api/catalogs/${documentId}/pages/${page}/image?size=${size}`;
  },

  entryImageUrl(entryId: string, size: "full" | "thumb" = "thumb") {
    return `${API}/api/catalogs/entries/${entryId}/image?size=${size}`;
  },
};
