"use client";

import { useRef, useState } from "react";

import { isNative, pickImage } from "@/lib/native-bridge";
import {
  GenerateInput,
  Operation,
  generate,
  promptHelp,
  uploadSource,
} from "@/lib/studio-api";

import MaskCanvas, { MaskCanvasHandle } from "./MaskCanvas";

const OPERATIONS: { key: Operation; label: string; needsSource: boolean }[] = [
  { key: "edit", label: "Редактировать", needsSource: true },
  { key: "generate", label: "Создать", needsSource: false },
  { key: "inpaint", label: "Область (маска)", needsSource: true },
  { key: "cleanup", label: "Очистить/чертёж", needsSource: true },
];

interface Props {
  onSubmitted: () => void;
}

export default function StudioComposer({ onSubmitted }: Props) {
  const [operation, setOperation] = useState<Operation>("edit");
  const [prompt, setPrompt] = useState("");
  const [negative, setNegative] = useState("");
  const [seed, setSeed] = useState<string>("0");
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [sourcePreview, setSourcePreview] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [helping, setHelping] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const maskRef = useRef<MaskCanvasHandle>(null);

  const op = OPERATIONS.find((o) => o.key === operation)!;

  function setSource(file: File | null) {
    setSourceFile(file);
    if (sourcePreview) URL.revokeObjectURL(sourcePreview);
    setSourcePreview(file ? URL.createObjectURL(file) : null);
  }

  async function pickFromGallery() {
    const files = await pickImage("PHOTOS");
    if (files.length) setSource(files[0]);
  }
  async function pickFromCamera() {
    const files = await pickImage("CAMERA");
    if (files.length) setSource(files[0]);
  }

  async function helpWithPrompt() {
    if (!prompt.trim()) return;
    setHelping(true);
    setErr(null);
    try {
      const res = await promptHelp(prompt, operation);
      if (res.prompt) setPrompt(res.prompt);
      if (res.negative_prompt) setNegative(res.negative_prompt);
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setHelping(false);
    }
  }

  async function submit() {
    setErr(null);
    if (op.needsSource && !sourceFile) {
      setErr("Для этой операции приложите изображение.");
      return;
    }
    if (operation === "generate" && !prompt.trim()) {
      setErr("Опишите, что нужно сгенерировать.");
      return;
    }
    setBusy(true);
    try {
      const input: GenerateInput = {
        operation,
        prompt: prompt || undefined,
        negative_prompt: negative || undefined,
        params: { seed: Number(seed) || 0 },
        source_image_paths: [],
      };
      if (sourceFile) {
        input.source_image_paths = [await uploadSource(sourceFile, "source")];
      }
      if (operation === "inpaint" && maskRef.current) {
        const blob = await maskRef.current.getMaskBlob();
        if (!blob) {
          setErr("Закрасьте область для правки на изображении.");
          setBusy(false);
          return;
        }
        const maskFile = new File([blob], "mask.png", { type: "image/png" });
        input.mask_path = await uploadSource(maskFile, "mask");
      }
      await generate(input);
      setPrompt("");
      setNegative("");
      onSubmitted();
    } catch (e) {
      setErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      {/* Operation tabs */}
      <div className="flex flex-wrap gap-1">
        {OPERATIONS.map((o) => (
          <button
            key={o.key}
            onClick={() => setOperation(o.key)}
            className={`px-3 py-1.5 rounded text-sm ${
              operation === o.key
                ? "bg-sky-600 text-white"
                : "bg-white/5 text-zinc-300 hover:bg-white/10"
            }`}
          >
            {o.label}
          </button>
        ))}
      </div>

      {/* Source image */}
      {op.needsSource && (
        <div>
          <div className="flex flex-wrap gap-2 mb-2">
            <label className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm cursor-pointer">
              Выбрать файл
              <input
                type="file"
                accept="image/*"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) setSource(f);
                }}
              />
            </label>
            {isNative() && (
              <>
                <button
                  onClick={pickFromCamera}
                  className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
                >
                  Снять фото
                </button>
                <button
                  onClick={pickFromGallery}
                  className="px-3 py-1.5 rounded bg-white/10 hover:bg-white/20 text-sm"
                >
                  Из галереи
                </button>
              </>
            )}
            {sourceFile && (
              <button
                onClick={() => setSource(null)}
                className="px-3 py-1.5 rounded bg-white/5 hover:bg-white/10 text-sm text-zinc-400"
              >
                Убрать
              </button>
            )}
          </div>
          {sourcePreview &&
            (operation === "inpaint" ? (
              <MaskCanvas ref={maskRef} imageUrl={sourcePreview} />
            ) : (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={sourcePreview}
                alt="источник"
                className="max-h-64 rounded border border-white/10"
              />
            ))}
        </div>
      )}

      {/* Prompt (not needed for pure cleanup) */}
      {operation !== "cleanup" && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <label className="text-xs text-zinc-500">
              {operation === "generate" ? "Что сгенерировать" : "Что изменить"}
            </label>
            <button
              onClick={helpWithPrompt}
              disabled={helping || !prompt.trim()}
              className="text-xs text-sky-400 hover:text-sky-300 disabled:opacity-40"
            >
              {helping ? "Света думает…" : "✨ Помочь с промптом"}
            </button>
          </div>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            placeholder={
              operation === "generate"
                ? "эскиз кондуктора для сверления фланца, технический линейный чертёж, вид сверху"
                : "убери фаску, добавь размер 40h7, перерисуй вид сверху"
            }
            className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
          />
        </div>
      )}

      <details className="text-sm">
        <summary className="text-xs text-zinc-500 cursor-pointer">
          Дополнительно
        </summary>
        <div className="mt-2 space-y-2">
          {operation !== "cleanup" && (
            <div>
              <label className="text-xs text-zinc-500">Negative prompt</label>
              <input
                value={negative}
                onChange={(e) => setNegative(e.target.value)}
                className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
                placeholder="размытие, лишние объекты, цветной фон"
              />
            </div>
          )}
          <div>
            <label className="text-xs text-zinc-500">
              Seed (0 = случайный)
            </label>
            <input
              type="number"
              value={seed}
              onChange={(e) => setSeed(e.target.value)}
              className="w-full rounded bg-zinc-900 border border-white/10 p-2 text-sm text-zinc-200"
            />
          </div>
        </div>
      </details>

      {err && <div className="text-xs text-red-400">{err}</div>}

      <button
        onClick={submit}
        disabled={busy}
        className="w-full px-4 py-2.5 rounded bg-sky-600 hover:bg-sky-500 text-white font-medium disabled:opacity-50"
      >
        {busy ? "Отправка…" : "Сгенерировать"}
      </button>
    </div>
  );
}
