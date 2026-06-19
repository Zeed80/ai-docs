"use client";

import {
  DndContext,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import type { TableColumn } from "@/lib/api-client";

interface ColumnManagerProps {
  catalog: TableColumn[]; // full catalog, keyed metadata
  order: string[]; // current display order (data columns only)
  visibility: Record<string, boolean>;
  onChange: (order: string[], visibility: Record<string, boolean>) => void;
  onReset: () => void;
  onClose: () => void;
}

function SortableRow({
  col,
  visible,
  onToggle,
}: {
  col: TableColumn;
  visible: boolean;
  onToggle: () => void;
}) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: col.key });
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={`flex items-center gap-2 rounded px-1.5 py-1 ${
        isDragging ? "bg-slate-600" : "hover:bg-slate-700/50"
      }`}
    >
      <button
        {...attributes}
        {...listeners}
        className="cursor-grab text-slate-500 hover:text-slate-300 active:cursor-grabbing"
        title="Перетащить"
        aria-label="Перетащить столбец"
      >
        <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20">
          <path d="M7 4a1 1 0 110 2 1 1 0 010-2zM7 9a1 1 0 110 2 1 1 0 010-2zM7 14a1 1 0 110 2 1 1 0 010-2zM13 4a1 1 0 110 2 1 1 0 010-2zM13 9a1 1 0 110 2 1 1 0 010-2zM13 14a1 1 0 110 2 1 1 0 010-2z" />
        </svg>
      </button>
      <label className="flex flex-1 cursor-pointer items-center gap-2 text-sm text-slate-200">
        <input
          type="checkbox"
          checked={visible}
          onChange={onToggle}
          className="h-3.5 w-3.5 accent-blue-500"
        />
        {col.label}
      </label>
    </div>
  );
}

export function ColumnManager({
  catalog,
  order,
  visibility,
  onChange,
  onReset,
  onClose,
}: ColumnManagerProps) {
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
  );
  const byKey = new Map(catalog.map((c) => [c.key, c]));
  const orderedCols = order
    .map((k) => byKey.get(k))
    .filter((c): c is TableColumn => Boolean(c));

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIndex = order.indexOf(active.id as string);
    const newIndex = order.indexOf(over.id as string);
    if (oldIndex < 0 || newIndex < 0) return;
    onChange(arrayMove(order, oldIndex, newIndex), visibility);
  };

  const toggle = (key: string) =>
    onChange(order, { ...visibility, [key]: !visibility[key] });

  return (
    <div
      className="absolute right-0 z-30 mt-2 w-72 rounded-lg border border-slate-700 bg-slate-800 p-3 shadow-2xl"
      onClick={(e) => e.stopPropagation()}
    >
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-200">Столбцы</h3>
        <button
          onClick={onClose}
          className="text-lg leading-none text-slate-500 hover:text-slate-200"
        >
          ×
        </button>
      </div>
      <div className="max-h-80 overflow-y-auto pr-1">
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          onDragEnd={handleDragEnd}
        >
          <SortableContext items={order} strategy={verticalListSortingStrategy}>
            {orderedCols.map((col) => (
              <SortableRow
                key={col.key}
                col={col}
                visible={!!visibility[col.key]}
                onToggle={() => toggle(col.key)}
              />
            ))}
          </SortableContext>
        </DndContext>
      </div>
      <div className="mt-3 flex justify-between border-t border-slate-700 pt-2">
        <button
          onClick={onReset}
          className="text-xs text-slate-400 hover:text-slate-200"
        >
          Сбросить к умолчанию
        </button>
      </div>
    </div>
  );
}
