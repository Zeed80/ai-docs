"use client";

import { useEffect, useState } from "react";

import {
  clearPin,
  getLockMode,
  hasPin,
  LockMode,
  PIN_MAX_LEN,
  PIN_MIN_LEN,
  pinSupported,
  setLockMode,
  setPin as storePin,
} from "@/lib/app-lock";
import PinPad from "@/components/mobile/PinPad";
import { biometricAvailable } from "@/lib/native-bridge";

type SetupStep = "enter" | "confirm";

/**
 * Lock-method chooser for the mobile app: off, biometric or a numeric PIN.
 * Picking "PIN" walks the user through a two-step set/confirm before the mode
 * takes effect, so a lock is never armed without a working credential.
 */
export default function AppLockSettings() {
  const [mode, setMode] = useState<LockMode>("off");
  const [bioOk, setBioOk] = useState(false);
  const [pinSet, setPinSet] = useState(false);

  // PIN setup dialog state.
  const [setup, setSetup] = useState<SetupStep | null>(null);
  const [entry, setEntry] = useState("");
  const [first, setFirst] = useState("");
  const [setupErr, setSetupErr] = useState<string | null>(null);

  useEffect(() => {
    setMode(getLockMode());
    setPinSet(hasPin());
    void biometricAvailable().then(setBioOk);
  }, []);

  function choose(next: LockMode) {
    if (next === "biometric" && !bioOk) return;
    if (next === "pin" && !pinSet) {
      openSetup();
      return;
    }
    setLockMode(next);
    setMode(next);
  }

  function openSetup() {
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
    await storePin(first);
    setPinSet(true);
    setLockMode("pin");
    setMode("pin");
    cancelSetup();
  }

  function removePin() {
    clearPin();
    setPinSet(false);
    if (getLockMode() === "pin") {
      setLockMode("off");
      setMode("off");
    }
  }

  const options: {
    key: LockMode;
    label: string;
    hint?: string;
    disabled?: boolean;
  }[] = [
    { key: "off", label: "Выключена" },
    {
      key: "biometric",
      label: "Отпечаток / лицо",
      hint: bioOk ? undefined : "Недоступно на этом устройстве",
      disabled: !bioOk,
    },
    {
      key: "pin",
      label: "PIN-код",
      hint: pinSet ? "PIN задан" : "Задать PIN",
      disabled: !pinSupported(),
    },
  ];

  return (
    <div className="border-t border-border pt-4">
      <div className="text-sm font-medium mb-1">Блокировка приложения</div>
      <p className="text-xs text-muted-foreground mb-3">
        Защита входа в приложение. Не заменяет вход на сервер.
      </p>

      <div className="space-y-2">
        {options.map((o) => (
          <label
            key={o.key}
            className={`flex items-center justify-between rounded-md border px-3 py-2 ${
              o.disabled ? "opacity-50" : "cursor-pointer hover:bg-muted/50"
            } ${mode === o.key ? "border-primary bg-primary/5" : "border-border"}`}
          >
            <span className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="lock-mode"
                checked={mode === o.key}
                disabled={o.disabled}
                onChange={() => choose(o.key)}
                className="accent-primary"
              />
              {o.label}
            </span>
            {o.hint && (
              <span className="text-xs text-muted-foreground">{o.hint}</span>
            )}
          </label>
        ))}
      </div>

      {pinSet && (
        <div className="mt-3 flex gap-4 text-xs">
          <button onClick={openSetup} className="text-primary hover:underline">
            Изменить PIN
          </button>
          <button onClick={removePin} className="text-red-500 hover:underline">
            Удалить PIN
          </button>
        </div>
      )}

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
              disabled={entry.length < PIN_MIN_LEN}
              className="rounded-lg bg-sky-500 px-6 py-2 text-sm font-medium text-white disabled:opacity-40"
            >
              {setup === "enter" ? "Далее" : "Готово"}
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
