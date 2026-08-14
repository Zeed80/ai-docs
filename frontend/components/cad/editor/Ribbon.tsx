"use client";

/** Тот же принцип, что у современных CAD-редакторов (SolidWorks/Fusion/
 * FreeCAD): группы инструментов в закреплённых вкладках вместо плоского
 * тулбара. Эскиз (Ф4) и Фичи (Ф2/Ф3) — рабочие; Тело (булевы/массивы) —
 * частично заглушка (см. Ф8 — Массив уже рабочий, Объединение/Пересечение
 * ждут отдельного плана). "Проверка" владеет действиями пересборки/
 * приёмки/экспорта. Ф10: RibbonGroup даёт кластерам кнопок подпись —
 * то же визуальное деление, что у настоящих ленточных интерфейсов. */

export type RibbonTabId = "sketch" | "features" | "body" | "spec" | "inspect";

const TABS: { id: RibbonTabId; label: string }[] = [
  { id: "sketch", label: "Эскиз" },
  { id: "features", label: "Фичи" },
  { id: "body", label: "Тело" },
  { id: "spec", label: "Спецификация" },
  { id: "inspect", label: "Проверка" },
];

export function RibbonButton({
  icon,
  label,
  onClick,
  disabled,
  title,
}: {
  // Ф10: a lucide-react icon element or (legacy) a plain glyph string —
  // either renders fine, no caller was forced to migrate at once.
  icon: React.ReactNode;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || !onClick}
      title={title}
      className="flex min-w-[64px] flex-col items-center gap-1 rounded px-2.5 py-1.5 text-[11px] text-zinc-300 hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent"
    >
      <span className="flex h-4 items-center justify-center leading-none">
        {icon}
      </span>
      <span className="whitespace-nowrap">{label}</span>
    </button>
  );
}

/** A tool that isn't wired up yet in the current phase — visible (so the
 * eventual shape of the ribbon is honest about what's coming) but inert,
 * with a tooltip naming which phase brings it, never a silently-broken
 * click. */
export function RibbonPlaceholder({
  icon,
  label,
  comingIn,
}: {
  icon: React.ReactNode;
  label: string;
  comingIn: string;
}) {
  return (
    <RibbonButton
      icon={icon}
      label={label}
      title={`Появится в ${comingIn}`}
      disabled
    />
  );
}

export function RibbonDivider() {
  return <span className="mx-1 h-8 w-px shrink-0 bg-white/10" />;
}

/** Ф10: a labelled cluster of buttons — Профиль/Вырезы/Модификация etc,
 * the same visual grouping a real ribbon UI uses so a person scanning the
 * strip doesn't have to guess which buttons belong together. */
export function RibbonGroup({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-0.5">
      <div className="flex items-center gap-1">{children}</div>
      <span className="text-[9px] uppercase tracking-wide text-zinc-600">
        {label}
      </span>
    </div>
  );
}

export default function Ribbon({
  active,
  onChange,
  children,
}: {
  active: RibbonTabId;
  onChange: (tab: RibbonTabId) => void;
  children: React.ReactNode;
}) {
  return (
    <div className="shrink-0 border-b border-white/10 bg-zinc-900">
      <div className="flex items-center gap-1 px-2 pt-1.5">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            className={`rounded-t px-3 py-1.5 text-xs font-medium transition-colors ${
              active === tab.id
                ? "bg-zinc-950 text-zinc-100"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      <div className="flex min-h-[56px] items-center gap-2 overflow-x-auto border-t border-white/5 bg-zinc-950 px-3 py-1.5">
        {children}
      </div>
    </div>
  );
}
