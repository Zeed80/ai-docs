"use client";

import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  CadIr,
  CadCertification,
  CadPipelineManifest,
  Generation,
  IrEntity,
  IrLineClass,
  IrPatchOp,
  ReleaseManifest,
  approveCadAsDrafter,
  approveCadAsNormcontroller,
  artifactUrl,
  getIr,
  getCadCertification,
  getReleaseManifest,
  patchIr,
  releasePackageUrl,
  revertIr,
  runFullCheck,
  solveIr,
  sourceUrl,
} from "@/lib/studio-api";

import Cad3dPanel from "@/components/cad/Cad3dPanel";
import CommandLine, { CommandPrompt } from "@/components/cad/CommandLine";
import EntityShape from "@/components/cad/EntityShape";
import ReviewPanel from "@/components/cad/ReviewPanel";
import LayersPanel from "@/components/cad/LayersPanel";
import ConstraintsPanel from "@/components/cad/ConstraintsPanel";
import StatusBar from "@/components/cad/StatusBar";
import AnnotationsPanel from "@/components/cad/AnnotationsPanel";
import TitleBlockPanel from "@/components/cad/TitleBlockPanel";
import ValidationPanel from "@/components/cad/ValidationPanel";
import {
  DIM_TOOL_KIND,
  Tool,
  dimArrowLenPx,
  dimensionArrowPolygons,
  entityLabel,
  isUnresolvedCritical,
  parseCoordinate,
  selectByRect,
  snapPoint,
} from "@/components/cad/geometry";
import { ASSURANCE_COLOR } from "@/components/cad/geometry";

interface Props {
  gen: Generation;
  onChanged: () => void;
}

function AssuranceBadge({
  assurance,
  t,
}: {
  assurance: string;
  t: (k: string) => string;
}) {
  return (
    <span
      className={`px-1.5 py-0.5 rounded bg-white/5 text-[10px] ${ASSURANCE_COLOR[assurance] ?? "text-zinc-400"}`}
    >
      {t(`vector.assurance_${assurance}`)}
    </span>
  );
}

// Command-line aliases → tool. RU/EN, AutoCAD-style short forms.
const COMMAND_TOOLS: Record<string, Tool> = {
  v: "select",
  select: "select",
  выбор: "select",
  pan: "pan",
  панорама: "pan",
  l: "line",
  line: "line",
  линия: "line",
  отрезок: "line",
  c: "circle",
  circle: "circle",
  окружность: "circle",
  t: "text",
  text: "text",
  текст: "text",
  d: "dim_linear",
  dim: "dim_linear",
  размер: "dim_linear",
  o: "dim_diameter",
  diameter: "dim_diameter",
  диаметр: "dim_diameter",
  r: "dim_radial",
  radius: "dim_radial",
  радиус: "dim_radial",
  p: "polyline",
  polyline: "polyline",
  полилиния: "polyline",
  h: "hatch",
  hatch: "hatch",
  штриховка: "hatch",
  mirror: "mirror",
  зеркало: "mirror",
  fillet: "fillet",
  скругление: "fillet",
  chamfer: "chamfer",
  фаска: "chamfer",
  trim: "trim",
  обрезать: "trim",
  extend: "extend",
  продлить: "extend",
  offset: "offset",
  смещение: "offset",
  подобие: "offset",
  split: "split",
  разделить: "split",
  разбить: "split",
  join: "join",
  соединить: "join",
  array: "pattern_linear",
  массив: "pattern_linear",
  parray: "pattern_polar",
  полярный: "pattern_polar",
};

