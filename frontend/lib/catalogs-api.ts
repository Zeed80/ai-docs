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
  page_number?: number;
  tool_type?: string;
  has_price?: boolean;
  has_image?: boolean;
  page?: number;
  page_size?: number;
  include_facets?: boolean;
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
