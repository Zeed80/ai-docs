"""Budgeted adaptive reader runtime for immutable EngineeringModelGraph revisions."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.engineering_model_graph import (
    Assertion,
    EngineeringModelGraph,
    Evidence,
    GraphEdge,
    GraphNode,
    GraphPatch,
    HypothesisOption,
    HypothesisSet,
    ReaderPassPlan,
    StrictModel,
    plan_next_reader_pass,
)
from app.services.engineering_model_graph import (
    latest_graph_revision,
    load_graph,
    merge_and_persist_patch,
)


class ReaderPassResult(StrictModel):
    """Untrusted pass payload before the runtime seals provenance and budget data."""

    add_nodes: list[GraphNode] = Field(default_factory=list)
    add_edges: list[GraphEdge] = Field(default_factory=list)
    add_assertions: list[Assertion] = Field(default_factory=list)
    add_evidence: list[Evidence] = Field(default_factory=list)
    add_hypothesis_options: list[HypothesisOption] = Field(default_factory=list)
    add_hypothesis_sets: list[HypothesisSet] = Field(default_factory=list)
    supersede_assertion_ids: list[str] = Field(default_factory=list)
    retract_assertion_ids: list[str] = Field(default_factory=list)
    resolved_question_ids: list[str] = Field(default_factory=list)
    remaining_contradictions: list[str] = Field(default_factory=list)
    model_calls_used: int = Field(default=1, ge=0, le=4, exclude=True)
    stop_reason: str | None = Field(default=None, exclude=True)


ReaderPassExecutor = Callable[
    [EngineeringModelGraph, ReaderPassPlan],
    Awaitable[ReaderPassResult],
]
PatchPersister = Callable[[GraphPatch], Awaitable[EngineeringModelGraph]]
PassResultObserver = Callable[
    [EngineeringModelGraph, ReaderPassPlan, ReaderPassResult],
    Awaitable[None],
]


class ReaderRunOutcome(StrictModel):
    graph_id: str
    revision: int
    canonical_sha256: str
    passes_completed: int
    stop_reason: str
    partial: bool


class EngineeringModelReader:
    """Drive narrow reader passes until convergence or a production budget gate."""

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic

    async def run(
        self,
        graph: EngineeringModelGraph,
        *,
        target_id: str,
        persist_patch: PatchPersister,
        read_pass: ReaderPassExecutor,
        hybrid_trace_pass: ReaderPassExecutor | None = None,
        observe_pass_result: PassResultObserver | None = None,
    ) -> ReaderRunOutcome:
        passes_completed = 0
        while True:
            plan = plan_next_reader_pass(graph, target_id=target_id)
            if plan.kind == "stop":
                return self._outcome(graph, passes_completed, plan.reason)
            if plan.kind == "hybrid_trace" and hybrid_trace_pass is None:
                graph = await persist_patch(
                    self._terminal_patch(
                        graph,
                        plan,
                        reason="hybrid_trace_unavailable",
                    )
                )
                return self._outcome(
                    graph,
                    passes_completed,
                    "hybrid_trace_unavailable",
                )

            executor = hybrid_trace_pass if plan.kind == "hybrid_trace" else read_pass
            assert executor is not None
            remaining = max(
                0.0,
                graph.reader_manifest.max_wall_seconds - graph.reader_manifest.elapsed_seconds,
            )
            if remaining <= 0:
                graph = await persist_patch(
                    self._terminal_patch(
                        graph,
                        plan,
                        reason="wall_budget_exhausted",
                    )
                )
                return self._outcome(
                    graph,
                    passes_completed,
                    "wall_budget_exhausted",
                )
            timeout = min(graph.reader_manifest.call_timeout_seconds, remaining)
            started = self._monotonic()
            stop_reason: str | None = None
            try:
                result = await asyncio.wait_for(executor(graph, plan), timeout=timeout)
            except TimeoutError:
                result = ReaderPassResult()
            except Exception:  # noqa: BLE001 - the partial revision is the contract
                result = ReaderPassResult()
                stop_reason = "reader_error"
            if result.stop_reason and plan.kind != "hybrid_trace":
                # A reader model cannot turn its own payload into a system patch
                # (which would bypass the assurance elevation guard).
                result = ReaderPassResult()
                stop_reason = "reader_error"
            else:
                stop_reason = stop_reason or result.stop_reason
            elapsed = min(
                max(self._monotonic() - started, 0.0),
                float(timeout),
            )
            if elapsed == 0.0:
                # A mocked/very fast call must still be visible in the budget.
                elapsed = min(float(timeout), 1e-6)
            if observe_pass_result is not None:
                await observe_pass_result(graph, plan, result)
            patch = self._result_patch(
                graph,
                plan,
                result,
                elapsed_seconds=elapsed,
                stop_reason=stop_reason,
            )
            graph = await persist_patch(patch)
            passes_completed += 1
            if stop_reason:
                return self._outcome(graph, passes_completed, stop_reason)

    @staticmethod
    def _patch_identity(graph: EngineeringModelGraph, plan: ReaderPassPlan) -> str:
        seed = (
            f"{graph.graph_id}:{graph.canonical_sha256}:"
            f"{graph.reader_manifest.calls_used + 1}:{plan.kind}"
        )
        return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))

    def _result_patch(
        self,
        graph: EngineeringModelGraph,
        plan: ReaderPassPlan,
        result: ReaderPassResult,
        *,
        elapsed_seconds: float,
        stop_reason: str | None,
    ) -> GraphPatch:
        identity = self._patch_identity(graph, plan)
        producer: Literal["reader", "tracer", "system"] = (
            "system" if stop_reason else "tracer" if plan.kind == "hybrid_trace" else "reader"
        )
        return GraphPatch(
            patch_id=f"reader-pass:{identity}",
            base_revision=graph.revision,
            base_sha256=graph.canonical_sha256,
            producer=producer,
            pass_id=identity,
            idempotency_key=f"reader-pass:{identity}",
            **result.model_dump(),
            reader_call_count=result.model_calls_used,
            reader_call_elapsed_seconds=(elapsed_seconds if result.model_calls_used else 0.0),
            reader_attempt_assertion_ids=(plan.assertion_ids if result.model_calls_used else []),
            reader_stop_reason=stop_reason,
        )

    def _terminal_patch(
        self,
        graph: EngineeringModelGraph,
        plan: ReaderPassPlan,
        *,
        reason: str,
    ) -> GraphPatch:
        identity = self._patch_identity(graph, plan)
        return GraphPatch(
            patch_id=f"reader-stop:{identity}",
            base_revision=graph.revision,
            base_sha256=graph.canonical_sha256,
            producer="system",
            pass_id=identity,
            idempotency_key=f"reader-stop:{identity}",
            reader_stop_reason=reason,
        )

    @staticmethod
    def _outcome(
        graph: EngineeringModelGraph,
        passes_completed: int,
        reason: str,
    ) -> ReaderRunOutcome:
        return ReaderRunOutcome(
            graph_id=graph.graph_id,
            revision=graph.revision,
            canonical_sha256=graph.canonical_sha256,
            passes_completed=passes_completed,
            stop_reason=reason,
            partial=reason != "frontier_resolved",
        )


async def run_persisted_reader(
    db: AsyncSession,
    *,
    graph_id: str,
    target_id: str,
    read_pass: ReaderPassExecutor,
    hybrid_trace_pass: ReaderPassExecutor | None = None,
    observe_pass_result: PassResultObserver | None = None,
) -> ReaderRunOutcome:
    """Run and commit every accepted pass so later failures retain partial work."""
    row = await latest_graph_revision(db, graph_id)
    if row is None:
        raise LookupError("EngineeringModelGraph not found")
    graph = load_graph(row)

    async def persist(patch: GraphPatch) -> EngineeringModelGraph:
        next_row, errors = await merge_and_persist_patch(
            db,
            patch,
            expected_graph_id=graph_id,
        )
        await db.commit()
        if next_row is None:
            raise ValueError("reader GraphPatch rejected: " + ",".join(errors))
        return load_graph(next_row)

    return await EngineeringModelReader().run(
        graph,
        target_id=target_id,
        persist_patch=persist,
        read_pass=read_pass,
        hybrid_trace_pass=hybrid_trace_pass,
        observe_pass_result=observe_pass_result,
    )
