const CURRENCY_SYMBOLS: Record<string, string> = {
  RUB: "₽",
  USD: "$",
  EUR: "€",
  CNY: "¥",
  KZT: "₸",
  BYN: "Br",
};

const CURRENCY_LOCALES: Record<string, string> = {
  RUB: "ru-RU",
  USD: "en-US",
  EUR: "de-DE",
  CNY: "zh-CN",
  KZT: "ru-KZ",
  BYN: "ru-BY",
};

export function formatCurrency(
  amount: number | null | undefined,
  currency = "RUB",
  opts?: { compact?: boolean },
): string {
  if (amount == null) return "—";
  const locale = CURRENCY_LOCALES[currency] ?? "ru-RU";
  const symbol = CURRENCY_SYMBOLS[currency] ?? currency;
  if (opts?.compact && Math.abs(amount) >= 1_000_000) {
    return (
      (amount / 1_000_000).toLocaleString(locale, {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
      }) +
      " М" +
      symbol
    );
  }
  if (opts?.compact && Math.abs(amount) >= 1_000) {
    return (
      (amount / 1_000).toLocaleString(locale, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 1,
      }) +
      " К" +
      symbol
    );
  }
  return (
    amount.toLocaleString(locale, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }) +
    " " +
    symbol
  );
}

export function formatAmount(
  amount: number | null | undefined,
  currency = "RUB",
): string {
  return formatCurrency(amount, currency);
}
