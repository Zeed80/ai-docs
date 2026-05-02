"use client";

interface CanvasImageProps {
  url: string;
  alt?: string;
  title?: string;
}

export function CanvasImage({ url, alt, title }: CanvasImageProps) {
  const handleDownload = () => {
    const a = document.createElement("a");
    a.href = url;
    a.download = title || alt || "image";
    a.click();
  };

  return (
    <div className="space-y-2">
      <div className="border border-slate-700 rounded overflow-hidden bg-slate-900">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={url}
          alt={alt || title || ""}
          className="max-w-full h-auto block"
        />
      </div>
      <button
        onClick={handleDownload}
        className="text-xs text-slate-400 hover:text-slate-200 underline"
      >
        Скачать изображение
      </button>
    </div>
  );
}
