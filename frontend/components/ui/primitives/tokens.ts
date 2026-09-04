/**
 * Строковые токены оформления для раздела «Модели».
 *
 * Перенесены из app/settings/models/page.tsx, где лежали локальными
 * константами и потому дублировались в app/settings/page.tsx похожими, но не
 * совпадающими значениями. Одна копия на оба экрана.
 */

export const card = "border border-slate-700 rounded-lg overflow-hidden";

export const cardHeader =
  "px-4 py-2 bg-slate-800 border-b border-slate-700 flex items-center justify-between";

export const input =
  "w-full rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm " +
  "text-slate-200 placeholder-slate-500 focus:outline-none focus:ring-2 " +
  "focus:ring-blue-500 disabled:opacity-50";

/**
 * База без ширины: `w-full` из общего класса нельзя предсказуемо перебить
 * `w-32`/`flex-1` — Tailwind не гарантирует порядок, и селект то растягивался,
 * то нет в зависимости от порядка классов в конкретном месте.
 */
export const selectBase =
  "rounded-md border border-slate-600 bg-slate-900 px-3 py-2 text-sm " +
  "text-slate-200 focus:outline-none focus:ring-2 focus:ring-blue-500";

export const select = `w-full ${selectBase}`;

export const btn = "px-3 py-1.5 rounded text-sm font-medium transition-colors";

export const focusRing =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 " +
  "focus-visible:ring-offset-1 focus-visible:ring-offset-slate-900";
