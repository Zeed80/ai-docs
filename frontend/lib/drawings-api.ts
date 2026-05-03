/**
 * API client for drawings, features, contours, tool bindings, and tool catalog.
 */

import { getApiBaseUrl } from "./api-base";

const API_BASE = () => getApiBaseUrl();

class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE()}${path}`, {
    ...options,
    headers:
      options?.body instanceof FormData
        ? { ...(options?.headers as Record<string, string>) }
        : {
            "Content-Type": "application/json",
            ...(options?.headers as Record<string, string>),
          },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new ApiError(res.status, body);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

const apiBase = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
  postForm: <T>(path: string, fd: FormData) =>
    request<T>(path, { method: "POST", body: fd }),
};

export type DrawingStatus =
  | "uploaded"
  | "analyzing"
  | "analyzed"
  | "needs_review"
  | "approved"
  | "failed";

export type DrawingFeatureType =
  | "hole"
  | "pocket"
  | "surface"
  | "boss"
  | "groove"
  | "thread"
  | "chamfer"
  | "radius"
  | "slot"
  | "contour"
  | "other";

export type PrimitiveType =
  | "circle"
  | "arc"
  | "rectangle"
  | "polyline"
  | "line"
  | "spline"
  | "ellipse";

export type DimType =
  | "linear"
  | "angular"
  | "diameter"
  | "radius"
  | "depth"
  | "arc_length";
export type RoughnessType = "Ra" | "Rz" | "Rmax" | "Rq";
export type ToolType =
  | "drill"
  | "endmill"
  | "insert"
  | "holder"
  | "tap"
  | "reamer"
  | "boring_bar"
  | "thread_mill"
  | "grinder"
  | "turning_tool"
  | "milling_cutter"
  | "countersink"
  | "counterbore"
  | "other";

export type ToolSource = "warehouse" | "catalog" | "manual";

export interface FeatureContour {
  id: string;
  feature_id: string;
  primitive_type: PrimitiveType;
  params: Record<string, unknown>;
  layer?: string;
  line_type: string;
  color?: string;
  sort_order: number;
  is_user_edited: boolean;
  created_at: string;
}

export interface FeatureDimension {
  id: string;
  feature_id: string;
  dim_type: DimType;
  nominal: number;
  upper_tol?: number;
  lower_tol?: number;
  unit: string;
  fit_system?: string;
  label?: string;
  annotation_position?: Record<string, number>;
  is_reference: boolean;
  created_at: string;
}

export interface FeatureSurface {
  id: string;
  feature_id: string;
  roughness_type: RoughnessType;
  value: number;
  direction?: string;
  lay_symbol?: string;
  machining_required: boolean;
  annotation_position?: Record<string, number>;
  created_at: string;
}

export interface FeatureGDT {
  id: string;
  feature_id: string;
  symbol: string;
  tolerance_value: number;
  tolerance_zone?: string;
  datum_reference?: string;
  material_condition?: string;
  annotation_position?: Record<string, number>;
  created_at: string;
}

export interface FeatureToolBinding {
  id: string;
  feature_id: string;
  tool_source: ToolSource;
  warehouse_item_id?: string;
  catalog_entry_id?: string;
  manual_description?: string;
  cutting_parameters?: Record<string, unknown>;
  notes?: string;
  bound_by: string;
  created_at: string;
}

export interface DrawingFeature {
  id: string;
  drawing_id: string;
  feature_type: DrawingFeatureType;
  name: string;
  description?: string;
  sort_order: number;
  confidence: number;
  reviewed_at?: string;
  reviewed_by?: string;
  contours: FeatureContour[];
  dimensions: FeatureDimension[];
  surfaces: FeatureSurface[];
  gdt_annotations: FeatureGDT[];
  tool_binding?: FeatureToolBinding;
  created_at: string;
  updated_at?: string;
}

export interface Drawing {
  id: string;
  document_id?: string;
  drawing_number?: string;
  revision?: string;
  filename: string;
  format: string;
  svg_path?: string;
  thumbnail_path?: string;
  title_block?: Record<string, unknown>;
  bounding_box?: Record<string, number>;
  status: DrawingStatus;
  analysis_error?: string;
  celery_task_id?: string;
  created_at: string;
  updated_at?: string;
}

export interface DrawingWithFeatures extends Drawing {
  features: DrawingFeature[];
}

export interface DrawingListResponse {
  items: Drawing[];
  total: number;
  page: number;
  page_size: number;
}

export interface ToolSupplier {
  id: string;
  name: string;
  website?: string;
  country?: string;
  contact_info?: Record<string, string>;
  catalog_format?: string;
  notes?: string;
  is_active: boolean;
  created_at: string;
}

export interface ToolCatalogEntry {
  id: string;
  supplier_id?: string;
  part_number?: string;
  tool_type: ToolType;
  name: string;
  description?: string;
  diameter_mm?: number;
  length_mm?: number;
  parameters?: Record<string, unknown>;
  material?: string;
  coating?: string;
  price_currency: string;
  price_value?: number;
  catalog_page?: number;
  is_active: boolean;
  created_at: string;
}

export interface ToolSuggestion {
  entry: ToolCatalogEntry;
  supplier?: ToolSupplier;
  score: number;
  reason?: string;
  warehouse_available: boolean;
  warehouse_qty?: number;
}

export interface ToolSuggestionResponse {
  feature_id: string;
  suggestions: ToolSuggestion[];
  model_used?: string;
}

export interface ToolCatalogListResponse {
  items: ToolCatalogEntry[];
  total: number;
  page: number;
  page_size: number;
}

// ── Drawing API ──────────────────────────────────────────────────────────────

export const drawingsApi = {
  async upload(
    file: File,
    documentId?: string,
    drawingNumber?: string,
  ): Promise<{ drawing_id: string; task_id?: string; message: string }> {
    const fd = new FormData();
    fd.append("file", file);
    const params = new URLSearchParams();
    if (documentId) params.set("document_id", documentId);
    if (drawingNumber) params.set("drawing_number", drawingNumber);
    return apiBase.postForm(`/api/drawings?${params}`, fd);
  },

  async list(
    params: {
      page?: number;
      page_size?: number;
      status?: DrawingStatus;
      document_id?: string;
      drawing_number?: string;
    } = {},
  ): Promise<DrawingListResponse> {
    const q = new URLSearchParams();
    if (params.page) q.set("page", String(params.page));
    if (params.page_size) q.set("page_size", String(params.page_size));
    if (params.status) q.set("status", params.status);
    if (params.document_id) q.set("document_id", params.document_id);
    if (params.drawing_number) q.set("drawing_number", params.drawing_number);
    return apiBase.get(`/api/drawings?${q}`);
  },

  async get(id: string): Promise<DrawingWithFeatures> {
    return apiBase.get(`/api/drawings/${id}`);
  },

  async update(id: string, data: Partial<Drawing>): Promise<Drawing> {
    return apiBase.patch(`/api/drawings/${id}`, data);
  },

  async delete(id: string): Promise<void> {
    return apiBase.delete(`/api/drawings/${id}`);
  },

  async bulkDelete(
    drawingIds: string[],
    deleteFiles = true,
  ): Promise<{ deleted: number; missing: number }> {
    return request<{ deleted: number; missing: number }>(
      `/api/drawings/bulk-delete`,
      {
        method: "DELETE",
        body: JSON.stringify({
          drawing_ids: drawingIds,
          delete_files: deleteFiles,
        }),
      },
    );
  },

  async reanalyze(
    id: string,
    model?: string,
  ): Promise<{ drawing_id: string; task_id?: string; message: string }> {
    return apiBase.post(`/api/drawings/${id}/reanalyze`, {
      model,
      force: true,
    });
  },

  getSvgUrl(id: string): string {
    return `${process.env.NEXT_PUBLIC_API_URL || ""}/api/drawings/${id}/svg`;
  },

  // Features
  async getFeatures(
    drawingId: string,
    featureType?: string,
  ): Promise<DrawingFeature[]> {
    const q = featureType ? `?feature_type=${featureType}` : "";
    return apiBase.get(`/api/drawings/${drawingId}/features${q}`);
  },

  async createFeature(
    drawingId: string,
    data: Partial<DrawingFeature>,
  ): Promise<DrawingFeature> {
    return apiBase.post(`/api/drawings/${drawingId}/features`, data);
  },

  async updateFeature(
    drawingId: string,
    featureId: string,
    data: Partial<DrawingFeature>,
  ): Promise<DrawingFeature> {
    return apiBase.patch(
      `/api/drawings/${drawingId}/features/${featureId}`,
      data,
    );
  },

  async deleteFeature(drawingId: string, featureId: string): Promise<void> {
    return apiBase.delete(`/api/drawings/${drawingId}/features/${featureId}`);
  },

  async reviewFeature(
    drawingId: string,
    featureId: string,
    reviewedBy = "user",
  ): Promise<DrawingFeature> {
    return apiBase.post(
      `/api/drawings/${drawingId}/features/${featureId}/review`,
      { reviewed_by: reviewedBy },
    );
  },

  // Contours
  async getContours(
    drawingId: string,
    featureId: string,
  ): Promise<FeatureContour[]> {
    return apiBase.get(
      `/api/drawings/${drawingId}/features/${featureId}/contours`,
    );
  },

  async updateContours(
    drawingId: string,
    featureId: string,
    contours: Partial<FeatureContour>[],
  ): Promise<FeatureContour[]> {
    return apiBase.put(
      `/api/drawings/${drawingId}/features/${featureId}/contours`,
      { contours },
    );
  },

  // Tool binding
  async getToolBinding(
    drawingId: string,
    featureId: string,
  ): Promise<FeatureToolBinding | null> {
    return apiBase.get(`/api/drawings/${drawingId}/features/${featureId}/tool`);
  },

  async bindTool(
    drawingId: string,
    featureId: string,
    data: Partial<FeatureToolBinding>,
  ): Promise<FeatureToolBinding> {
    return apiBase.post(
      `/api/drawings/${drawingId}/features/${featureId}/tool`,
      data,
    );
  },

  async updateToolBinding(
    drawingId: string,
    featureId: string,
    data: Partial<FeatureToolBinding>,
  ): Promise<FeatureToolBinding> {
    return apiBase.patch(
      `/api/drawings/${drawingId}/features/${featureId}/tool`,
      data,
    );
  },

  async removeToolBinding(drawingId: string, featureId: string): Promise<void> {
    return apiBase.delete(
      `/api/drawings/${drawingId}/features/${featureId}/tool`,
    );
  },
};

// ── Tool Catalog API ──────────────────────────────────────────────────────────

export const toolCatalogApi = {
  // Suppliers
  async createSupplier(data: Partial<ToolSupplier>): Promise<ToolSupplier> {
    return apiBase.post("/api/tool-catalog/suppliers", data);
  },

  async listSuppliers(
    activeOnly = true,
  ): Promise<{ items: ToolSupplier[]; total: number }> {
    return apiBase.get(`/api/tool-catalog/suppliers?active_only=${activeOnly}`);
  },

  async getSupplier(id: string): Promise<ToolSupplier> {
    return apiBase.get(`/api/tool-catalog/suppliers/${id}`);
  },

  async uploadCatalog(
    supplierId: string,
    file: File,
  ): Promise<{ supplier_id: string; supplier_name: string; task_id?: string }> {
    const fd = new FormData();
    fd.append("file", file);
    return apiBase.postForm(
      `/api/tool-catalog/suppliers/${supplierId}/catalog`,
      fd,
    );
  },

  async listSupplierEntries(
    supplierId: string,
    params: { tool_type?: string; page?: number; page_size?: number } = {},
  ): Promise<ToolCatalogListResponse> {
    const q = new URLSearchParams();
    if (params.tool_type) q.set("tool_type", params.tool_type);
    if (params.page) q.set("page", String(params.page));
    if (params.page_size) q.set("page_size", String(params.page_size));
    return apiBase.get(
      `/api/tool-catalog/suppliers/${supplierId}/entries?${q}`,
    );
  },

  // Search
  async search(params: {
    query?: string;
    tool_type?: string;
    supplier_id?: string;
    diameter_min?: number;
    diameter_max?: number;
    material?: string;
    coating?: string;
    max_price?: number;
    semantic?: boolean;
    page?: number;
    page_size?: number;
  }): Promise<ToolCatalogListResponse> {
    const q = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
    });
    return apiBase.get(`/api/tool-catalog/search?${q}`);
  },

  // Suggestions
  async suggestForFeature(
    featureId: string,
    limit = 5,
  ): Promise<ToolSuggestionResponse> {
    return apiBase.get(`/api/tool-catalog/suggest/${featureId}?limit=${limit}`);
  },

  async getEntry(
    id: string,
  ): Promise<ToolCatalogEntry & { supplier?: ToolSupplier }> {
    return apiBase.get(`/api/tool-catalog/entries/${id}`);
  },

  // By main supplier (party) — linked catalog
  async listByParty(
    partyId: string,
  ): Promise<{ items: ToolSupplier[]; total: number }> {
    return apiBase.get(`/api/tool-catalog/by-supplier/${partyId}`);
  },

  async uploadCatalogForParty(
    partyId: string,
    file: File,
  ): Promise<{ supplier_id: string; supplier_name: string; task_id?: string }> {
    const fd = new FormData();
    fd.append("file", file);
    return apiBase.postForm(
      `/api/tool-catalog/by-supplier/${partyId}/catalog`,
      fd,
    );
  },

  async listEntriesByParty(
    partyId: string,
    params: {
      tool_type?: string;
      query?: string;
      page?: number;
      page_size?: number;
    } = {},
  ): Promise<ToolCatalogListResponse> {
    const q = new URLSearchParams();
    if (params.tool_type) q.set("tool_type", params.tool_type);
    if (params.query) q.set("query", params.query);
    if (params.page) q.set("page", String(params.page));
    if (params.page_size) q.set("page_size", String(params.page_size));
    return apiBase.get(`/api/tool-catalog/by-supplier/${partyId}/entries?${q}`);
  },
};
