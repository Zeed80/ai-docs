"use client";

import {
  useCanvas,
  useCanvasDispatch,
  type CanvasBlock,
} from "@/lib/canvas-context";
import { CanvasMarkdown } from "./canvas-markdown";
import { CanvasTable } from "./canvas-table";
import { CanvasImage } from "./canvas-image";
import { CanvasChart } from "./canvas-chart";

function BlockView({ block }: { block: CanvasBlock }) {
  const dispatch = useCanvasDispatch();

  return (
    <div className="border border-slate-700 rounded-lg bg-slate-900 overflow-hidden">
      {block.title && (
        <div className="flex items-center justify-between px-3 py-2 bg-slate-800 border-b border-slate-700">
          <span className="text-sm font-medium text-slate-200 truncate">
            {block.title}
          </span>
          <button
            onClick={() => dispatch({ type: "REMOVE_BLOCK", id: block.id })}
            className="ml-2 text-slate-500 hover:text-slate-300 text-lg leading-none flex-shrink-0"
            aria-label="Удалить блок"
          >
            ×
          </button>
        </div>
      )}
      <div className="p-3">
        {block.type === "markdown" && block.content && (
          <CanvasMarkdown content={block.content} />
        )}
        {block.type === "table" && block.columns && block.rows && (
          <CanvasTable
            columns={block.columns}
            rows={block.rows}
            title={block.title}
          />
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
      </div>
    </div>
  );
}

export function AgentCanvas() {
  const state = useCanvas();
  const dispatch = useCanvasDispatch();

  if (!state.isOpen) return null;

  return (
    <div className="flex flex-col h-full bg-slate-950 border-l border-slate-700">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 bg-slate-900 border-b border-slate-700 shrink-0">
        <span className="text-sm font-semibold text-slate-200">Холст</span>
        <div className="flex items-center gap-1">
          {state.blocks.length > 0 && (
            <button
              onClick={() => dispatch({ type: "CLEAR" })}
              className="px-2 py-0.5 text-xs text-slate-400 hover:text-slate-200 hover:bg-slate-700 rounded"
            >
              Очистить
            </button>
          )}
          <button
            onClick={() => dispatch({ type: "CLOSE" })}
            className="text-slate-500 hover:text-slate-300 text-xl leading-none w-6 h-6 flex items-center justify-center"
            aria-label="Закрыть"
          >
            ×
          </button>
        </div>
      </div>

      {/* Blocks */}
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {state.blocks.length === 0 ? (
          <div className="text-center mt-16 text-slate-600 text-sm">
            <div className="text-3xl mb-3">🖼</div>
            <p>Холст пуст</p>
            <p className="text-xs mt-1">
              Попросите агента вывести таблицу,
              <br />
              текст или изображение сюда.
            </p>
          </div>
        ) : (
          state.blocks.map((block) => (
            <BlockView key={block.id} block={block} />
          ))
        )}
      </div>
    </div>
  );
}
