"use client";

import {
  createContext,
  Dispatch,
  ReactNode,
  useContext,
  useReducer,
} from "react";

export type CanvasBlockType = "markdown" | "table" | "image" | "chart";

export interface CanvasColumn {
  key: string;
  header: string;
  type?: "text" | "number" | "date" | "boolean";
  width?: number;
}

export interface CanvasBlock {
  id: string;
  type: CanvasBlockType;
  title?: string;
  // markdown
  content?: string;
  // table
  columns?: CanvasColumn[];
  rows?: Record<string, unknown>[];
  // image
  url?: string;
  alt?: string;
  // chart
  chart_type?: "bar" | "line" | "pie" | "area";
  chart_data?: Record<string, unknown>;
}

export interface CanvasState {
  isOpen: boolean;
  blocks: CanvasBlock[];
}

export type CanvasAction =
  | { type: "OPEN" }
  | { type: "CLOSE" }
  | { type: "TOGGLE" }
  | { type: "APPEND_BLOCK"; block: Omit<CanvasBlock, "id"> & { id?: string } }
  | { type: "REPLACE_BLOCK"; id: string; block: Omit<CanvasBlock, "id"> }
  | { type: "REMOVE_BLOCK"; id: string }
  | { type: "CLEAR" };

let _blockCounter = 0;

function canvasReducer(state: CanvasState, action: CanvasAction): CanvasState {
  switch (action.type) {
    case "OPEN":
      return { ...state, isOpen: true };
    case "CLOSE":
      return { ...state, isOpen: false };
    case "TOGGLE":
      return { ...state, isOpen: !state.isOpen };
    case "APPEND_BLOCK": {
      const id = action.block.id ?? `block_${++_blockCounter}`;
      return {
        ...state,
        blocks: [...state.blocks, { ...action.block, id }],
      };
    }
    case "REPLACE_BLOCK": {
      return {
        ...state,
        blocks: state.blocks.map((b) =>
          b.id === action.id ? { ...action.block, id: action.id } : b,
        ),
      };
    }
    case "REMOVE_BLOCK":
      return {
        ...state,
        blocks: state.blocks.filter((b) => b.id !== action.id),
      };
    case "CLEAR":
      return { ...state, blocks: [] };
    default:
      return state;
  }
}

const CanvasStateContext = createContext<CanvasState>({
  isOpen: false,
  blocks: [],
});
const CanvasDispatchContext = createContext<Dispatch<CanvasAction>>(() => {});

export function CanvasProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(canvasReducer, {
    isOpen: false,
    blocks: [],
  });

  return (
    <CanvasStateContext.Provider value={state}>
      <CanvasDispatchContext.Provider value={dispatch}>
        {children}
      </CanvasDispatchContext.Provider>
    </CanvasStateContext.Provider>
  );
}

export function useCanvas(): CanvasState {
  return useContext(CanvasStateContext);
}

export function useCanvasDispatch(): Dispatch<CanvasAction> {
  return useContext(CanvasDispatchContext);
}
