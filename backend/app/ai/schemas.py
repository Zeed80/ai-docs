from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ProviderKind(str, Enum):
    OLLAMA = "ollama"
    VLLM = "vllm"
    OPENAI_COMPATIBLE = "openai_compatible"
    CLOUD_PROVIDER = "cloud_provider"
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    GEMINI = "gemini"


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


class ProviderConfig(BaseModel):
    kind: ProviderKind
    base_url: HttpUrl | str
    api_key_env: str | None = None
    timeout_seconds: float = 120.0
    is_local: bool = True
    extra_headers: dict[str, str] = Field(default_factory=dict)


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
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    quality_score: float = 0.0
    speed_score: float = 0.0
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
