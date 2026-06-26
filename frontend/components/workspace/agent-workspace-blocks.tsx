"use client";

import { useCallback, useEffect, useState } from "react";
import { CanvasChart } from "@/components/canvas/canvas-chart";
import { CanvasDocuments } from "@/components/canvas/canvas-documents";
import { CanvasImage } from "@/components/canvas/canvas-image";
import { CanvasMarkdown } from "@/components/canvas/canvas-markdown";
import { CanvasSheet } from "@/components/canvas/canvas-sheet";
import { CanvasTable } from "@/components/canvas/canvas-table";
import type { CanvasBlock } from "@/lib/canvas-context";
import { getApiBaseUrl } from "@/lib/api-base";
import { mutFetch } from "@/lib/auth";
import { useAgentName } from "@/lib/agent-name";
import {
  getActiveTabularSurface,
  setActiveTabularSurface,
} from "@/lib/workspace-context";

const API = getApiBaseUrl();

interface WorkspaceResponse {
  items: CanvasBlock[];
  total: number;
}

function BlockView({
  block,
  onDeleted,
}: {
  block: CanvasBlock;
  onDeleted: () => void;
}) {
  const agentName = useAgentName();
  async function deleteBlock() {
    await mutFetch(
      `${API}/api/workspace/blocks/${encodeURIComponent(block.id)}`,
      { method: "DELETE" },
    ).catch(() => {});
    onDeleted();
  }

  function activateBlock() {
    if (block.type === "sheet" && block.sheet_id) {
      setActiveTabularSurface({
        id: block.id,
        kind: "sheet",
        title: block.title,
        sheet_id: block.sheet_id,
        write_policy: "scratch",
      });
    } else if (block.type === "table") {
      setActiveTabularSurface({
        id: block.id,
        kind: "spec_table",
        title: block.title,
        write_policy: "approval",
      });
    }
  }

  return (
    <section
      onFocusCapture={activateBlock}
      onMouseDown={activateBlock}
      className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-slate-700 bg-slate-900"
    >
      <div className="flex items-center justify-between border-b border-slate-700 bg-slate-800 px-3 py-2">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold text-slate-100">
            {block.title || `Результат ${agentName}`}
          </h2>
        </div>
        <button
          onClick={deleteBlock}
          className="ml-3 rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-700 hover:text-slate-100"
        >
          Удалить
        </button>
      </div>
      <div className="min-h-0 flex-1 p-3">
        {block.type === "markdown" && block.content && (
          <CanvasMarkdown content={block.content} />
        )}
        {block.type === "table" && block.columns && block.rows && (
          <CanvasTable
            columns={block.columns}
            rows={block.rows}
            title={block.title}
            fill
            blockId={block.id}
          />
        )}
        {block.type === "sheet" && block.columns && (
          <CanvasSheet block={block} fill />
        )}
        {block.type === "image" && block.url && (
          <CanvasImage url={block.url} alt={block.alt} title={block.title} />
        )}
        {block.type === "chart" && (
          <CanvasChart
            chartType={block.chart_type}
            chartData={block.chart_data}
            title={block.title}
          />
        )}
        {block.type === "document" && block.documents && (
          <CanvasDocuments documents={block.documents} />
        )}
      </div>
    </section>
  );
}

export function AgentWorkspaceBlocks({
  className = "",
}: {
  className?: string;
}) {
  const agentName = useAgentName();
  const [blocks, setBlocks] = useState<CanvasBlock[]>([]);
  const [loading, setLoading] = useState(true);
  // Shown while a turn is routed to the desktop but the block hasn't arrived yet,
  // so the user isn't left staring at an empty panel wondering where it went.
  const [pending, setPending] = useState(false);

  const activateSurfaceForBlock = useCallback((block: CanvasBlock | null) => {
    if (!block) {
      setActiveTabularSurface(null);
      return;
    }
    if (block.type === "sheet" && block.sheet_id) {
      setActiveTabularSurface({
        id: block.id,
        kind: "sheet",
        title: block.title,
        sheet_id: block.sheet_id,
        write_policy: "scratch",
      });
    } else if (block.type === "table") {
      setActiveTabularSurface({
        id: block.id,
        kind: "spec_table",
        title: block.title,
        write_policy: "approval",
      });
    }
  }, []);

  const load = useCallback(async () => {
    const res = await fetch(`${API}/api/workspace/blocks`, {
      cache: "no-store",
    }).catch(() => null);
    if (!res?.ok) {
      setLoading(false);
      return;
    }
    const data = (await res.json()) as WorkspaceResponse;
    const items = data.items ?? [];
    setBlocks(items);
    const active = getActiveTabularSurface();
    if (!active || !items.some((item) => item.id === active.id)) {
      activateSurfaceForBlock(
        items.find((item) => item.type === "sheet" || item.type === "table") ??
          null,
      );
    }
    setLoading(false);
  }, [activateSurfaceForBlock]);

  async function clearBlocks() {
    await mutFetch(`${API}/api/workspace/blocks`, { method: "DELETE" }).catch(
      () => {},
    );
    await load();
  }

  useEffect(() => {
    load();
    const onUpdate = () => {
      setPending(false);
      load();
    };
    const onPending = () => setPending(true);
    window.addEventListener("workspace-blocks-updated", onUpdate);
    window.addEventListener("workspace-pending", onPending);
    // Fallback poll every 15 s in case the WS event was lost
    const timer = setInterval(load, 15_000);
    return () => {
      window.removeEventListener("workspace-blocks-updated", onUpdate);
      window.removeEventListener("workspace-pending", onPending);
      clearInterval(timer);
    };
  }, [load]);

  if (loading) {
    return (
      <div
        className={`rounded-lg border border-slate-800 bg-slate-900/50 p-4 text-sm text-slate-500 ${className}`}
      >
        Загружаю рабочий стол...
      </div>
    );
  }

  if (blocks.length === 0) {
    if (!pending) return null;
    return (
      <div
        className={`flex items-center gap-2 rounded-lg border border-slate-800 bg-slate-900/50 p-4 text-sm text-slate-400 ${className}`}
      >
        <span className="inline-block h-3 w-3 animate-pulse rounded-full bg-sky-500" />
        {agentName} готовит вывод на рабочий стол…
      </div>
    );
  }

  return (
    <div className={`flex min-h-0 w-full flex-col gap-3 ${className}`}>
      <div className="flex shrink-0 items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-slate-100">
            Вывод {agentName}
          </h2>
          <p className="mt-0.5 text-xs text-slate-500">
            Таблицы, документы, ссылки, графики и отчеты открываются здесь.
          </p>
        </div>
        <button
          onClick={clearBlocks}
          className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
        >
          Очистить
        </button>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-3">
        {blocks.map((block) => (
          <BlockView key={block.id} block={block} onDeleted={load} />
        ))}
      </div>
    </div>
  );
}
