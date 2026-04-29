"use client";

import { useEffect, useState } from "react";

const commands = [
  "Создать ManufacturingCase",
  "Загрузить документ",
  "Запустить processing pipeline",
  "Извлечь счет",
  "Проверить чертеж",
  "Черновик письма заказчику",
];

export function CommandPalette() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  if (!open) {
    return null;
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 20,
        display: "grid",
        placeItems: "start center",
        paddingTop: "12vh",
        background: "rgba(31, 43, 37, 0.26)",
        backdropFilter: "blur(8px)",
      }}
    >
      <div className="glass-panel" style={{ width: "min(680px, calc(100vw - 24px))", padding: 18 }}>
        <p className="eyebrow">Ctrl K</p>
        <input
          autoFocus
          placeholder="Что сделать дальше?"
          style={{
            width: "100%",
            border: "1px solid var(--line)",
            borderRadius: 18,
            padding: "18px 20px",
            background: "rgba(255, 252, 242, 0.86)",
            color: "var(--bg-ink)",
            fontSize: 18,
            outline: "none",
          }}
        />
        <div style={{ display: "grid", gap: 8, marginTop: 14 }}>
          {commands.map((command, index) => (
            <button
              className="ghost-button"
              key={command}
              style={{ display: "flex", justifyContent: "space-between", borderRadius: 16 }}
              type="button"
            >
              <span>{command}</span>
              <span className="small-muted">0{index + 1}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
