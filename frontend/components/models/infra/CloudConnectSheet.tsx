"use client";

import { useState } from "react";
import { Button } from "@/components/ui/primitives/Button";
import { Field } from "@/components/ui/primitives/Field";
import { Sheet } from "@/components/ui/primitives/Sheet";
import { StatusDot } from "@/components/ui/primitives/StatusDot";
import { input } from "@/components/ui/primitives/tokens";
import {
  ApiError,
  refreshProviderModels,
  testProvider,
  updateProvider,
} from "@/lib/models/api";
import { providerLabel } from "@/lib/models/labels";

type StepState = "idle" | "running" | "ok" | "error";

interface StepResult {
  state: StepState;
  message?: string;
}

/**
 * Подключение облачного провайдера одним потоком.
 *
 * Ключ вводился на вкладке «Провайдеры», а модель выбиралась на вкладке
 * «Назначение» — «завести облако» распадалось на переходы между экранами, и
 * на каждом шаге было неясно, что делать дальше. Здесь три шага подряд, не
 * уводя со страницы: ключ → проверка → загрузка моделей.
 *
 * Шаги намеренно раздельные, а не один запрос: проверка ключа и загрузка
 * каталога ломаются по-разному (401 против 502) и чинятся разными
 * действиями — человек должен видеть, на чём именно остановилось.
 */
export function CloudConnectSheet({
  open,
  onClose,
  instanceId,
  kind,
  hasKey,
  onConnected,
}: {
  open: boolean;
  onClose: () => void;
  instanceId: string;
  kind: string;
  /** У провайдера уже сохранён ключ — поле можно оставить пустым. */
  hasKey: boolean;
  /** Модели подтянуты: список выбора нужно перечитать. */
  onConnected: () => void;
}) {
  const [apiKey, setApiKey] = useState("");
  const [saveStep, setSaveStep] = useState<StepResult>({ state: "idle" });
  const [testStep, setTestStep] = useState<StepResult>({ state: "idle" });
  const [loadStep, setLoadStep] = useState<StepResult>({ state: "idle" });

  const describe = (e: unknown) =>
    e instanceof ApiError ? `${e.status}: ${e.detail}` : String(e);

  const runSaveAndTest = async () => {
    setTestStep({ state: "idle" });
    setLoadStep({ state: "idle" });

    if (apiKey.trim()) {
      setSaveStep({ state: "running" });
      try {
        await updateProvider(instanceId, { api_key: apiKey.trim() });
        setSaveStep({ state: "ok", message: "ключ сохранён" });
      } catch (e) {
        setSaveStep({ state: "error", message: describe(e) });
        return;
      }
    } else if (!hasKey) {
      setSaveStep({ state: "error", message: "введите ключ" });
      return;
    }

    setTestStep({ state: "running" });
    try {
      const res = await testProvider(instanceId);
      if (res.ok) {
        setTestStep({
          state: "ok",
          message:
            res.model_count != null
              ? `провайдер отвечает, моделей: ${res.model_count}`
              : "провайдер отвечает",
        });
      } else {
        setTestStep({
          state: "error",
          message: res.error ?? "провайдер не ответил",
        });
      }
    } catch (e) {
      setTestStep({ state: "error", message: describe(e) });
    }
  };

  const runLoadModels = async () => {
    setLoadStep({ state: "running" });
    try {
      const res = await refreshProviderModels(instanceId);
      setLoadStep({
        state: "ok",
        message:
          res.count > 0
            ? `загружено моделей: ${res.count}`
            : "новых моделей нет — каталог уже актуален",
      });
      onConnected();
    } catch (e) {
      setLoadStep({ state: "error", message: describe(e) });
    }
  };

  const stepDot = (s: StepResult) =>
    s.state === "ok" ? "ok" : s.state === "error" ? "error" : "unknown";

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title={`Подключение: ${providerLabel(kind)}`}
      footer={
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Закрыть
          </Button>
          <Button
            variant="primary"
            disabled={testStep.state !== "ok"}
            loading={loadStep.state === "running"}
            onClick={runLoadModels}
          >
            Загрузить модели
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <Field
          label="Ключ доступа"
          htmlFor="cloud-api-key"
          hint={
            hasKey
              ? "Ключ уже сохранён — оставьте поле пустым, чтобы не менять его"
              : "Ключ хранится зашифрованным и обратно не показывается"
          }
          error={saveStep.state === "error" ? saveStep.message : null}
        >
          <input
            id="cloud-api-key"
            type="password"
            className={input}
            value={apiKey}
            autoComplete="off"
            placeholder={hasKey ? "••••••••" : "sk-…"}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>

        <Button
          variant="secondary"
          loading={testStep.state === "running" || saveStep.state === "running"}
          onClick={runSaveAndTest}
        >
          Сохранить и проверить
        </Button>

        <ol className="flex flex-col gap-2 text-xs">
          {[
            { label: "Ключ", step: saveStep },
            { label: "Связь с провайдером", step: testStep },
            { label: "Каталог моделей", step: loadStep },
          ].map(({ label, step }) => (
            <li key={label} className="flex items-start gap-2">
              <span className="mt-1">
                <StatusDot state={stepDot(step)} title={label} />
              </span>
              <span className="min-w-0">
                <span className="text-slate-300">{label}</span>
                {step.message && (
                  <span
                    className={`ml-1 ${step.state === "error" ? "text-red-400" : "text-slate-400"}`}
                  >
                    — {step.message}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ol>

        <p className="text-[11px] text-slate-400">
          После загрузки моделей они появятся в списке выбора. Слоты, через
          которые проходит содержимое документов, остаются локальными, пока для
          них отдельно не разрешить облако.
        </p>
      </div>
    </Sheet>
  );
}
