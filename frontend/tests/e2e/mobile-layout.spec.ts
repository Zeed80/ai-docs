/**
 * Раскладка на телефоне.
 *
 * Интерфейс открывают не только с монитора: Android-оболочка (mobile/) —
 * тонкий WebView поверх этого же сайта, то есть «мобильное приложение» и есть
 * этот UI на экране 320–412 px. Проверять это глазами получалось редко, а
 * ломалось предсказуемо: строка `flex items-center` без переноса, в которой
 * заголовок, поиск и три кнопки перестают помещаться.
 *
 * Проверка ровно одна и объективная: страница не должна прокручиваться вбок.
 * Широкие таблицы это не запрещает — им положен свой контейнер с
 * `overflow-x: auto`, и элементы внутри такого контейнера из проверки
 * исключены.
 *
 * Запуск: PLAYWRIGHT_MOCK_API=1 npx playwright test mobile-layout
 */
import { devices, expect, test } from "@playwright/test";
import { mockEmptyApi, setAuthCookie } from "./helpers/mock-api";

// Узкий и обычный телефон: 320 px — практический минимум (Galaxy S9+ и
// подобные), 412 px — типовой Android сегодня.
const VIEWPORTS = [
  { name: "320px", width: 320, height: 658 },
  { name: "412px", ...devices["Pixel 7"].viewport },
];

const PAGES = [
  "/",
  "/documents",
  "/invoices",
  "/suppliers",
  "/warehouse",
  "/catalogs",
  "/drawings",
  "/procurement",
  "/payments",
  "/collections",
  "/boms",
  "/work-orders",
  "/settings",
  "/settings/models?tab=assignment",
  "/settings/models?tab=overview",
];

/**
 * Элементы, вылезающие за правый край окна и не лежащие в контейнере с
 * горизонтальной прокруткой. `position: fixed` пропускаем: оболочка держит там
 * шапку и нижнюю панель, они позиционируются относительно окна.
 */
async function overflowingElements(page: import("@playwright/test").Page) {
  return page.evaluate(() => {
    const inScrollableX = (el: Element): boolean => {
      let n = el.parentElement;
      while (n) {
        const s = getComputedStyle(n);
        if (
          /(auto|scroll)/.test(s.overflowX) &&
          n.scrollWidth > n.clientWidth + 2
        ) {
          return true;
        }
        n = n.parentElement;
      }
      return false;
    };
    const out: string[] = [];
    for (const el of Array.from(document.querySelectorAll("body *"))) {
      const r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.right > window.innerWidth + 2)) continue;
      if (getComputedStyle(el).position === "fixed") continue;
      if (inScrollableX(el)) continue;
      out.push(
        `${el.tagName.toLowerCase()}.${String(el.className || "").slice(0, 40)} ` +
          `«${(el.textContent || "").trim().slice(0, 30)}» +${Math.round(r.right - window.innerWidth)}px`,
      );
      if (out.length >= 5) break;
    }
    return out;
  });
}

for (const vp of VIEWPORTS) {
  test.describe(`экран ${vp.name}`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });

    for (const path of PAGES) {
      test(`${path} помещается по ширине`, async ({ page, context }) => {
        await setAuthCookie(context);
        await mockEmptyApi(page);
        await page.goto(path);
        await page.waitForLoadState("networkidle").catch(() => {});

        const overflowing = await overflowingElements(page);
        expect(
          overflowing,
          `выходят за правый край:\n${overflowing.join("\n")}`,
        ).toEqual([]);

        const scrolls = await page.evaluate(
          () => document.body.scrollWidth > window.innerWidth + 2,
        );
        expect(scrolls, "страница прокручивается вбок").toBe(false);
      });
    }
  });
}
