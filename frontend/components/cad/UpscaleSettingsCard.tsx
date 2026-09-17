"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";

import {
  fetchUpscaleSettings,
  saveUpscaleSettings,
  UpscaleLimitKey,
  UpscalePatch,
  UpscaleSettings,
} from "@/lib/cad-upscale";

type Draft = Record<UpscaleLimitKey, string>;

const FIELDS: { key: UpscaleLimitKey; step: number }[] = [
  { key: "min_line_px", step: 0.1 },
  { key: "max_factor", step: 1 },
  { key: "min_agreement", step: 0.01 },
  { key: "timeout_s", step: 30 },
];

function draftOf(settings: UpscaleSettings): Draft {
  return {
    min_line_px: String(settings.min_line_px),
    max_factor: String(settings.max_factor),
    min_agreement: String(settings.min_agreement),
    timeout_s: String(settings.timeout_s),
  };
}

/**
 * Настройки улучшения грубого листа перед оцифровкой: включено ли по
 * умолчанию, порог толщины линии, предел увеличения, порог согласия
 * предохранителя и время ожидания ComfyUI. Галочка на странице оцифровки
 * решает для одного прогона и сильнее этих настроек.
 */
export default function UpscaleSettingsCard() {
  const t = useTranslations("cad.upscale");
  const [settings, setSettings] = useState<UpscaleSettings | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    fetchUpscaleSettings()
      .then((value) => {
        setSettings(value);
        setDraft(draftOf(value));
      })
      .catch((e) => setError(String((e as Error).message || e)));
  }, []);

  async function save(patch: UpscalePatch) {
    setSaving(true);
    try {
      const value = await saveUpscaleSettings(patch);
      setSettings(value);
      setDraft(draftOf(value));
      setError(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      // Молчаливый сбой выглядел бы как успех: поле в новом положении,
      // а на сервер ничего не легло.
      setError(String((e as Error).message || e));
    }
    setSaving(false);
  }

  const invalid: UpscaleLimitKey[] =
    settings && draft
      ? FIELDS.map(({ key }) => key).filter((key) => {
          const value = Number(draft[key]);
          const [low, high] = settings.limits[key];
          return (
            draft[key].trim() === "" ||
            !Number.isFinite(value) ||
            value < low ||
            value > high ||
            (key === "max_factor" && !Number.isInteger(value))
          );
        })
      : [];

  return (
    <section
      className="rounded-lg border border-slate-700 bg-slate-800/40 p-4"
      aria-labelledby="cad-upscale-title"
    >
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h3
            id="cad-upscale-title"
            className="text-sm font-semibold text-slate-100"
          >
            {t("title")}
          </h3>
          <p className="mt-1 text-xs text-slate-400">{t("subtitle")}</p>
        </div>
        {saved && (
          <span className="pt-1 text-xs text-emerald-400">{t("saved")}</span>
        )}
      </div>
      {error && (
        <p
          role="alert"
          className="mb-3 rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300"
        >
          {t("error", { error })}
        </p>
      )}
      {settings && draft && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex min-w-0 items-center gap-3">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={settings.enabled}
                disabled={saving}
                onChange={(e) =>
                  save({ cad_upscale_enabled: e.target.checked })
                }
              />
              <span className="text-sm text-slate-200">{t("enabled")}</span>
            </label>
            <span className="text-xs text-slate-400">
              {settings.enabled_source === "settings"
                ? t("source_settings")
                : t("source_environment", {
                    value: settings.env_enabled ? t("on") : t("off"),
                  })}
            </span>
            {settings.enabled_source === "settings" && (
              <button
                type="button"
                disabled={saving}
                onClick={() => save({ cad_upscale_enabled: null })}
                className="text-xs text-blue-300 underline"
              >
                {t("reset_environment")}
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {FIELDS.map(({ key, step }) => {
              const [low, high] = settings.limits[key];
              return (
                <label key={key} className="block min-w-0">
                  <span className="mb-1 block text-sm text-slate-200">
                    {t(`${key}_label`)}
                  </span>
                  <input
                    type="number"
                    inputMode="decimal"
                    min={low}
                    max={high}
                    step={step}
                    value={draft[key]}
                    disabled={saving}
                    aria-invalid={invalid.includes(key)}
                    onChange={(e) =>
                      setDraft({ ...draft, [key]: e.target.value })
                    }
                    className="w-full rounded border border-slate-600 bg-slate-950 px-2 py-1.5 text-sm text-slate-100 aria-[invalid=true]:border-red-500"
                  />
                  <span className="mt-1 block text-xs text-slate-400">
                    {t(`${key}_hint`, { low, high })}
                  </span>
                </label>
              );
            })}
          </div>
          <button
            type="button"
            disabled={saving || invalid.length > 0}
            onClick={() =>
              save({
                cad_upscale_min_line_px: Number(draft.min_line_px),
                cad_upscale_max_factor: Number(draft.max_factor),
                cad_upscale_min_agreement: Number(draft.min_agreement),
                cad_upscale_timeout_s: Number(draft.timeout_s),
              })
            }
            className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {t("save")}
          </button>
        </div>
      )}
    </section>
  );
}
