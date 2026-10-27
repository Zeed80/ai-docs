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
  /** "oauth" — узел вошёл через сайт провайдера вместо ключа. */
  auth_mode?: "api_key" | "oauth";
  /** Что можно показать о входе (никогда не токены). */
  oauth?: ProviderOAuthInfo;
}

export interface ProviderOAuthInfo {
  account?: string | null;
  /** Unix-время окончания токена (секунды); обновляется сервером сам. */
  expires_at?: number | null;
  region?: string | null;
  refreshable?: boolean;
}

/** Провайдер умеет вход через свой сайт. */
export interface ProviderOAuthKind {
  label: string;
  note: string;
  /** "redirect" — уход на сайт и возврат (OpenRouter); "device" — код на сайте. */
  flow: "redirect" | "device";
  regions: string[];
}

export interface KnownKind {
  kind: string;
  is_local: boolean;
  default_base_url: string;
  requires_api_key: boolean;
  oauth?: ProviderOAuthKind | null;
}

export interface OAuthStart {
  flow_id: string;
  type: "redirect" | "device";
  authorize_url?: string;
  verification_uri?: string;
  user_code?: string;
  expires_at?: number;
  interval?: number;
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
  /** Ниже — поля, которые лежали в схеме модели и не доходили до интерфейса. */
  max_context_tokens: number | null;
  supports_tool_calling: boolean;
  supports_structured_output: boolean;
  cost_per_1k_input: number | null;
  cost_per_1k_output: number | null;
  notes: string | null;
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
  /** Наружу уходит само содержимое документов, а не производная от него. */
  document_content?: boolean;
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
  /** Слот, без которого конвейер работает: его можно выключить целиком. */
  optional?: boolean;
  /** Оператор выключил слот — стадия не выполняется. */
  disabled?: boolean;
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

/** Почему модель не стоит назначать на слот — и чем это чинится. */
export interface CandidateReason {
  code: string;
  message: string;
  fix_action:
    | "open_cloud_provider"
    | "enable_node"
    | "pull_model"
    | "verify_model"
    | "none";
  fix_target: string | null;
}

/**
 * Модель-кандидат с готовым вердиктом сервера.
 *
 * Раньше интерфейс считал пригодность сам, повторяя серверные правила на
 * TypeScript, и копии уже расходились.
 */
export interface ModelCandidate extends CatalogModel {
  node: string | null;
  capabilities_unknown: boolean;
  eligibility: "ok" | "needs_action" | "unsuitable" | "forbidden";
  reasons: CandidateReason[];
}

// ── Состояние локальных серверов и видеопамяти ───────────────────────────────
// Приходит из /api/local-models/status. Жило внутри монолита экрана моделей,
// из-за чего вкладки нельзя было разнести по файлам.

export interface ProviderStatus {
  running: boolean;
  url?: string;
  models?: string[];
  model_loaded?: string | null;
  model_count?: number;
  error?: string;
  gpu_memory_utilization?: number;
  max_model_len?: number;
  dtype?: string;
}

export interface VramAllocation {
  vram_used_gb: number;
  vram_limit_gb: number | null;
  running: boolean;
  models: { name: string; vram_gb: number }[];
}

export interface AllStatus {
  providers: {
    ollama: ProviderStatus;
    llamacpp: ProviderStatus;
    vllm: ProviderStatus;
  };
  gpu: {
    total_gb: number;
    used_gb: number;
    free_gb: number;
    driver_version?: string;
  } | null;
  vram_allocations: Record<string, VramAllocation>;
  total_vram_gb: number;
}

/** Вкладки раздела «Модели». */
export type ModelsTab =
  "assignment" | "overview" | "library" | "parameters" | "gpu";

// ── Библиотека моделей ───────────────────────────────────────────────────────

export type LocalProviderKind = "ollama" | "llamacpp" | "vllm";

/** Откуда берётся список: локальный каталог или внешний реестр. */
export type ModelSource = "local" | "huggingface" | "modelscope";

export interface RepoFile {
  filename?: string;
  rfilename?: string;
  size_human?: string;
  quant?: string;
  is_split?: boolean;
}

/** Запись библиотеки: и локальная модель, и найденная в реестре. */
export interface ModelItem {
  name?: string;
  repo_id?: string;
  model_name?: string;
  author?: string;
  path?: string;
  size_bytes?: number;
  size_human?: string;
  format?: string;
  active?: boolean;
  downloads?: number;
  likes?: number;
  stars?: number;
  tags?: string[];
  gated?: boolean;
  source?: string;
  vram_gb_estimate?: number;
}

// ── Strata (GPU-рантайм, переключаемый против GPU-Ollama) ───────────────────

export type GpuOwner = "ollama" | "strata";

export type StrataPhase =
  "not_created" | "stopped" | "installing" | "loading" | "ready" | "unloaded";

export type StrataRamMode =
  "auto" | "off" | "resident" | "mmap" | "budget" | "own";

export interface StrataQuant {
  /** Имя варианта у setup Strata: «IQ3_S», «qwen36-UD-Q4_K_XL». */
  id: string;
  family: string;
  model: string;
  label: string;
  download_gb: number;
  disk_need_gb: number;
  experts_gb: number;
  ram_gb: number;
  installed: boolean;
  size_on_disk_gb: number | null;
  low_ram_mode: boolean;
  ram_mode: StrataRamMode;
  fits_ram: boolean;
  catalog_key: string;
  /** Strata, начиная с которой setup знает это семейство (Qwen3.6 — 0.1.45). */
  min_version: string | null;
  installed_settings: { context: number | null; vision: boolean | null } | null;
}

export interface StrataFamily {
  family: string;
  title: string;
  about: string;
  catalog_key: string;
}

export interface StrataRamPlan {
  reserve_gb: number;
  limit_gb: number | null;
  low_ram: string;
  resident_budget_gib: number | null;
  mode: StrataRamMode;
  fits: boolean;
}

export interface StrataRelease {
  version?: string;
  tag?: string;
  commit?: string;
  published_at?: string | null;
  name?: string;
  notes?: string;
  url?: string | null;
  error?: string;
  checked_at?: number;
  supported?: boolean | null;
  /** Newest version with a ready image for this card (may be older than `version`). */
  image_version?: string | null;
  flavor?: string;
}

export interface StrataBuildJob {
  stage: "building" | "built" | "failed" | "apply_failed";
  version: string | null;
  commit: string | null;
  image: string | null;
  then: "apply" | "create" | null;
  started_at: string | null;
  exit_code: number | null;
  error: string | null;
  log_tail: string[];
}

export interface StrataServerInfo {
  current: {
    version: string | null;
    commit: string | null;
    image: string;
    image_id: string;
  } | null;
  latest: StrataRelease;
  update_available: boolean;
  /** A newer release the panel cannot run yet (why), instead of offering it. */
  update_blocked?: string | null;
  job: StrataBuildJob | null;
  error?: string;
}

export interface StrataInstaller {
  variant: string | null;
  stage: "downloading" | "failed";
  exit_code?: number | null;
  size_on_disk_gb?: number | null;
  disk_need_gb?: number | null;
  log_tail: string[];
}

export interface StrataStatus {
  owner: GpuOwner | null;
  server: StrataServerInfo;
  installer: StrataInstaller | null;
  container: string | null;
  docker_error: string | null;
  phase: StrataPhase;
  health: {
    model?: string;
    images?: boolean;
    loaded?: boolean;
    max_context?: number;
  } | null;
  desired: {
    id: string;
    family: string;
    model: string;
    context: number;
    vision: boolean;
    reinstall_pending: boolean;
    install_only: boolean;
    low_ram: string;
  };
  down_while_owner: boolean;
  installed: string[];
  installed_context: number | null;
  quants: StrataQuant[];
  families: StrataFamily[];
  served_catalog_key: string;
  ram_plan: StrataRamPlan;
  reserve_choices: number[];
  contexts: number[];
  disk_free_gb: number | null;
  ram_total_gb: number | null;
  log_tail: string[];
  switch_revision: string | null;
  runtime: {
    idle_unload_s: number;
    free_comfyui: boolean;
    min_free_vram_mib: number;
    parallel: number;
    conversation_cache_mib: number;
    ram_reserve_gb: number;
  };
  idle_choices: number[];
  parallel_choices: number[];
  conversation_cache_choices: number[];
  external_url: string | null;
}

export interface StrataLoadPlan {
  id: string;
  catalog_key: string;
  context: number;
  vision: boolean;
  setup_pass: boolean;
  ram_plan: StrataRamPlan;
  follow: StrataSlotPlanItem[];
  warnings: string[];
}

export interface StrataAccess {
  url: string | null;
  openai_base_url: string | null;
  api_key: string | null;
}

export interface StrataSlotPlanItem {
  slot: string;
  label: string;
  current_model: string | null;
  move: boolean;
  reason: string;
}
