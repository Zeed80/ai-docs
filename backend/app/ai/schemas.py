from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ProviderKind(str, Enum):
    # Local
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"
    VLLM = "vllm"
    OPENAI_COMPATIBLE = "openai_compatible"
    LMSTUDIO = "lmstudio"
    COMFYUI = "comfyui"  # Image generation/editing server (not an LLM chat provider)
    # Cloud — native integrations
    CLOUD_PROVIDER = "cloud_provider"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    DEEPSEEK = "deepseek"
    GEMINI = "gemini"
    OPENAI = "openai"
    # Cloud — OpenAI-compatible gateways
    OLLAMA_CLOUD = "ollama_cloud"
    MOONSHOT = "moonshot"      # Kimi
    MINIMAX = "minimax"
    DASHSCOPE = "dashscope"    # Qwen (Alibaba)
    MISTRAL = "mistral"
    GROQ = "groq"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    XAI = "xai"               # Grok
    COHERE = "cohere"
    PERPLEXITY = "perplexity"
    DEEPINFRA = "deepinfra"
    CEREBRAS = "cerebras"
    SAMBANOVA = "sambanova"
    NEBIUS = "nebius"
    NOVITA = "novita"
    HYPERBOLIC = "hyperbolic"


class ModelStatus(str, Enum):
    CANDIDATE = "candidate"
    STAGING = "staging"
    PRODUCTION = "production"
    DISABLED = "disabled"


class Modality(str, Enum):
    TEXT = "text"
    VISION = "vision"
    AUDIO = "audio"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    TOOL_CALLING = "tool_calling"


class AITask(str, Enum):
    INVOICE_OCR = "invoice_ocr"
    STRUCTURED_EXTRACTION = "structured_extraction"
    DRAWING_ANALYSIS = "drawing_analysis"
    DRAWING_ANALYSIS_VLM = "drawing_analysis_vlm"
    ENGINEERING_REASONING = "engineering_reasoning"
    EMAIL_DRAFTING = "email_drafting"
    EMBEDDING = "embedding"
    RERANKING = "reranking"
    CLASSIFICATION = "classification"
    LONG_CONTEXT_SUMMARIZATION = "long_context_summarization"
    TOOL_CALLING = "tool_calling"
    SPEECH = "speech"
    ORCHESTRATOR_PLANNING = "orchestrator_planning"
    CODE_GENERATION = "code_generation"
    # CAD digitization — understanding->drafting ("two-model") path.
    # CAD_SPEC_READ: the VLM that reads a drawing into a structured feature/dim spec.
    # CAD_SPEC_DRAFT: an (optional) generative model that drafts CAD geometry from
    # that spec; unassigned → the deterministic parametric drafter is used.
    CAD_DRAWING_GRAPH_READ = "cad_drawing_graph_read"
    CAD_DRAWING_GRAPH_LAYOUT = "cad_drawing_graph_layout"
    CAD_DRAWING_GRAPH_FRAGMENT_READ = "cad_drawing_graph_fragment_read"
    CAD_DRAWING_GRAPH_EVIDENCE_VERIFY = "cad_drawing_graph_evidence_verify"
    CAD_SPEC_READ = "cad_spec_read"
    CAD_SPEC_DRAFT = "cad_spec_draft"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ProposedToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = None
    rationale: str | None = None


class AIRequest(BaseModel):
    task: AITask
    messages: list[ChatMessage] = Field(default_factory=list)
    prompt: str | None = None
    images: list[str] = Field(default_factory=list, description="Base64 strings or provider URLs.")
    audio: str | None = None
    input_text: str | None = None
    response_schema: type[BaseModel] | None = Field(default=None, exclude=True)
    tools: list[ToolSpec] = Field(default_factory=list)
    confidential: bool = True
    allow_cloud: bool = False
    preferred_model: str | None = None
    # Per-call thinking/CoT override. None → use the model's catalog default
    # (``ModelCapability.thinking_enabled``). True/False force it on/off.
    thinking: bool | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class AIUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: int | None = None


