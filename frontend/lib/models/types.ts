/**
 * Зеркало Pydantic-схем из backend/app/api/providers_api.py.
 *
 * Держим отдельно от компонентов: типы нужны и экрану назначений, и каталогу,
 * и инфраструктуре, а раньше они жили внутри одного файла на 3647 строк и
 * потому не переиспользовались.
 */

export type Modality =
  "text" | "vision" | "audio" | "embedding" | "rerank" | "tool_calling";

export type Availability = "available" | "missing" | "unknown";

export type ThinkingLevel = "low" | "medium" | "high";

/** Тройственный выбор рассуждения в интерфейсе; null = как у модели. */
export type ThinkingChoice = "auto" | "on" | "off";

export type ApiKeyState = "unset" | "set" | "corrupt";

export interface ProviderInstance {
  id: string;
  kind: string;
  name: string;
  base_url: string | null;
  default_base_url: string;
  enabled: boolean;
  is_local: boolean;
  api_key_set: boolean;
  api_key_mask: string;
  /** Испорченный ключ раньше был неотличим от отсутствующего. */
  api_key_state?: ApiKeyState;
  extra: Record<string, unknown>;
  last_check_at: string | null;
  last_check_ok: boolean | null;
  last_error: string | null;
}

export interface KnownKind {
  kind: string;
  is_local: boolean;
  default_base_url: string;
  requires_api_key: boolean;
}

export interface CatalogModel {
  key: string;
  provider: string;
  provider_model: string;
  status: string;
  modalities: Modality[];
  local_only: boolean;
  thinking_supported: boolean;
  thinking_enabled: boolean;
  thinking_levels: ThinkingLevel[];
  thinking_level_default: ThinkingLevel | null;
  preferred_instance: string | null;
  quality_score: number;
  speed_score: number;
  vram_gb_estimate: number | null;
  availability: Availability;
}

export interface Slot {
  slot: string;
  group: string;
  label: string;
  hint: string;
  model: string | null;
  current_model: string | null;
  /** Действующая политика: запрещено ли облако прямо сейчас. */
  local_only: boolean;
  /** Слот локален по умолчанию, но его можно открыть облаку. */
  cloud_optionable: boolean;
  cloud_allowed: boolean;
  required_modality: Modality | null;
  thinking_supported_by_slot: boolean;
  thinking_supported_by_model: boolean;
  thinking_model_default: boolean | null;
  thinking_override: boolean | null;
  thinking_effective: boolean | null;
  /** slot | model | unsupported — откуда взялось действующее значение. */
  thinking_source: string;
  thinking_disable_supported: boolean;
  thinking_warning: string | null;
  thinking_levels: ThinkingLevel[];
  thinking_level_override: ThinkingLevel | null;
  thinking_level_effective: ThinkingLevel | null;
}

export interface AssignmentDiffItem {
  slot: string;
  old_model: string | null;
  new_model: string | null;
  affected: string[];
}

export interface AssignmentIssue {
  slot: string;
  code: string;
  message: string;
}

export interface AssignmentDraft {
  slots: Slot[];
  diff: AssignmentDiffItem[];
  warnings: AssignmentIssue[];
  errors: AssignmentIssue[];
  revision_id?: string | null;
}

export interface RoutingChainEntry {
  key: string;
  provider: string;
  provider_model: string;
  availability: Availability;
}

export interface RoutingChain {
  task: string;
  models: RoutingChainEntry[];
  dead: number;
}

export interface SlotSmokeResult {
  ok: boolean;
  slot: string;
  model: string | null;
  dry_run: boolean;
  latency_ms?: number | null;
  error?: string | null;
  detail?: string | null;
}