export default function CadWorkspace({ gen, onChanged }: Props) {
  const t = useTranslations("studio");
  const [ir, setIr] = useState<CadIr | null>(null);
  const [revision, setRevision] = useState<number>(0);
  const [busy, setBusy] = useState(false);
  // Separate from `busy`: full-check (LLM/VLM review levels) can run up to
  // ~180s server-side, unlike every other apply() here which is a fast
  // deterministic PATCH — the button needs its own elapsed-time readout so
  // a long wait doesn't look identical to a hang.
  const [fullCheckRunning, setFullCheckRunning] = useState(false);
  const [fullCheckElapsed, setFullCheckElapsed] = useState(0);
  const [fullCheckedRevision, setFullCheckedRevision] = useState<number | null>(
    typeof gen.params?.full_check_revision === "number"
      ? gen.params.full_check_revision
      : null,
  );
  const [err, setErr] = useState<string | null>(null);
  const [release, setRelease] = useState<ReleaseManifest | null>(null);
  const [certification, setCertification] = useState<CadCertification | null>(null);
  // Multi-select (A3): the whole selection set; `selected` below is the
  // single-entity view used by the property grid when exactly one is picked.
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [tool, setTool] = useState<Tool>("select");
  const [showSource, setShowSource] = useState(true);
  const [sourceOpacity, setSourceOpacity] = useState(0.45);
  const hasNormalizedSource = Boolean(gen.params?.normalized_source_path);
  const [sourceVariant, setSourceVariant] = useState<"original" | "normalized">(
    hasNormalizedSource ? "normalized" : "original",
  );
  const [viewBox, setViewBox] = useState<{
    x: number;
    y: number;
    width: number;
    height: number;
  } | null>(null);
  const [panStart, setPanStart] = useState<{
    clientX: number;
    clientY: number;
    viewBox: { x: number; y: number; width: number; height: number };
  } | null>(null);
  const [draftStart, setDraftStart] = useState<{ x: number; y: number } | null>(
    null,
  );
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [scaleInput, setScaleInput] = useState("");
  const [textInput, setTextInput] = useState("");
  const [visibleLayers, setVisibleLayers] = useState<Set<IrLineClass>>(
    () => new Set(["contour", "thin", "axis", "hidden", "dim", "hatch"]),
  );
  // I4: AutoCAD-style layer state. Frozen = neither drawn nor selectable
  // (stronger than hidden); locked = drawn but not selectable/editable.
  const [frozenLayers, setFrozenLayers] = useState<Set<IrLineClass>>(
    () => new Set(),
  );
  const [lockedLayers, setLockedLayers] = useState<Set<IrLineClass>>(
    () => new Set(),
  );
  const [mirrorTargetId, setMirrorTargetId] = useState<string | null>(null);
  const [pickedSegmentId, setPickedSegmentId] = useState<string | null>(null);
  // A2: an in-progress sketch op waiting on a second canvas click (offset side,
  // polar-pattern centre).
  const [sketchPending, setSketchPending] = useState<
    | { op: "offset"; entityId: string; value: number }
    | { op: "pattern_polar"; entityId: string }
    | { op: "insert_block"; name: string }
    | null
  >(null);
  const [polylinePoints, setPolylinePoints] = useState<
    { x: number; y: number }[]
  >([]);
  const [osnap, setOsnap] = useState(true);
  const [ortho, setOrtho] = useState(false);
  // Window/crossing marquee (select tool): drag start + live corner.
  const [marquee, setMarquee] = useState<{
    start: { x: number; y: number };
    end: { x: number; y: number };
    shift: boolean;
  } | null>(null);
  const suppressClickRef = useRef(false);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // Command line value requests (replaces window.prompt): the pending
  // question + its promise resolver.
  const [cmdPrompt, setCmdPrompt] = useState<CommandPrompt | null>(null);
  const promptResolver = useRef<((v: string | null) => void) | null>(null);

  const requestValue = useCallback(
    (message: string, defaultValue?: string): Promise<string | null> =>
      new Promise((resolve) => {
        promptResolver.current?.(null);
        promptResolver.current = resolve;
        setCmdPrompt({ message, defaultValue });
      }),
    [],
  );

  const resolvePrompt = useCallback((value: string | null) => {
    const resolver = promptResolver.current;
    promptResolver.current = null;
    setCmdPrompt(null);
    resolver?.(value);
  }, []);

  // Undo/redo (Ф5.2): a session-local pointer into the revision numbers this
  // editor has visited, in logical (not raw DB-counter) order. Every real
  // edit truncates anything past the pointer and appends; undo/redo just
  // move the pointer and ask the backend to re-surface that revision's
  // content as a NEW row (POST .../ir/revert) — history is append-only on
  // the server, this is purely a client-side navigation convenience.
  const historyRef = useRef<number[]>([]);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const canUndo = historyIndex > 0;
  const canRedo =
    historyIndex >= 0 && historyIndex < historyRef.current.length - 1;

  const hasSource = (gen.source_image_paths?.length ?? 0) > 0;

  const load = useCallback(async () => {
    try {
      const env = await getIr(gen.id);
      const certificate = await getCadCertification(gen.id);
      setIr(env.ir);
      setRevision(env.revision);
      setCertification(certificate);
      setFullCheckedRevision(
        typeof gen.params?.full_check_revision === "number"
          ? gen.params.full_check_revision
          : null,
      );
      setViewBox(
        (current) =>
          current ?? {
            x: 0,
            y: 0,
            width: env.ir.source.image_width,
            height: env.ir.source.image_height,
          },
      );
      setErr(null);
      historyRef.current = [env.revision];
      setHistoryIndex(0);
    } catch (e) {
      setErr(String((e as Error).message || e));
    }
  }, [gen.id, gen.params?.full_check_revision]);

  useEffect(() => {
    void load();
  }, [load]);

  // C5: fetch the release manifest once the drawing is accepted, so the
  // reproducibility + approval trail shows next to the download button.
  useEffect(() => {
    if (!gen.accepted) {
      setRelease(null);
      return;
    }
    let cancelled = false;
    getReleaseManifest(gen.id)
      .then((m) => !cancelled && setRelease(m))
      .catch(() => !cancelled && setRelease(null));
    return () => {
      cancelled = true;
    };
  }, [gen.accepted, gen.id, revision]);

  function clearDrafts() {
    setDraftStart(null);
    setPickedSegmentId(null);
    setMirrorTargetId(null);
    setPolylinePoints([]);
    setSketchPending(null);
  }

  function selectOnly(id: string | null) {
    setSelection(id ? new Set([id]) : new Set());
  }

  async function undo() {
    if (!canUndo) return;
    const target = historyRef.current[historyIndex - 1];
    setBusy(true);
    setErr(null);
    try {
      const env = await revertIr(gen.id, target);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(null);
      setHistoryIndex((i) => i - 1);
      selectOnly(null);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function redo() {
    if (!canRedo) return;
    const target = historyRef.current[historyIndex + 1];
    setBusy(true);
    setErr(null);
    try {
      const env = await revertIr(gen.id, target);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(null);
      setHistoryIndex((i) => i + 1);
      selectOnly(null);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  const pending = useMemo(
    () => (ir?.review ?? []).filter((r) => !r.resolved),
    [ir],
  );
  const unresolved = useMemo(
    () => (ir?.unresolved_regions ?? []).filter((region) => !region.resolved),
    [ir],
  );
  const flaggedIds = useMemo(
    () => new Set(pending.map((r) => r.entity_id)),
    [pending],
  );
  const pendingIds = flaggedIds;
  const issues = useMemo(() => ir?.validation.issues ?? [], [ir]);
  const blocking = issues.filter((i) => i.severity === "error");
  const fullCheckCurrent = fullCheckedRevision === revision;
  const criticalUnresolved = useMemo(
    () =>
      (ir?.entities ?? []).filter((e) => isUnresolvedCritical(e, pendingIds)),
    [ir, pendingIds],
  );
  const selectedId = selection.size === 1 ? Array.from(selection)[0] : null;
  const selected = ir?.entities.find((e) => e.id === selectedId) ?? null;
  const selectedList = useMemo(
    () => (ir ? ir.entities.filter((e) => selection.has(e.id)) : []),
    [ir, selection],
  );

  function focusEntity(entityId: string) {
    if (!ir) return;
    const entity = ir.entities.find((item) => item.id === entityId);
    if (!entity) return;
    const points: { x: number; y: number }[] = [];
    if (entity.p1) points.push(entity.p1);
    if (entity.p2) points.push(entity.p2);
    if (entity.position) points.push(entity.position);
    if (entity.points) points.push(...entity.points);
    if (entity.boundary) points.push(...entity.boundary);
    if (entity.center) {
      const radius = entity.radius ?? 8;
      points.push(
        { x: entity.center.x - radius, y: entity.center.y - radius },
        { x: entity.center.x + radius, y: entity.center.y + radius },
      );
    }
    selectOnly(entityId);
    if (points.length === 0) return;
    const xs = points.map((point) => point.x);
    const ys = points.map((point) => point.y);
    const padding = Math.max(20, ir.source.image_width / 50);
    const width = Math.min(
      ir.source.image_width,
      Math.max(
        Math.max(...xs) - Math.min(...xs) + padding * 2,
        ir.source.image_width / 8,
      ),
    );
    const height = width * (ir.source.image_height / ir.source.image_width);
    const centerX = (Math.min(...xs) + Math.max(...xs)) / 2;
    const centerY = (Math.min(...ys) + Math.max(...ys)) / 2;
    setViewBox({
      x: Math.max(
        0,
        Math.min(ir.source.image_width - width, centerX - width / 2),
      ),
      y: Math.max(
        0,
        Math.min(ir.source.image_height - height, centerY - height / 2),
      ),
      width,
      height,
    });
  }

  async function apply(ops: IrPatchOp[]) {
    setBusy(true);
    setErr(null);
    try {
      const env = await patchIr(gen.id, ops);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(null);
      // A fresh edit after undo abandons the redo branch — standard
      // undo/redo semantics (matches browser/editor behavior).
      setHistoryIndex((i) => {
        const truncated = historyRef.current.slice(0, i + 1);
        truncated.push(env.revision);
        historyRef.current = truncated;
        return truncated.length - 1;
      });
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function onRunFullCheck() {
    setBusy(true);
    setErr(null);
    setFullCheckRunning(true);
    setFullCheckElapsed(0);
    const timer = window.setInterval(
      () => setFullCheckElapsed((s) => s + 1),
      1000,
    );
    try {
      const env = await runFullCheck(gen.id);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(env.revision);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      window.clearInterval(timer);
      setFullCheckRunning(false);
      setBusy(false);
    }
  }

  async function onSolve() {
    setBusy(true);
    setErr(null);
    try {
      const env = await solveIr(gen.id);
      setIr(env.ir);
      setRevision(env.revision);
      setFullCheckedRevision(null);
      onChanged();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  // Keyboard-first UX (Ф5.3): tool switching, delete, escape, undo/redo.
  // Ignored while typing in a text field (scale/label/command inputs) so
  // shortcuts don't fire mid-keystroke.
  useEffect(() => {
    function onKeyDown(ev: KeyboardEvent) {
      const target = ev.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName))
        return;
      const mod = ev.ctrlKey || ev.metaKey;
      if (mod && ev.key.toLowerCase() === "z") {
        ev.preventDefault();
        if (ev.shiftKey) void redo();
        else void undo();
        return;
      }
      if (mod && ev.key.toLowerCase() === "y") {
        ev.preventDefault();
        void redo();
        return;
      }
      if (ev.key === "Escape") {
        clearDrafts();
        selectOnly(null);
        setMarquee(null);
        return;
      }
      if ((ev.key === "Delete" || ev.key === "Backspace") && selection.size) {
        ev.preventDefault();
        const ids = Array.from(selection);
        selectOnly(null);
        void apply(ids.map((id) => ({ op: "delete" as const, entity_id: id })));
        return;
      }
      if (
        tool === "select" &&
        selection.size &&
        ir &&
        ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(ev.key)
      ) {
        ev.preventDefault();
        const step = (ev.shiftKey ? 10 : 1) * (ir.source.image_width / 400);
        const [dx, dy] = {
          ArrowUp: [0, -step],
          ArrowDown: [0, step],
          ArrowLeft: [-step, 0],
          ArrowRight: [step, 0],
        }[ev.key] as [number, number];
        void apply(
          Array.from(selection).map((id) => ({
            op: "move" as const,
            entity_id: id,
            dx,
            dy,
          })),
        );
        return;
      }
      const toolKeys: Record<string, Tool> = {
        v: "select",
        l: "line",
        c: "circle",
        t: "text",
        d: "dim_linear",
        o: "dim_diameter",
        r: "dim_radial",
        p: "polyline",
        h: "hatch",
      };
      const next = toolKeys[ev.key.toLowerCase()];
      if (next) {
        setTool(next);
        clearDrafts();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  function finishPolyline() {
    // Same in-flight guard as canvasClick — dblclick is a distinct handler
    // from the click chain that builds polylinePoints, so it isn't covered
    // by the guard added there.
    if (busy) return;
    let pts = polylinePoints;
    if (pts.length >= 2) {
      const last = pts[pts.length - 1];
      const secondLast = pts[pts.length - 2];
      // Double-click fires two "click"s at the same spot before dblclick —
      // drop the accidental duplicate point that leaves behind.
      if (Math.hypot(last.x - secondLast.x, last.y - secondLast.y) < 1e-6) {
        pts = pts.slice(0, -1);
      }
    }
    if (pts.length >= 2) {
      void apply([
        {
          op: "add",
          entity: {
            type: "polyline",
            points: pts,
            closed: false,
            line_class: "contour",
            width_class: "main",
          },
        },
      ]);
    }
    setPolylinePoints([]);
  }

  function svgPoint(ev: React.MouseEvent): { x: number; y: number } | null {
    const svg = svgRef.current;
    if (!svg || !ir || !viewBox) return null;
    const rect = svg.getBoundingClientRect();
    const x =
      viewBox.x + ((ev.clientX - rect.left) / rect.width) * viewBox.width;
    const y =
      viewBox.y + ((ev.clientY - rect.top) / rect.height) * viewBox.height;
    return { x, y };
  }

  /** One drawing point arriving either from a canvas click (useSnap=true)
   * or from an exact command-line coordinate (useSnap=false). */
  async function handlePoint(raw: { x: number; y: number }, useSnap: boolean) {
    if (!ir || busy) return;
    const tol = ir.source.image_width / 80;
    if (tool === "hatch") {
      void apply([{ op: "hatch_click", click_x: raw.x, click_y: raw.y }]);
      return;
    }
    if (tool === "polyline") {
      const anchor = polylinePoints[polylinePoints.length - 1] ?? null;
      const pp = useSnap
        ? snapPoint(ir, raw.x, raw.y, anchor, tol, { osnap, ortho })
        : raw;
      if (polylinePoints.length >= 2) {
        const first = polylinePoints[0];
        if (Math.hypot(pp.x - first.x, pp.y - first.y) < tol) {
          void apply([
            {
              op: "add",
              entity: {
                type: "polyline",
                points: polylinePoints,
                closed: true,
                line_class: "contour",
                width_class: "main",
              },
            },
          ]);
          setPolylinePoints([]);
          return;
        }
      }
      setPolylinePoints([...polylinePoints, pp]);
      return;
    }
    const p = useSnap
      ? snapPoint(ir, raw.x, raw.y, draftStart, tol, { osnap, ortho })
      : raw;
    if (tool === "text") {
      const text =
        textInput.trim() || (await requestValue(t("vector.text_prompt"))) || "";
      if (!text.trim()) return;
      void apply([
        {
          op: "add",
          entity: {
            type: "text",
            position: { x: p.x, y: p.y },
            text: text.trim(),
            height: 14,
            line_class: "dim",
            width_class: "thin",
          },
        },
      ]);
      return;
    }
    if (!draftStart) {
      setDraftStart(p);
      return;
    }
    if (tool === "line") {
      void apply([
        {
          op: "add",
          entity: {
            type: "segment",
            p1: { x: draftStart.x, y: draftStart.y },
            p2: { x: p.x, y: p.y },
            line_class: "contour",
            width_class: "main",
          },
        },
      ]);
    } else if (tool === "circle") {
      const r = Math.hypot(p.x - draftStart.x, p.y - draftStart.y);
      if (r > 1) {
        void apply([
          {
            op: "add",
            entity: {
              type: "circle",
              center: { x: draftStart.x, y: draftStart.y },
              radius: r,
              line_class: "contour",
              width_class: "main",
            },
          },
        ]);
      }
    } else if (tool in DIM_TOOL_KIND) {
      const kind = DIM_TOOL_KIND[tool];
      const lengthPx = Math.hypot(p.x - draftStart.x, p.y - draftStart.y);
      const defaultVal = ir.scale ? (lengthPx * ir.scale).toFixed(1) : "";
      const typed = await requestValue(
        t("vector.dimension_prompt"),
        defaultVal,
      );
      if (typed !== null && typed.trim()) {
        const text = typed.trim();
        const numMatch = text.replace(",", ".").match(/[\d.]+/);
        void apply([
          {
            op: "add",
            entity: {
              type: "dimension",
              kind,
              p1: { x: draftStart.x, y: draftStart.y },
              p2: { x: p.x, y: p.y },
              text,
              value_mm: numMatch ? Number(numMatch[0]) : null,
              line_class: "dim",
              width_class: "thin",
            },
          },
        ]);
      }
    } else if (tool === "mirror") {
      if (mirrorTargetId) {
        void apply([
          {
            op: "mirror",
            entity_id: mirrorTargetId,
            mirror_p1: { x: draftStart.x, y: draftStart.y },
            mirror_p2: { x: p.x, y: p.y },
          },
        ]);
      }
      setMirrorTargetId(null);
      setTool("select");
    }
    setDraftStart(null);
  }

  function canvasClick(ev: React.MouseEvent) {
    if (!ir) return;
    // A previous apply() (PATCH /ir) is still in flight — a rapid double-
    // click (or clicking again before the network round-trip finishes)
    // would otherwise fire a second concurrent PATCH for the same
    // generation. Toolbar buttons already have this via disabled={busy};
    // the canvas itself didn't.
    if (busy) return;
    // A marquee drag just completed — the browser still fires a click.
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    const raw = svgPoint(ev);
    if (!raw) return;
    if (tool === "pan") return;
    // A2: second click of an offset / polar-pattern — the point IS the side or
    // the rotation centre.
    if (sketchPending) {
      if (sketchPending.op === "offset") {
        void apply([
          {
            op: "offset",
            entity_id: sketchPending.entityId,
            value: sketchPending.value,
            click_x: raw.x,
            click_y: raw.y,
          },
        ]);
        setSketchPending(null);
        setErr(null);
        return;
      }
      if (sketchPending.op === "insert_block") {
        // Every subsequent click stamps another instance; switching tools or
        // pressing Esc (clearDrafts) ends the insert session.
        void apply([
          {
            op: "insert_block",
            block_name: sketchPending.name,
            click_x: raw.x,
            click_y: raw.y,
          },
        ]);
        setErr(null);
        return;
      }
      const entityId = sketchPending.entityId;
      const center = raw;
      setSketchPending(null);
      void (async () => {
        const typed = await requestValue(
          t("vector.pattern_polar_prompt"),
          "6, 360",
        );
        if (typed === null) return;
        const [cStr, aStr] = typed.split(/[ ,;]+/);
        const count = parseInt(cStr, 10);
        const angle = Number((aStr ?? "360").replace(",", "."));
        if (count >= 2 && Number.isFinite(angle) && angle !== 0) {
          void apply([
            {
              op: "pattern_polar",
              entity_id: entityId,
              count,
              click_x: center.x,
              click_y: center.y,
              value: angle,
            },
          ]);
          setErr(null);
        } else {
          setErr(t("vector.pattern_bad_input"));
        }
      })();
      return;
    }
    if (tool === "select") {
      if (!ev.shiftKey) selectOnly(null);
      return;
    }
    if (
      tool === "fillet" ||
      tool === "chamfer" ||
      tool === "trim" ||
      tool === "extend" ||
      tool === "join"
    ) {
      // Clicking blank canvas cancels an in-progress pick — the actual
      // picks happen on EntityShape's onClick (segments only).
      setPickedSegmentId(null);
      return;
    }
    void handlePoint(raw, true);
  }

  async function entityClick(id: string, ev: React.MouseEvent) {
    if (!ir) return;
    // I4: entities on a locked or frozen layer are inert — no select, no edit.
    const clickedLine = ir.entities.find((x) => x.id === id)?.line_class;
    if (
      clickedLine &&
      (lockedLayers.has(clickedLine) || frozenLayers.has(clickedLine))
    )
      return;
    // Same in-flight guard as canvasClick — this is a separate handler
    // (EntityShape's onClick, not the SVG's onClick) so it needs its own
    // check before the fillet/chamfer second pick can fire apply().
    if (busy) return;
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    if (tool === "select") {
      if (ev.shiftKey) {
        setSelection((current) => {
          const next = new Set(current);
          if (next.has(id)) next.delete(id);
          else next.add(id);
          return next;
        });
      } else {
        selectOnly(id === selectedId ? null : id);
      }
      return;
    }
    if (tool === "fillet" || tool === "chamfer") {
      const clicked = ir.entities.find((x) => x.id === id);
      if (!clicked || clicked.type !== "segment") {
        // Previously silent no-op — a click that visibly does nothing reads
        // as a broken tool, not "wrong pick, try again". Surface it in the
        // same error banner apply() failures use, without cancelling an
        // already-picked first segment.
        setErr(t("vector.fillet_chamfer_not_a_segment"));
        return;
      }
      setErr(null);
      if (!pickedSegmentId) {
        setPickedSegmentId(id);
        return;
      }
      if (pickedSegmentId === id) return;
      const label =
        tool === "fillet"
          ? t("vector.fillet_prompt")
          : t("vector.chamfer_prompt");
      const typed = await requestValue(label, "5");
      const value = typed ? Number(typed.replace(",", ".")) : NaN;
      const firstId = pickedSegmentId;
      setPickedSegmentId(null);
      if (typed !== null && !Number.isNaN(value) && value > 0) {
        void apply([
          {
            op: tool,
            entity_id: firstId,
            entity_id_2: id,
            value,
          },
        ]);
      }
      return;
    }
    if (tool === "trim" || tool === "extend") {
      const clicked = ir.entities.find((x) => x.id === id);
      if (!clicked || clicked.type !== "segment") {
        setErr(t("vector.sketch_not_a_segment"));
        return;
      }
      setErr(null);
      // First pick = the cutting edge / boundary; second pick = the segment to
      // trim/extend, and where you clicked chooses the side.
      if (!pickedSegmentId) {
        setPickedSegmentId(id);
        return;
      }
      if (pickedSegmentId === id) return;
      const pt = svgPoint(ev);
      const cutterId = pickedSegmentId;
      setPickedSegmentId(null);
      if (pt) {
        void apply([
          {
            op: tool,
            entity_id: id,
            entity_id_2: cutterId,
            click_x: pt.x,
            click_y: pt.y,
          },
        ]);
      }
      return;
    }
    if (tool === "offset") {
      const clicked = ir.entities.find((x) => x.id === id);
      if (!clicked || !["segment", "circle", "arc"].includes(clicked.type)) {
        setErr(t("vector.offset_bad_target"));
        return;
      }
      const typed = await requestValue(t("vector.offset_prompt"), "10");
      const value = typed ? Number(typed.replace(",", ".")) : NaN;
      if (typed === null || !(value > 0)) return;
      setSketchPending({ op: "offset", entityId: id, value });
      setErr(t("vector.offset_pick_side"));
      return;
    }
    if (tool === "pattern_linear") {
      const typed = await requestValue(
        t("vector.pattern_linear_prompt"),
        "3, 30, 0",
      );
      if (typed === null) return;
      const [cStr, dxStr, dyStr] = typed.split(/[ ,;]+/);
      const count = parseInt(cStr, 10);
      const dx = Number((dxStr ?? "").replace(",", "."));
      const dy = Number((dyStr ?? "").replace(",", "."));
      if (!(count >= 2) || !Number.isFinite(dx) || !Number.isFinite(dy)) {
        setErr(t("vector.pattern_bad_input"));
        return;
      }
      void apply([{ op: "pattern_linear", entity_id: id, count, dx, dy }]);
      return;
    }
    if (tool === "pattern_polar") {
      setSketchPending({ op: "pattern_polar", entityId: id });
      setErr(t("vector.pattern_polar_pick_center"));
      return;
    }
    if (tool === "split") {
      const clicked = ir.entities.find((x) => x.id === id);
      if (!clicked || clicked.type !== "segment") {
        setErr(t("vector.sketch_not_a_segment"));
        return;
      }
      const pt = svgPoint(ev);
      if (pt)
        void apply([
          { op: "split", entity_id: id, click_x: pt.x, click_y: pt.y },
        ]);
      return;
    }
    if (tool === "join") {
      const clicked = ir.entities.find((x) => x.id === id);
      if (!clicked || clicked.type !== "segment") {
        setErr(t("vector.sketch_not_a_segment"));
        return;
      }
      setErr(null);
      if (!pickedSegmentId) {
        setPickedSegmentId(id);
        return;
      }
      if (pickedSegmentId === id) return;
      const firstId = pickedSegmentId;
      setPickedSegmentId(null);
      void apply([{ op: "join", entity_id: firstId, entity_id_2: id }]);
      return;
    }
  }

  /** Free-form command line input: tool aliases, actions or a coordinate. */
  function runCommand(text: string) {
    if (!ir || !text) return;
    const lower = text.toLowerCase();
    const nextTool = COMMAND_TOOLS[lower];
    if (nextTool) {
      setTool(nextTool);
      clearDrafts();
      setErr(null);
      return;
    }
    if (["undo", "u", "отмена"].includes(lower)) {
      void undo();
      return;
    }
    if (["redo", "повтор"].includes(lower)) {
      void redo();
      return;
    }
    if (["delete", "del", "e", "удалить"].includes(lower)) {
      if (selection.size) {
        const ids = Array.from(selection);
        selectOnly(null);
        void apply(ids.map((id) => ({ op: "delete" as const, entity_id: id })));
      }
      return;
    }
    if (["confirm", "подтвердить", "ок"].includes(lower)) {
      if (selection.size) {
        void apply(
          Array.from(selection).map((id) => ({
            op: "confirm" as const,
            entity_id: id,
          })),
        );
      }
      return;
    }
    // A2 blocks: `block <имя>` snapshots the selection as a named block;
    // `insert <имя>` starts click-to-stamp insertion; `blocks` lists them.
    const blockMatch = lower.match(/^(?:block|блок)\s+(.+)$/);
    if (blockMatch) {
      if (!selection.size) {
        setErr(t("vector.block_needs_selection"));
        return;
      }
      void apply([
        {
          op: "define_block",
          block_name: blockMatch[1].trim(),
          entity_ids: Array.from(selection),
        },
      ]);
      return;
    }
    const insertMatch = lower.match(/^(?:insert|вставить)\s+(.+)$/);
    if (insertMatch) {
      const name = insertMatch[1].trim();
      if (!(ir.blocks ?? []).some((b) => b.name.toLowerCase() === name)) {
        setErr(t("vector.block_not_found", { name }));
        return;
      }
      setSketchPending({ op: "insert_block", name });
      setErr(t("vector.block_pick_point", { name }));
      return;
    }
    if (["blocks", "блоки"].includes(lower)) {
      const names = (ir.blocks ?? []).map((b) => b.name).join(", ");
      setErr(
        names
          ? t("vector.block_list", { names })
          : t("vector.block_list_empty"),
      );
      return;
    }
    if (["construction", "вспом", "вспомогательная", "cons"].includes(lower)) {
      // A2: toggle the selection between real and auxiliary (construction)
      // geometry — the latter guides the drawing but is excluded from export.
      if (selection.size) {
        void apply(
          Array.from(selection).map((id) => ({
            op: "set_construction" as const,
            entity_id: id,
          })),
        );
      }
      return;
    }
    if (["fit", "f", "показать всё", "вписать"].includes(lower)) {
      setViewBox({
        x: 0,
        y: 0,
        width: ir.source.image_width,
        height: ir.source.image_height,
      });
      return;
    }
    // Coordinate entry: absolute `100,50`, relative `@50,0`, polar `@50<45`
    // (mm when the sheet has a scale, px otherwise).
    const anchor =
      tool === "polyline"
        ? (polylinePoints[polylinePoints.length - 1] ?? null)
        : draftStart;
    const point = parseCoordinate(text, anchor, ir.scale);
    if (point) {
      void handlePoint(point, false);
      return;
    }
    setErr(t("vector.cmd_unknown", { cmd: text }));
  }

  if (!ir) {
    return (
      <div className="text-xs text-zinc-400">
        {err ? (
          <span className="text-red-400">{err}</span>
        ) : (
          t("vector.loading")
        )}
      </div>
    );
  }

  const activeViewBox = viewBox ?? {
    x: 0,
    y: 0,
    width: ir.source.image_width,
    height: ir.source.image_height,
  };
  const vb = `${activeViewBox.x} ${activeViewBox.y} ${activeViewBox.width} ${activeViewBox.height}`;
  const toolButtons: { key: Tool; label: string }[] = [
    { key: "select", label: t("vector.tool_select") },
    { key: "pan", label: t("vector.tool_pan") },
    { key: "line", label: t("vector.tool_line") },
    { key: "circle", label: t("vector.tool_circle") },
    { key: "text", label: t("vector.tool_text") },
    { key: "dim_linear", label: t("vector.tool_dim_linear") },
    { key: "dim_diameter", label: t("vector.tool_dim_diameter") },
    { key: "dim_radial", label: t("vector.tool_dim_radial") },
    { key: "fillet", label: t("vector.tool_fillet") },
    { key: "chamfer", label: t("vector.tool_chamfer") },
    { key: "trim", label: t("vector.tool_trim") },
    { key: "extend", label: t("vector.tool_extend") },
    { key: "offset", label: t("vector.tool_offset") },
    { key: "split", label: t("vector.tool_split") },
    { key: "join", label: t("vector.tool_join") },
    { key: "pattern_linear", label: t("vector.tool_pattern_linear") },
    { key: "pattern_polar", label: t("vector.tool_pattern_polar") },
    { key: "polyline", label: t("vector.tool_polyline") },
    { key: "hatch", label: t("vector.tool_hatch") },
  ];
  const layerClasses: IrLineClass[] = [
    "contour",
    "thin",
    "axis",
    "hidden",
    "dim",
    "hatch",
  ];
  const layerColors: Record<IrLineClass, string> = {
    contour: "#f4f4f5",
    thin: "#a1a1aa",
    axis: "#22d3ee",
    hidden: "#a78bfa",
    dim: "#facc15",
    hatch: "#34d399",
  };
  const layerCounts = ir.entities.reduce<Record<string, number>>((acc, e) => {
    acc[e.line_class] = (acc[e.line_class] ?? 0) + 1;
    return acc;
  }, {});
  const toggleInSet =
    (setter: typeof setVisibleLayers) => (layer: IrLineClass) =>
      setter((current) => {
        const next = new Set(current);
        if (next.has(layer)) next.delete(layer);
        else next.add(layer);
        return next;
      });
  const arrowLen = dimArrowLenPx(ir.scale);
  const imageWidth = ir.source.image_width;
  const imageHeight = ir.source.image_height;

  function zoomAt(factor: number, centerX?: number, centerY?: number) {
    setViewBox((current) => {
      if (!current) return current;
      const cx = centerX ?? current.x + current.width / 2;
      const cy = centerY ?? current.y + current.height / 2;
      const width = Math.min(
        imageWidth,
        Math.max(imageWidth / 50, current.width * factor),
      );
      const height = width * (imageHeight / imageWidth);
      const rx = (cx - current.x) / current.width;
      const ry = (cy - current.y) / current.height;
      return {
        x: Math.max(0, Math.min(imageWidth - width, cx - width * rx)),
        y: Math.max(0, Math.min(imageHeight - height, cy - height * ry)),
        width,
        height,
      };
    });
  }

  const marqueeCrossing = marquee ? marquee.end.x < marquee.start.x : false;

  const cmdHint = cmdPrompt
    ? ""
    : t("vector.cmd_hint", { tool: t(`vector.tool_${tool}`) });
  const pipelineManifest = gen.params?.cad_pipeline_manifest as
    | CadPipelineManifest
    | undefined;
  const draftingSpec = gen.params?.spec as
    | { optional_unresolved?: string[]; unresolved?: string[] }
    | undefined;
  const drawingGraph = gen.params?.drawing_graph as
    | {
        graph_status?: string;
        evidence?: unknown[];
        views?: unknown[];
        entities?: unknown[];
        relations?: unknown[];
        unresolved_regions?: Array<{ resolved?: boolean }>;
      }
    | undefined;

  return (
    <div className="flex flex-col gap-3 pb-24 sm:pb-0">
      {/* Header: revision, coverage, scale */}
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-zinc-400">
        <span className="px-2 py-0.5 rounded bg-white/5">
          {t("vector.revision", { n: revision })}
        </span>
        <span
          className={`rounded px-2 py-0.5 ${
            ir.digitization_status === "exact_candidate"
              ? "bg-emerald-500/10 text-emerald-300"
              : ir.digitization_status === "refused"
                ? "bg-red-500/10 text-red-300"
                : "bg-amber-500/10 text-amber-300"
          }`}
        >
          {t(`vector.status_${ir.digitization_status}`)}
        </span>
        {ir.validation.vector_recall != null && (
          <span className="px-2 py-0.5 rounded bg-white/5">
            {t("vector.vector_fidelity", {
              recall: Math.round((ir.validation.vector_recall ?? 0) * 100),
              precision: Math.round(
                (ir.validation.vector_precision ?? 0) * 100,
              ),
            })}
          </span>
        )}
        {unresolved.length > 0 && (
          <span className="rounded bg-red-500/10 px-2 py-0.5 text-red-300">
            {t("vector.unresolved_count", { n: unresolved.length })}
          </span>
        )}
        {ir.scale ? (
          <span className="px-2 py-0.5 rounded bg-white/5">
            {t("vector.scale_known", { scale: ir.scale.toFixed(4) })}
          </span>
        ) : (
          <span className="flex flex-wrap items-center gap-1 px-2 py-0.5 rounded bg-amber-500/10 text-amber-300">
            {t("vector.scale_unknown")}
            {/* B6: one-click sheet-format confirmation — the fast path when
                the drawing has a ГОСТ frame (A-series aspect is ambiguous in
                pixels, so the user just names the format). */}
            {(["A4", "A3", "A2", "A1", "A0"] as const).map((fmt) => (
              <button
                key={fmt}
                disabled={busy}
                onClick={() =>
                  apply([{ op: "set_sheet_format", sheet_format: fmt }])
                }
                title={t("vector.set_sheet_format", { fmt })}
                className="rounded bg-white/5 px-1.5 py-0.5 text-[11px] text-zinc-200 hover:bg-white/15 disabled:opacity-40"
              >
                {fmt}
              </button>
            ))}
            <span className="text-zinc-500">·</span>
            <input
              value={scaleInput}
              onChange={(e) => setScaleInput(e.target.value)}
              placeholder={t("vector.scale_placeholder")}
              className="w-20 rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-[11px] text-zinc-200"
            />
            <button
              disabled={busy || !Number(scaleInput)}
              onClick={() =>
                apply([{ op: "set_scale", scale: Number(scaleInput) }])
              }
              className="text-sky-400 hover:text-sky-300 disabled:opacity-40"
            >
              OK
            </button>
          </span>
        )}
        {pending.length > 0 && (
          <span className="px-2 py-0.5 rounded bg-amber-500/10 text-amber-300">
            {t("vector.review_pending", { n: pending.length })}
          </span>
        )}
      </div>

      {pipelineManifest && (
        <details className="rounded border border-white/10 bg-white/[0.03] px-3 py-2 text-[11px] text-zinc-400">
          <summary className="cursor-pointer text-zinc-300">
            {t("vector.pipeline_manifest")}: {pipelineManifest.pipeline_revision} · {pipelineManifest.config_sha256.slice(0, 12)}…
          </summary>
          <div className="mt-2 grid gap-1 md:grid-cols-2">
            <div>{t("vector.pipeline_profile")}: {pipelineManifest.profile}</div>
            <div>{t("vector.pipeline_method")}: {pipelineManifest.method}</div>
            <div>{t("vector.pipeline_input")}: {pipelineManifest.input_kind}</div>
            <div>{t("vector.pipeline_reader")}: {pipelineManifest.components.spec_reader.models.map((model) => model.key).join(" → ") || "—"}</div>
            {pipelineManifest.components.drawing_graph_reader && (
              <div>{t("vector.pipeline_graph_reader")}: {pipelineManifest.components.drawing_graph_reader.models.map((model) => model.key).join(" → ") || "—"}</div>
            )}
            {pipelineManifest.components.drawing_graph_drafter && (
              <div>{t("vector.pipeline_graph_drafter")}: {pipelineManifest.components.drawing_graph_drafter.version}</div>
            )}
            <div>{t("vector.pipeline_drafter")}: {pipelineManifest.components.spec_drafter.models.map((model) => model.key).join(" → ") || t("vector.pipeline_deterministic")}</div>
            <div>{t("vector.pipeline_contract")}: {pipelineManifest.components.spec_drafter.deterministic_contract}</div>
            <div className="md:col-span-2">
              {t("vector.pipeline_supported_geometry")}: {pipelineManifest.components.spec_drafter.supported_geometry.join(", ")}
            </div>
            <div className="md:col-span-2 font-mono text-[10px]">
              {t("vector.pipeline_reference_cases")}: {pipelineManifest.components.spec_drafter.reference_cases}
            </div>
            <div className="font-mono text-[10px]">source {pipelineManifest.source_sha256?.slice(0, 16) ?? "—"}…</div>
            <Link href="/settings/models" className="text-sky-300 hover:text-sky-200">
              {t("vector.pipeline_change_models")}
            </Link>
          </div>
        </details>
      )}

      {drawingGraph && (
        <div className="rounded border border-sky-400/20 bg-sky-950/20 px-3 py-2 text-xs text-sky-100">
          <div className="font-medium">{t("vector.drawing_graph_title")}</div>
          <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-sky-200/80">
            <span>{t("vector.drawing_graph_status")}: {drawingGraph.graph_status ?? "—"}</span>
            <span>{t("vector.drawing_graph_views")}: {drawingGraph.views?.length ?? 0}</span>
            <span>{t("vector.drawing_graph_entities")}: {drawingGraph.entities?.length ?? 0}</span>
            <span>{t("vector.drawing_graph_relations")}: {drawingGraph.relations?.length ?? 0}</span>
            <span>{t("vector.drawing_graph_evidence")}: {drawingGraph.evidence?.length ?? 0}</span>
            <span>{t("vector.drawing_graph_unresolved")}: {drawingGraph.unresolved_regions?.filter((item) => !item.resolved).length ?? 0}</span>
          </div>
        </div>
      )}

      {draftingSpec?.optional_unresolved && draftingSpec.optional_unresolved.length > 0 && (
        <div className="rounded border border-amber-400/20 bg-amber-950/20 px-3 py-2 text-xs text-amber-100">
          <div className="font-medium">{t("vector.spec_optional_missing")}</div>
          <ul className="mt-1 list-disc pl-4">
            {draftingSpec.optional_unresolved.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-1">
        {toolButtons.map((b) => (
          <button
            key={b.key}
            onClick={() => {
              setTool(b.key);
              clearDrafts();
            }}
            className={`px-2 py-1 rounded text-xs ${
              tool === b.key
                ? "bg-sky-600 text-white"
                : "bg-white/5 text-zinc-300 hover:bg-white/10"
            }`}
          >
            {b.label}
          </button>
        ))}
        {tool === "text" && (
          <input
            value={textInput}
            onChange={(e) => setTextInput(e.target.value)}
            placeholder={t("vector.text_placeholder")}
            className="rounded bg-zinc-900 border border-white/10 px-2 py-1 text-xs text-zinc-200"
          />
        )}
        <button
          disabled={busy || !canUndo}
          onClick={() => void undo()}
          title={t("vector.undo")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10 disabled:opacity-30"
        >
          ↶
        </button>
        <button
          disabled={busy || !canRedo}
          onClick={() => void redo()}
          title={t("vector.redo")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10 disabled:opacity-30"
        >
          ↷
        </button>
        <button
          type="button"
          onClick={() => zoomAt(0.75)}
          title={t("vector.zoom_in")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10"
        >
          +
        </button>
        <button
          type="button"
          onClick={() => zoomAt(1.25)}
          title={t("vector.zoom_out")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10"
        >
          −
        </button>
        <button
          type="button"
          onClick={() =>
            setViewBox({
              x: 0,
              y: 0,
              width: ir.source.image_width,
              height: ir.source.image_height,
            })
          }
          title={t("vector.zoom_fit")}
          className="px-2 py-1 rounded text-xs bg-white/5 text-zinc-300 hover:bg-white/10"
        >
          ⛶
        </button>
        <div className="flex items-center gap-1 border-l border-white/10 pl-2">
          {layerClasses.map((layer) => {
            const visible = visibleLayers.has(layer);
            return (
              <button
                key={layer}
                type="button"
                title={`${visible ? t("vector.layer_hide") : t("vector.layer_show")}: ${t(`vector.line_${layer}`)}`}
                onClick={() =>
                  setVisibleLayers((current) => {
                    const next = new Set(current);
                    if (next.has(layer)) next.delete(layer);
                    else next.add(layer);
                    return next;
                  })
                }
                className={`h-6 w-6 rounded border ${visible ? "border-white/30 bg-white/10" : "border-white/10 opacity-35"}`}
              >
                <span
                  className="mx-auto block h-2.5 w-2.5 rounded-sm"
                  style={{ backgroundColor: layerColors[layer] }}
                />
              </button>
            );
          })}
        </div>
        {hasSource && (
          <div className="ml-auto flex items-center gap-2 text-[11px] text-zinc-400">
            {hasNormalizedSource && showSource && (
              <select
                value={sourceVariant}
                onChange={(e) =>
                  setSourceVariant(e.target.value as "original" | "normalized")
                }
                className="rounded bg-zinc-900 border border-white/10 px-1.5 py-1 text-zinc-200"
              >
                <option value="normalized">
                  {t("vector.source_normalized")}
                </option>
                <option value="original">{t("vector.source_original")}</option>
              </select>
            )}
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={showSource}
                onChange={(e) => setShowSource(e.target.checked)}
              />
              {t("vector.show_source")}
            </label>
            {showSource && (
              // B6: transparency slider to compare the vector overlay against
              // the source raster while reviewing recognition quality.
              <input
                type="range"
                min={0}
                max={100}
                value={Math.round(sourceOpacity * 100)}
                onChange={(e) => setSourceOpacity(Number(e.target.value) / 100)}
                title={t("vector.source_opacity")}
                className="w-20 accent-sky-500"
              />
            )}
          </div>
        )}
      </div>

      {/* Canvas: source photo + entity overlay */}
      <div
        className="relative w-full rounded border border-white/10 bg-white overflow-hidden"
        style={{
          aspectRatio: `${ir.source.image_width} / ${ir.source.image_height}`,
        }}
      >
        <svg
          ref={svgRef}
          viewBox={vb}
          className={`absolute inset-0 h-full w-full ${tool === "pan" ? "cursor-grab" : ""} ${panStart ? "cursor-grabbing" : ""}`}
          onClick={canvasClick}
          onMouseDown={(ev) => {
            if (tool === "pan" && viewBox) {
              ev.preventDefault();
              setPanStart({
                clientX: ev.clientX,
                clientY: ev.clientY,
                viewBox,
              });
              return;
            }
            if (tool === "select") {
              const point = svgPoint(ev);
              if (point)
                setMarquee({
                  start: point,
                  end: point,
                  shift: ev.shiftKey,
                });
            }
          }}
          onMouseUp={() => {
            setPanStart(null);
            if (marquee) {
              const moved = Math.hypot(
                marquee.end.x - marquee.start.x,
                marquee.end.y - marquee.start.y,
              );
              const threshold = activeViewBox.width / 100;
              if (moved > threshold) {
                const crossing = marquee.end.x < marquee.start.x;
                const ids = selectByRect(
                  ir.entities.filter(
                    (e) =>
                      visibleLayers.has(e.line_class) &&
                      !frozenLayers.has(e.line_class) &&
                      !lockedLayers.has(e.line_class),
                  ),
                  {
                    x0: marquee.start.x,
                    y0: marquee.start.y,
                    x1: marquee.end.x,
                    y1: marquee.end.y,
                  },
                  crossing,
                );
                setSelection((current) => {
                  const next = marquee.shift
                    ? new Set(current)
                    : new Set<string>();
                  for (const id of ids) next.add(id);
                  return next;
                });
                // The click event that follows this mouseup must not clear
                // the selection we just made.
                suppressClickRef.current = true;
              }
              setMarquee(null);
            }
          }}
          onMouseLeave={() => {
            setPanStart(null);
            setMarquee(null);
          }}
          onDoubleClick={() => {
            if (tool === "polyline") finishPolyline();
          }}
          onWheel={(ev) => {
            ev.preventDefault();
            const point = svgPoint(ev);
            zoomAt(ev.deltaY < 0 ? 0.8 : 1.25, point?.x, point?.y);
          }}
          onMouseMove={(ev) => {
            if (panStart && svgRef.current) {
              const rect = svgRef.current.getBoundingClientRect();
              const dx =
                ((ev.clientX - panStart.clientX) / rect.width) *
                panStart.viewBox.width;
              const dy =
                ((ev.clientY - panStart.clientY) / rect.height) *
                panStart.viewBox.height;
              setViewBox({
                ...panStart.viewBox,
                x: Math.max(
                  0,
                  Math.min(
                    ir.source.image_width - panStart.viewBox.width,
                    panStart.viewBox.x - dx,
                  ),
                ),
                y: Math.max(
                  0,
                  Math.min(
                    ir.source.image_height - panStart.viewBox.height,
                    panStart.viewBox.y - dy,
                  ),
                ),
              });
              return;
            }
            const point = svgPoint(ev);
            setCursor(point);
            if (marquee && ev.buttons & 1 && point) {
              setMarquee({ ...marquee, end: point });
            } else if (marquee && !(ev.buttons & 1)) {
              setMarquee(null);
            }
          }}
        >
          {hasSource && showSource && (
            <image
              href={sourceUrl(gen.id, 0, sourceVariant)}
              x={0}
              y={0}
              width={ir.source.image_width}
              height={ir.source.image_height}
              opacity={sourceOpacity}
              preserveAspectRatio="none"
            />
          )}
          {unresolved.map((item) => (
            <rect
              key={item.id}
              x={item.region.x0}
              y={item.region.y0}
              width={item.region.x1 - item.region.x0}
              height={item.region.y1 - item.region.y0}
              fill="#ef444422"
              stroke="#ef4444"
              strokeWidth={2}
              strokeDasharray="8 4"
              pointerEvents="none"
            />
          ))}
          {ir.entities
            .filter(
              (e) =>
                visibleLayers.has(e.line_class) &&
                !frozenLayers.has(e.line_class),
            )
            .map((e) => (
              <EntityShape
                key={e.id}
                e={e}
                selected={selection.has(e.id) || e.id === pickedSegmentId}
                flagged={flaggedIds.has(e.id)}
                arrowLen={arrowLen}
                tool={tool}
                locked={lockedLayers.has(e.line_class)}
                onClick={(id, ev) => void entityClick(id, ev)}
              />
            ))}
          {draftStart && cursor && tool === "line" && (
            <line
              x1={draftStart.x}
              y1={draftStart.y}
              x2={cursor.x}
              y2={cursor.y}
              stroke="#38bdf8"
              strokeDasharray="6 4"
              strokeWidth={1.5}
            />
          )}
          {draftStart && cursor && tool === "circle" && (
            <circle
              cx={draftStart.x}
              cy={draftStart.y}
              r={Math.hypot(cursor.x - draftStart.x, cursor.y - draftStart.y)}
              stroke="#38bdf8"
              strokeDasharray="6 4"
              strokeWidth={1.5}
              fill="none"
            />
          )}
          {draftStart && cursor && tool in DIM_TOOL_KIND && (
            <g stroke="#38bdf8" strokeDasharray="6 4" strokeWidth={1.5}>
              <line
                x1={draftStart.x}
                y1={draftStart.y}
                x2={cursor.x}
                y2={cursor.y}
              />
              {dimensionArrowPolygons(
                draftStart,
                cursor,
                DIM_TOOL_KIND[tool],
                arrowLen,
              ).map((points, i) => (
                <polygon key={i} points={points} fill="#38bdf8" stroke="none" />
              ))}
            </g>
          )}
          {draftStart && cursor && tool === "mirror" && (
            <line
              x1={draftStart.x}
              y1={draftStart.y}
              x2={cursor.x}
              y2={cursor.y}
              stroke="#a78bfa"
              strokeDasharray="3 3"
              strokeWidth={1}
            />
          )}
          {tool === "polyline" && polylinePoints.length > 0 && (
            <g
              stroke="#38bdf8"
              strokeDasharray="6 4"
              strokeWidth={1.5}
              fill="none"
            >
              <polyline
                points={polylinePoints.map((p) => `${p.x},${p.y}`).join(" ")}
              />
              {cursor && (
                <line
                  x1={polylinePoints[polylinePoints.length - 1].x}
                  y1={polylinePoints[polylinePoints.length - 1].y}
                  x2={cursor.x}
                  y2={cursor.y}
                />
              )}
              {polylinePoints.map((p, i) => (
                <circle key={i} cx={p.x} cy={p.y} r={2.5} fill="#38bdf8" />
              ))}
            </g>
          )}
          {marquee &&
            Math.hypot(
              marquee.end.x - marquee.start.x,
              marquee.end.y - marquee.start.y,
            ) > 0 && (
              <rect
                x={Math.min(marquee.start.x, marquee.end.x)}
                y={Math.min(marquee.start.y, marquee.end.y)}
                width={Math.abs(marquee.end.x - marquee.start.x)}
                height={Math.abs(marquee.end.y - marquee.start.y)}
                fill={marqueeCrossing ? "#34d39922" : "#38bdf822"}
                stroke={marqueeCrossing ? "#34d399" : "#38bdf8"}
                strokeWidth={1}
                strokeDasharray={marqueeCrossing ? "4 3" : undefined}
              />
            )}
        </svg>
      </div>

      {/* AutoCAD-style status bar + command line */}
      <StatusBar
        cursor={cursor}
        scale={ir.scale}
        osnap={osnap}
        ortho={ortho}
        onToggleOsnap={() => setOsnap((v) => !v)}
        onToggleOrtho={() => setOrtho((v) => !v)}
        selectionCount={selection.size}
        toolLabel={t(`vector.tool_${tool}`)}
        t={t}
      />
      <CommandLine
        prompt={cmdPrompt}
        hint={cmdHint}
        onSubmit={(text) => {
          if (cmdPrompt) resolvePrompt(text);
          else runCommand(text);
        }}
        onCancel={() => {
          if (cmdPrompt) resolvePrompt(null);
        }}
      />

      {err && <div className="text-xs text-red-400">{err}</div>}

      {unresolved.length > 0 && (
        <section className="rounded border border-red-500/30 bg-red-500/5 p-2 text-xs">
          <div className="mb-2 font-medium text-red-300">
            {t("vector.unresolved_title")}
          </div>
          <div className="max-h-44 space-y-1 overflow-auto">
            {unresolved.map((item, index) => (
              <div
                key={item.id}
                className="flex flex-wrap items-center gap-2 rounded bg-black/20 px-2 py-1"
              >
                <button
                  type="button"
                  onClick={() => {
                    const width = Math.max(
                      item.region.x1 - item.region.x0,
                      ir.source.image_width / 10,
                    );
                    const height =
                      width *
                      (ir.source.image_height / ir.source.image_width);
                    setViewBox({
                      x: Math.max(0, item.region.x0 - width * 0.25),
                      y: Math.max(0, item.region.y0 - height * 0.25),
                      width: Math.min(width * 1.5, ir.source.image_width),
                      height: Math.min(height * 1.5, ir.source.image_height),
                    });
                  }}
                  className="text-left text-red-200 hover:text-red-100"
                >
                  #{index + 1} · {item.ink_pixels} px · {item.reason}
                </button>
                <span className="flex-1" />
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void apply([
                      { op: "resolve_region", region_id: item.id },
                    ])
                  }
                  className="rounded bg-emerald-600/80 px-2 py-0.5 text-white disabled:opacity-50"
                >
                  {t("vector.unresolved_resolve")}
                </button>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* Bulk actions for a multi-entity selection */}
      {selectedList.length > 1 && (
        <div className="rounded border border-white/10 bg-zinc-900/60 p-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-zinc-300">
            {t("vector.status_selected", { n: selectedList.length })}
          </span>
          <label className="flex items-center gap-1 text-zinc-400">
            {t("vector.line_class")}
            <select
              disabled={busy}
              value=""
              onChange={(e) => {
                const lc = e.target.value as IrEntity["line_class"];
                if (!lc) return;
                void apply(
                  selectedList.map((entity) => ({
                    op: "update" as const,
                    entity_id: entity.id,
                    entity: {
                      ...entity,
                      line_class: lc,
                      width_class: ["axis", "dim", "hatch"].includes(lc)
                        ? ("thin" as const)
                        : entity.width_class,
                    },
                  })),
                );
              }}
              className="rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-zinc-200"
            >
              <option value="">—</option>
              {["contour", "thin", "axis", "hidden", "dim", "hatch"].map(
                (lc) => (
                  <option key={lc} value={lc}>
                    {t(`vector.line_${lc}`)}
                  </option>
                ),
              )}
            </select>
          </label>
          <span className="flex-1" />
          <button
            disabled={busy}
            onClick={() =>
              apply(
                selectedList.map((entity) => ({
                  op: "confirm" as const,
                  entity_id: entity.id,
                })),
              )
            }
            className="px-2 py-1 rounded bg-emerald-600/80 hover:bg-emerald-500 text-white disabled:opacity-50"
          >
            {t("vector.confirm")}
          </button>
          <button
            disabled={busy}
            onClick={() => {
              const ids = selectedList.map((entity) => entity.id);
              selectOnly(null);
              void apply(
                ids.map((id) => ({ op: "delete" as const, entity_id: id })),
              );
            }}
            className="px-2 py-1 rounded bg-red-600/70 hover:bg-red-500 text-white disabled:opacity-50"
          >
            {t("vector.delete")}
          </button>
        </div>
      )}

      {/* Selected entity properties */}
      {selected && (
        <div className="rounded border border-white/10 bg-zinc-900/60 p-2 space-y-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="flex flex-wrap items-center gap-1 text-zinc-300">
              {entityLabel(selected, t)}
              <span className="text-zinc-500">
                {Math.round(selected.confidence * 100)}% · {selected.origin}
              </span>
              <AssuranceBadge assurance={selected.assurance} t={t} />
            </span>
            <div className="flex gap-2">
              {selected.assurance !== "human_approved" && (
                <button
                  disabled={busy}
                  onClick={() =>
                    apply([{ op: "confirm", entity_id: selected.id }])
                  }
                  className="px-2 py-1 rounded bg-emerald-600/80 hover:bg-emerald-500 text-white disabled:opacity-50"
                >
                  {t("vector.confirm")}
                </button>
              )}
              <button
                disabled={busy}
                onClick={async () => {
                  const typed = await requestValue(
                    t("vector.copy_prompt"),
                    "20,0",
                  );
                  if (!typed) return;
                  const [dxs, dys] = typed.split(",").map((s) => s.trim());
                  const dx = Number(dxs?.replace(",", "."));
                  const dy = Number(dys?.replace(",", ".") || "0");
                  if (Number.isNaN(dx) || Number.isNaN(dy)) return;
                  void apply([{ op: "copy", entity_id: selected.id, dx, dy }]);
                }}
                className="px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-zinc-200 disabled:opacity-50"
              >
                {t("vector.copy")}
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  setMirrorTargetId(selected.id);
                  setTool("mirror");
                  setDraftStart(null);
                }}
                className="px-2 py-1 rounded bg-white/10 hover:bg-white/20 text-zinc-200 disabled:opacity-50"
              >
                {t("vector.mirror")}
              </button>
              <button
                disabled={busy}
                onClick={() => {
                  selectOnly(null);
                  void apply([{ op: "delete", entity_id: selected.id }]);
                }}
                className="px-2 py-1 rounded bg-red-600/70 hover:bg-red-500 text-white disabled:opacity-50"
              >
                {t("vector.delete")}
              </button>
            </div>
          </div>
          {tool === "mirror" && mirrorTargetId === selected.id && (
            <div className="text-[11px] text-sky-300">
              {t("vector.mirror_pick_line")}
            </div>
          )}
          {(selected.alternatives?.length ?? 0) > 0 && (
            <div className="rounded bg-white/5 p-1.5 space-y-1">
              <div className="text-[10px] text-zinc-500">
                {t("vector.alternatives_title")}
              </div>
              {selected.alternatives!.map((alt, i) => (
                <button
                  key={i}
                  disabled={busy || !alt.value}
                  onClick={() => {
                    if (!alt.value) return;
                    void apply([
                      {
                        op: "update",
                        entity_id: selected.id,
                        entity: { ...selected, text: alt.value },
                      },
                    ]);
                  }}
                  className="flex w-full items-center justify-between rounded px-1.5 py-1 text-left text-[11px] text-zinc-300 hover:bg-white/10 disabled:cursor-default disabled:opacity-60"
                >
                  <span>{alt.value ?? t("vector.alternative_geometry")}</span>
                  <span className="text-zinc-500">
                    {Math.round(alt.p * 100)}%
                  </span>
                </button>
              ))}
            </div>
          )}
          <div className="flex flex-wrap gap-2 items-center">
            <label className="flex items-center gap-1 text-zinc-400">
              {t("vector.line_class")}
              <select
                value={selected.line_class}
                disabled={busy}
                onChange={(e) =>
                  apply([
                    {
                      op: "update",
                      entity_id: selected.id,
                      entity: {
                        ...selected,
                        line_class: e.target.value as IrEntity["line_class"],
                        width_class: ["axis", "dim", "hatch"].includes(
                          e.target.value,
                        )
                          ? "thin"
                          : selected.width_class,
                      },
                    },
                  ])
                }
                className="rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-zinc-200"
              >
                {["contour", "thin", "axis", "hidden", "dim", "hatch"].map(
                  (lc) => (
                    <option key={lc} value={lc}>
                      {t(`vector.line_${lc}`)}
                    </option>
                  ),
                )}
              </select>
            </label>
            <button
              type="button"
              disabled={busy}
              title={t("vector.construction_hint")}
              onClick={() =>
                apply([{ op: "set_construction", entity_id: selected.id }])
              }
              className={`rounded px-2 py-0.5 text-[11px] disabled:opacity-40 ${
                selected.construction
                  ? "bg-cyan-500/20 text-cyan-300"
                  : "bg-white/5 text-zinc-300 hover:bg-white/10"
              }`}
            >
              {t("vector.construction")}
            </button>
            {selected.type === "text" && (
              <label className="flex items-center gap-1 text-zinc-400">
                {t("vector.text_label")}
                <input
                  defaultValue={selected.text ?? ""}
                  disabled={busy}
                  onBlur={(e) => {
                    if (e.target.value !== selected.text) {
                      void apply([
                        {
                          op: "update",
                          entity_id: selected.id,
                          entity: { ...selected, text: e.target.value },
                        },
                      ]);
                    }
                  }}
                  className="rounded bg-zinc-900 border border-white/10 px-1 py-0.5 text-zinc-200"
                />
              </label>
            )}
          </div>
        </div>
      )}

      <LayersPanel
        counts={layerCounts}
        visible={visibleLayers}
        locked={lockedLayers}
        frozen={frozenLayers}
        onToggleVisible={toggleInSet(setVisibleLayers)}
        onToggleLocked={toggleInSet(setLockedLayers)}
        onToggleFrozen={toggleInSet(setFrozenLayers)}
        t={t}
      />

      <ReviewPanel
        ir={ir}
        busy={busy}
        onApply={(ops) => void apply(ops)}
        onFocus={focusEntity}
        t={t}
      />

      <TitleBlockPanel
        ir={ir}
        busy={busy}
        onApply={(ops) => void apply(ops)}
        t={t}
      />

      <AnnotationsPanel
        busy={busy}
        onAdd={(payload) => {
          // Drop the annotation at the current view centre; the user then
          // drags it into place (arrow keys / select-move).
          const cx = activeViewBox.x + activeViewBox.width / 2;
          const cy = activeViewBox.y + activeViewBox.height / 2;
          const h = ir.scale ? 3.5 / ir.scale : 14;
          void apply([
            {
              op: "add",
              entity: {
                type: "annotation",
                position: { x: cx, y: cy },
                height: h,
                line_class: "dim",
                width_class: "thin",
                ...payload,
              },
            },
          ]);
        }}
        t={t}
      />

      <ConstraintsPanel
        ir={ir}
        genId={gen.id}
        selected={selectedList}
        busy={busy}
        onApply={(ops) => void apply(ops)}
        onSolve={() => void onSolve()}
        onFocus={focusEntity}
        onError={setErr}
        t={t}
      />

      <ValidationPanel
        ir={ir}
        busy={busy}
        fullCheckCurrent={fullCheckCurrent}
        fullCheckRunning={fullCheckRunning}
        fullCheckElapsed={fullCheckElapsed}
        onRunFullCheck={() => void onRunFullCheck()}
        onApply={(ops) => void apply(ops)}
        onFocus={focusEntity}
        onError={setErr}
        t={t}
      />

      {!gen.accepted && criticalUnresolved.length > 0 && (
        <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
          {t("vector.accept_blocked_critical", {
            n: criticalUnresolved.length,
          })}
        </div>
      )}
      {!gen.accepted &&
        pending.length > 0 &&
        criticalUnresolved.length === 0 && (
          <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
            {t("vector.accept_blocked_review", { n: pending.length })}
          </div>
        )}
      {!gen.accepted &&
        blocking.length === 0 &&
        pending.length === 0 &&
        !fullCheckCurrent && (
          <div className="rounded border border-amber-500/20 bg-amber-500/5 p-2 text-[11px] text-amber-300">
            {t("vector.accept_blocked_full_check")}
          </div>
        )}

      <Cad3dPanel
        gen={gen}
        revision={revision}
        onChanged={onChanged}
        onError={setErr}
        t={t}
      />

      {/* Downloads + accept */}
      <div className="flex flex-wrap items-center gap-2">
        <a
          href={artifactUrl(gen.id, "dxf")}
          download={`studio-${gen.id}.dxf`}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          {gen.accepted ? "DXF" : t("vector.draft_dxf")}
        </a>
        <a
          href={artifactUrl(gen.id, "dwg")}
          download={`studio-${gen.id}.dwg`}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          {gen.accepted ? "DWG" : t("vector.draft_dwg")}
        </a>
        <a
          href={artifactUrl(gen.id, "svg")}
          download={`studio-${gen.id}.svg`}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          SVG
        </a>
        <a
          href={artifactUrl(gen.id, "pdf")}
          target="_blank"
          rel="noopener noreferrer"
          title={t("vector.print_pdf_hint")}
          className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
        >
          {t("vector.print_pdf")}
        </a>
        {!gen.accepted && certification?.status !== "drafter_approved" && (
          <button
            disabled={
              busy ||
              blocking.length > 0 ||
              pending.length > 0 ||
              unresolved.length > 0 ||
              !fullCheckCurrent
            }
            title={
              blocking.length > 0
                ? t("vector.accept_blocked")
                : pending.length > 0
                  ? t("vector.accept_blocked_review", {
                      n: pending.length,
                    })
                  : unresolved.length > 0
                    ? t("vector.accept_blocked_unresolved", {
                        n: unresolved.length,
                      })
                  : !fullCheckCurrent
                    ? t("vector.accept_blocked_full_check")
                    : undefined
            }
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                const certificate = await approveCadAsDrafter(gen.id);
                setCertification(certificate);
              } catch (e) {
                setErr(String((e as Error).message || e));
              } finally {
                setBusy(false);
              }
            }}
            className="ml-auto px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50"
          >
            {t("vector.certify_drafter")}
          </button>
        )}
        {!gen.accepted && certification?.status === "drafter_approved" && (
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setErr(null);
              try {
                const certificate = await approveCadAsNormcontroller(gen.id);
                setCertification(certificate);
                onChanged();
              } catch (e) {
                setErr(String((e as Error).message || e));
              } finally {
                setBusy(false);
              }
            }}
            className="ml-auto px-3 py-1.5 rounded bg-emerald-600 hover:bg-emerald-500 text-white text-sm disabled:opacity-50"
          >
            {t("vector.certify_normcontrol")}
          </button>
        )}
        {gen.accepted && (
          <a
            href={releasePackageUrl(gen.id)}
            download={`release-${gen.id}.zip`}
            className="ml-auto rounded bg-emerald-600/90 px-3 py-1.5 text-sm text-white hover:bg-emerald-500"
            title={t("vector.release_hint")}
          >
            {t("vector.release_download")}
          </a>
        )}
      </div>

      {gen.accepted && release && (
        <div className="rounded border border-emerald-500/20 bg-emerald-500/5 p-2 text-[11px] text-zinc-300 space-y-0.5">
          <div className="flex items-center gap-2">
            <span
              className={
                release.fully_reproducible
                  ? "text-emerald-300"
                  : "text-amber-300"
              }
            >
              {release.fully_reproducible
                ? t("vector.release_reproducible")
                : t("vector.release_not_reproducible")}
            </span>
            <span className="text-zinc-500">· DXF {release.dxf_version}</span>
          </div>
          <div className="font-mono text-zinc-500">
            manifest {(release.manifest_sha256 ?? "").slice(0, 16)}…
          </div>
          {release.approval.accepted_by && (
            <div className="text-zinc-500">
              {t("vector.release_approved_by", {
                who: release.approval.accepted_by,
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
