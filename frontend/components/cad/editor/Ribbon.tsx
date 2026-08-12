"use client";

/** Тот же принцип, что у современных CAD-редакторов (SolidWorks/Fusion/
 * FreeCAD): группы инструментов в закреплённых вкладках вместо плоского
 * тулбара. Эскиз (Ф4) и Фичи (Ф2/Ф3) — рабочие; Тело (булевы/массивы) —
 * всё ещё заглушка, будущая фаза. "Проверка" владеет действиями
 * пересборки/приёмки/экспорта. */

export type RibbonTabId = "sketch" | "features" | "body" | "inspect";

const TABS: { id: RibbonTabId; label: string }[] = [
  { id: "sketch", label: "Эскиз" },
  { id: "features", label: "Фичи" },
  { id: "body", label: "Тело" },
  { id: "inspect", label: "Проверка" },
];

export function RibbonButton({
  icon,
  label,
  onClick,
  disabled,
  title,
}: {
  icon: string;
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
      <span className="text-base leading-none">{icon}</span>
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
  icon: string;
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
      <div className="flex min-h-[56px] items-center gap-1 overflow-x-auto border-t border-white/5 bg-zinc-950 px-3 py-1.5">
        {children}
      </div>
    </div>
  );
}
