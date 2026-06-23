// Shared grid model — the single contract every table consumer feeds on
// (invoices page, workspace desktop / spec-tables, ad-hoc sheets, agent output).
//
// Kept deliberately close to the existing CanvasColumn shape so the migration
// of CanvasTable is a thin wrapper, not a rewrite.

export type GridColumnType =
  | "text"
  | "number"
  | "date"
  | "boolean"
  | "select"
  | "money"
  | "link"
  | "download"
  | "delete";

export interface GridColumn {
  key: string;
  header: string;
  type?: GridColumnType;
  width?: number;
  /** Cell is editable inline (Phase 2/3). Defaults to false. */
  editable?: boolean;
  /** Options for `select`-type editable cells. */
  options?: string[];
  /** Per-column formula (computed column, Phase 4), e.g. "quantity * unit_price". */
  formula?: string;
}

export type GridRow = Record<string, unknown>;

// Column layout preferences — generalised from invoices' ColumnPrefs so any
// grid can persist order / visibility / widths under its own storage key.
export interface GridPrefs {
  order: string[];
  visibility: Record<string, boolean>;
  widths: Record<string, number>;
}

// One committed cell edit emitted by an editable grid.
export interface GridCellEdit {
  rowIndex: number;
  /** Stable row primary key when the grid carries one (spec-table writeback). */
  rowPk?: string | number;
  field: string;
  value: unknown;
  previous: unknown;
}

export const MIN_GRID_COLUMN_WIDTH = 56;
export const FALLBACK_GRID_WIDTH = 150;