class AIResponse(BaseModel):
    task: AITask
    provider: ProviderKind
    model: str
    text: str | None = None
    data: Any = None
    embedding: list[float] | None = None
    scores: list[float] | None = None
    proposed_tool_calls: list[ProposedToolCall] = Field(default_factory=list)
    usage: AIUsage = Field(default_factory=AIUsage)
    raw: dict[str, Any] = Field(default_factory=dict)


class ProviderNode(BaseModel):
    """A single provider endpoint (forward-looking: multi-GPU / remote nodes).

    ``base_url`` is the primary node. Additional nodes let the router fan out
    across several GPUs/machines. Wiring (load balancing, per-node VRAM) is
    added incrementally; this is the schema hook.
    """

    name: str
    base_url: HttpUrl | str
    vram_gb: float | None = None
    enabled: bool = True


class ProviderConfig(BaseModel):
    kind: ProviderKind
    base_url: HttpUrl | str
    api_key_env: str | None = None
    # Runtime-resolved plaintext key (from DB provider_instances or env). In-memory
    # only — never persisted to the YAML registry. Providers prefer this over env.
    api_key: str | None = Field(default=None, exclude=True)
    timeout_seconds: float = 120.0
    is_local: bool = True
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # Deprecated: superseded by the ``provider_instances`` table (multi-node).
    nodes: list[ProviderNode] = Field(default_factory=list)


class ModelCapability(BaseModel):
    name: str
    provider: ProviderKind
    provider_model: str
    status: ModelStatus = ModelStatus.CANDIDATE
    modalities: set[Modality] = Field(default_factory=set)
    max_context_tokens: int | None = None
    supports_structured_output: bool = False
    supports_tool_calling: bool = False
    embedding_dimension: int | None = None
    distance_metric: Literal["cosine", "dot", "euclid"] = "cosine"
    normalize_embeddings: bool = True
    max_input_tokens: int | None = None
    batch_size: int | None = None
    pooling: str | None = None
    supports_batching: bool = False
    rerank_score_range: str | None = None
    model_family: str | None = None
    capability_source: Literal["discovered", "manual", "verified"] = "manual"
    local_only: bool = True
    supports_multi_image: bool = False
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    quality_score: float = 0.0
    speed_score: float = 0.0
    vram_gb_estimate: float | None = None   # expected VRAM usage in GB
    # Thinking / chain-of-thought (CoT) control.
    #   thinking_supported — the model can reason step-by-step (Qwen3, DeepSeek-R1…)
    #   thinking_enabled    — default state of the per-model toggle. For extraction
    #                         workloads CoT usually hurts, so it defaults off.
    thinking_supported: bool = False
    thinking_enabled: bool = False
    # Optional pin to a specific provider node (provider_instances.name). When set,
    # calls for this model always route to that node; otherwise the router picks
    # any healthy node of the model's provider kind that hosts the model.
    preferred_instance: str | None = None
    notes: str | None = None


class TaskRoute(BaseModel):
    task: AITask
    required_modalities: set[Modality] = Field(default_factory=set)
    local_only: bool = True
    fallback_chain: list[str] = Field(default_factory=list)
    manual_review_fallback: bool = True
    min_quality_score: float = 0.0


class RegistrySnapshot(BaseModel):
    providers: dict[ProviderKind, ProviderConfig]
    models: dict[str, ModelCapability]
    routes: dict[AITask, TaskRoute]


class EvalCase(BaseModel):
    id: str
    task: AITask
    prompt: str
    expected_contains: list[str] = Field(default_factory=list)
    expected_json: dict[str, Any] | None = None
    confidential: bool = True


class EvalResult(BaseModel):
    case_id: str
    task: AITask
    model: str
    passed: bool
    score: float
    reason: str
    response_text: str | None = None
