"use client";

import { useEffect, useState } from "react";

import PinPad from "@/components/mobile/PinPad";
import { biometricAvailable } from "@/lib/native-bridge";
import {
  cryptoSupported,
  disableQuickLogin,
  enableBiometric,
  enablePin,
  PIN_MAX_LEN,
  PIN_MIN_LEN,
  QuickLoginMethod,
  quickLoginMethod,
} from "@/lib/quick-login";

type SetupStep = "enter" | "confirm";

/**
 * Quick-login management for the mobile app: enable fingerprint or a PIN so the
 * password isn't asked on every launch. Enrolling requires the current
 * (password) session — the server issues a device secret that the phone keeps
 * behind biometrics / PIN encryption.
 */
export default function AppLockSettings() {
  const [method, setMethod] = useState<QuickLoginMethod | null>(null);
  const [bioOk, setBioOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // PIN setup dialog.
  const [setup, setSetup] = useState<SetupStep | null>(null);
  const [entry, setEntry] = useState("");
  const [first, setFirst] = useState("");
  const [setupErr, setSetupErr] = useState<string | null>(null);

  useEffect(() => {
    setMethod(quickLoginMethod());
    void biometricAvailable().then(setBioOk);
  }, []);

  async function turnOnBiometric() {
    setError(null);
    setBusy(true);
    try {
      await enableBiometric();
      setMethod("biometric");
    } catch (e) {
      setError(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  async function turnOff() {
    setBusy(true);
    try {
      await disableQuickLogin();
      setMethod(null);
    } finally {
      setBusy(false);
    }
  }

  function openPinSetup() {
    setEntry("");
    setFirst("");
    setSetupErr(null);
    setSetup("enter");
  }

  function cancelSetup() {
    setSetup(null);
    setEntry("");
    setFirst("");
    setSetupErr(null);
  }

  function submitEnter() {
    if (entry.length < PIN_MIN_LEN) {
      setSetupErr(`Минимум ${PIN_MIN_LEN} цифры`);
      return;
    }
    setFirst(entry);
    setEntry("");
    setSetupErr(null);
    setSetup("confirm");
  }

  async function submitConfirm() {
    if (entry !== first) {
      setSetupErr("PIN-коды не совпадают");
      setEntry("");
      return;
    }
    setBusy(true);
    try {
      await enablePin(first);
      setMethod("pin");
      cancelSetup();
    } catch (e) {
      setSetupErr(String((e as Error).message || e));
    } finally {
      setBusy(false);
    }
  }

  const rows: {
    key: QuickLoginMethod;
    label: string;
    hint?: string;
    disabled?: boolean;
    onEnable: () => void;
  }[] = [
    {
      key: "biometric",
      label: "Отпечаток / лицо",
      hint: bioOk ? undefined : "Недоступно на этом устройстве",
      disabled: !bioOk || busy,
      onEnable: turnOnBiometric,
    },
    {
      key: "pin",
      label: "PIN-код",
      disabled: !cryptoSupported() || busy,
      onEnable: openPinSetup,
    },
  ];

  return (
    <div className="border-t border-border pt-4">
      <div className="text-sm font-medium mb-1">Быстрый вход</div>
      <p className="text-xs text-muted-foreground mb-3">
        Вход по отпечатку или PIN вместо пароля. Первый вход — паролем, затем
        приложение запоминает вас на этом устройстве.
      </p>

      <div className="space-y-2">
        {rows.map((r) => {
          const active = method === r.key;
          return (
            <div
              key={r.key}
              className={`flex items-center justify-between rounded-md border px-3 py-2 ${
                active ? "border-primary bg-primary/5" : "border-border"
              } ${r.disabled && !active ? "opacity-50" : ""}`}
            >
              <span className="text-sm">
                {r.label}
                {r.hint && (
                  <span className="ml-2 text-xs text-muted-foreground">
                    {r.hint}
                  </span>
                )}
              </span>
              {active ? (
                <span className="text-xs font-medium text-primary">
                  Включено
                </span>
              ) : (
                <button
                  onClick={r.onEnable}
                  disabled={r.disabled}
                  className="text-xs font-medium text-primary hover:underline disabled:opacity-50 disabled:no-underline"
                >
                  Включить
                </button>
              )}
            </div>
          );
        })}
      </div>

      {method && (
        <button
          onClick={turnOff}
          disabled={busy}
          className="mt-3 text-xs text-red-500 hover:underline disabled:opacity-50"
        >
          Выключить быстрый вход
        </button>
      )}

      {error && <p className="mt-2 text-xs text-red-500">{error}</p>}

      {setup && (
        <div className="fixed inset-0 z-[110] flex flex-col items-center justify-center gap-6 bg-slate-950/95 px-6 text-slate-100">
          <p className="text-sm text-slate-300">
            {setup === "enter" ? "Придумайте PIN-код" : "Повторите PIN-код"}
          </p>
          <PinPad
            value={entry}
            onChange={(v) => {
              setSetupErr(null);
              setEntry(v);
            }}
            error={!!setupErr}
            disabled={busy}
          />
          {setupErr && <p className="text-xs text-red-400">{setupErr}</p>}
          <div className="flex items-center gap-4">
            <button
              onClick={cancelSetup}
              className="text-sm text-slate-400 hover:text-slate-200"
            >
              Отмена
            </button>
            <button
              onClick={setup === "enter" ? submitEnter : submitConfirm}
              disabled={entry.length < PIN_MIN_LEN || busy}
              className="rounded-lg bg-sky-500 px-6 py-2 text-sm font-medium text-white disabled:opacity-40"
            >
              {setup === "enter" ? "Далее" : busy ? "Сохранение…" : "Готово"}
            </button>
          </div>
          <p className="text-[11px] text-slate-500">
            От {PIN_MIN_LEN} до {PIN_MAX_LEN} цифр
          </p>
        </div>
      )}
    </div>
  );
}
