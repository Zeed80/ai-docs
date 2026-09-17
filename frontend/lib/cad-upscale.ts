import { getApiBaseUrl } from "@/lib/api-base";
import { apiFetch, mutFetch } from "@/lib/auth";

/**
 * Улучшение грубого листа перед оцифровкой (SeedVR2 в локальном ComfyUI).
 *
 * Хранится в ai_config полями `cad_upscale_*`; сервер отдаёт в `cad_upscale`
 * то, что реально применится: галочка прогона → настройки → окружение.
 */
export type UpscaleLimitKey =
  | "min_line_px"
  | "max_factor"
  | "timeout_s"
  | "min_agreement";

export interface UpscaleSettings {
  enabled: boolean;
  enabled_source: "run" | "settings" | "environment";
  env_enabled: boolean;
  min_line_px: number;
  max_factor: number;
  timeout_s: number;
  min_agreement: number;
  limits: Record<UpscaleLimitKey, [number, number]>;
}

export type UpscalePatch = Partial<{
  cad_upscale_enabled: boolean | null;
  cad_upscale_min_line_px: number;
  cad_upscale_max_factor: number;
  cad_upscale_timeout_s: number;
  cad_upscale_min_agreement: number;
}>;

const CONFIG = () => `${getApiBaseUrl()}/api/ai/config`;

async function settingsFrom(res: Response): Promise<UpscaleSettings> {
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  const data = (await res.json()) as { cad_upscale?: UpscaleSettings };
  if (!data.cad_upscale) throw new Error("сервер не вернул cad_upscale");
  return data.cad_upscale;
}

export async function fetchUpscaleSettings(): Promise<UpscaleSettings> {
  return settingsFrom(await apiFetch(CONFIG()));
}

export async function saveUpscaleSettings(
  patch: UpscalePatch,
): Promise<UpscaleSettings> {
  return settingsFrom(
    await mutFetch(CONFIG(), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}
