"use client";

import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { CommandPalette } from "@/components/ui/command-palette";

interface KeyboardState {
  showHelp: boolean;
  setShowHelp: (v: boolean) => void;
  showPalette: boolean;
  setShowPalette: (v: boolean) => void;
}

const KeyboardContext = createContext<KeyboardState>({
  showHelp: false,
  setShowHelp: () => {},
  showPalette: false,
  setShowPalette: () => {},
});

export function KeyboardProvider({ children }: { children: ReactNode }) {
  const [showHelp, setShowHelp] = useState(false);
  const [showPalette, setShowPalette] = useState(false);

  useEffect(() => {
    function handleGlobal(e: KeyboardEvent) {
      // Ctrl+K always opens palette, even from inputs
      if (e.key === "k" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        setShowPalette((v) => !v);
        return;
      }

      // Other shortcuts only outside inputs
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      )
        return;

      if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        setShowHelp((v) => !v);
      }
    }

    window.addEventListener("keydown", handleGlobal);
    return () => window.removeEventListener("keydown", handleGlobal);
  }, []);

  return (
    <KeyboardContext.Provider
      value={{ showHelp, setShowHelp, showPalette, setShowPalette }}
    >
      {children}
      {showHelp && <KeyboardHelpModal onClose={() => setShowHelp(false)} />}
      <CommandPalette
        open={showPalette}
        onClose={() => setShowPalette(false)}
      />
    </KeyboardContext.Provider>
  );
}

export function useKeyboard() {
  return useContext(KeyboardContext);
}

function KeyboardHelpModal({ onClose }: { onClose: () => void }) {
  const shortcuts = [
    { key: "j", desc: "Следующий" },
    { key: "k", desc: "Предыдущий" },
    { key: "Enter", desc: "Открыть" },
    { key: "a", desc: "Утвердить" },
    { key: "r", desc: "Отклонить" },
    { key: "s", desc: "Отложить" },
    { key: "c", desc: "Комментарий" },
    { key: "e", desc: "Редактировать" },
    { key: "n", desc: "Пропустить" },
    { key: "x", desc: "Выбрать" },
    { key: "Esc", desc: "Назад / Закрыть" },
    { key: "Ctrl+K", desc: "Командная палитра" },
    { key: "?", desc: "Эта справка" },
  ];

  return (
    <div
      className="fixed inset-0 bg-black/50 flex items-center justify-center z-50"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-lg shadow-xl p-6 w-80"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-bold mb-4">Горячие клавиши</h2>
        <dl className="space-y-2">
          {shortcuts.map((s) => (
            <div key={s.key} className="flex justify-between text-sm">
              <kbd className="px-1.5 py-0.5 bg-slate-100 rounded border text-xs font-mono">
                {s.key}
              </kbd>
              <span className="text-slate-600">{s.desc}</span>
            </div>
          ))}
        </dl>
        <button
          onClick={onClose}
          className="mt-4 w-full text-center text-sm text-slate-400 hover:text-slate-600"
        >
          ? — закрыть
        </button>
      </div>
    </div>
  );
}
