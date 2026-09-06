/**
 * Смоук по разделам, у которых не было ни одного e2e.
 *
 * Аудит показал десять таких: suppliers, warehouse, catalogs, drawings,
 * procurement, payments, collections, canonical, boms, admin. Это половина
 * ежедневно используемых экранов — поломка в любом из них обнаруживалась
 * только человеком, открывшим страницу.
 *
 * Проверка намеренно неглубокая: раздел открывается, не падает с ошибкой
 * приложения и показывает свой заголовок. Этого хватает, чтобы поймать самое
 * частое — сломанный импорт, ошибку рендера, обращение к несуществующему
 * полю ответа.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test section-smoke
 */
import { expect, test } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

const SECTIONS: { path: string; heading: RegExp }[] = [
  { path: "/suppliers", heading: /поставщик/i },
  { path: "/warehouse", heading: /склад/i },
  { path: "/catalogs", heading: /каталог/i },
  { path: "/drawings", heading: /чертеж|чертёж/i },
  { path: "/procurement", heading: /закуп/i },
  { path: "/payments", heading: /платеж|платёж|оплат/i },
  { path: "/collections", heading: /подборк|коллекц/i },
  { path: "/canonical", heading: /канон|номенклатур/i },
  { path: "/boms", heading: /состав|специфик|bom/i },
  { path: "/admin", heading: /админ|управлен/i },
];

for (const section of SECTIONS) {
  test(`раздел ${section.path} открывается и не падает`, async ({
    page,
    context,
  }) => {
    const appErrors: string[] = [];
    page.on("pageerror", (e) => appErrors.push(e.message));

    await setAuthCookie(context);
    await mockEmptyApi(page);

    const response = await page.goto(section.path);
    expect(response?.status(), `${section.path} отдал ошибку`).toBeLessThan(
      400,
    );

    // Next.js рисует свой экран при необработанном исключении в рендере —
    // именно его и надо поймать.
    await expect(
      page.getByText(/Application error|Unhandled Runtime Error/i),
    ).toHaveCount(0);

    await expect(page.getByRole("heading").first()).toBeVisible();
    expect(appErrors, `ошибки в консоли: ${appErrors.join("; ")}`).toEqual([]);
  });
}
