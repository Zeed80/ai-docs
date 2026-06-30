"use client";

import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
} from "react";

export interface MaskCanvasHandle {
  /** Returns a PNG mask (white = edit area on black) or null if nothing painted. */
  getMaskBlob: () => Promise<Blob | null>;
  clear: () => void;
}

interface Props {
  imageUrl: string;
  className?: string;
}

/**
 * Brush-on-image mask editor for inpainting. Pointer events make it work with
 * both mouse and touch. The painted (white) area is what ComfyUI will regenerate.
 */
const MaskCanvas = forwardRef<MaskCanvasHandle, Props>(function MaskCanvas(
  { imageUrl, className },
  ref,
) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const drawing = useRef(false);
  const [brush, setBrush] = useState(36);
  const [painted, setPainted] = useState(false);

  useEffect(() => {
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.onload = () => {
      imgRef.current = img;
      const canvas = canvasRef.current;
      if (!canvas) return;
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      redraw();
    };
    img.src = imageUrl;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [imageUrl]);

  function redraw() {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
  }

  function toCanvasCoords(e: React.PointerEvent) {
    const canvas = canvasRef.current!;
    const rect = canvas.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * canvas.width;
    const y = ((e.clientY - rect.top) / rect.height) * canvas.height;
    return { x, y };
  }

  function paint(e: React.PointerEvent) {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const { x, y } = toCanvasCoords(e);
    ctx.fillStyle = "rgba(239,68,68,0.55)"; // red overlay = mask preview
    ctx.beginPath();
    ctx.arc(x, y, brush, 0, Math.PI * 2);
    ctx.fill();
    setPainted(true);
  }

  // Track painted points separately so we can render a clean B/W mask on export.
  const strokes = useRef<{ x: number; y: number; r: number }[]>([]);

  function record(e: React.PointerEvent) {
    const { x, y } = toCanvasCoords(e);
    strokes.current.push({ x, y, r: brush });
  }

  useImperativeHandle(ref, () => ({
    clear: () => {
      strokes.current = [];
      setPainted(false);
      redraw();
    },
    getMaskBlob: async () => {
      if (strokes.current.length === 0) return null;
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const mask = document.createElement("canvas");
      mask.width = canvas.width;
      mask.height = canvas.height;
      const ctx = mask.getContext("2d");
      if (!ctx) return null;
      ctx.fillStyle = "#000000";
      ctx.fillRect(0, 0, mask.width, mask.height);
      ctx.fillStyle = "#ffffff";
      for (const s of strokes.current) {
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fill();
      }
      return await new Promise<Blob | null>((resolve) =>
        mask.toBlob((b) => resolve(b), "image/png"),
      );
    },
  }));

  return (
    <div className={className}>
      <canvas
        ref={canvasRef}
        className="max-w-full rounded border border-white/10 touch-none cursor-crosshair"
        onPointerDown={(e) => {
          drawing.current = true;
          (e.target as HTMLElement).setPointerCapture(e.pointerId);
          record(e);
          paint(e);
        }}
        onPointerMove={(e) => {
          if (!drawing.current) return;
          record(e);
          paint(e);
        }}
        onPointerUp={() => {
          drawing.current = false;
        }}
        onPointerLeave={() => {
          drawing.current = false;
        }}
      />
      <div className="mt-2 flex items-center gap-3 text-xs text-zinc-400">
        <label className="flex items-center gap-2">
          Кисть
          <input
            type="range"
            min={8}
            max={120}
            value={brush}
            onChange={(e) => setBrush(Number(e.target.value))}
          />
        </label>
        <button
          type="button"
          className="px-2 py-1 rounded bg-white/5 hover:bg-white/10"
          onClick={() => {
            strokes.current = [];
            setPainted(false);
            redraw();
          }}
        >
          Очистить маску
        </button>
        <span>
          {painted ? "Область выделена" : "Закрасьте область для правки"}
        </span>
      </div>
    </div>
  );
});

export default MaskCanvas;
