import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

/**
 * Юнит-тесты фронтенда.
 *
 * До этого на фронте был только Playwright: чтобы проверить чистую функцию —
 * фильтр каталога, форматирование цены — приходилось поднимать браузер и
 * стенд, поэтому такие функции не проверялись вовсе. E2E остаётся за
 * Playwright: сюда попадает только tests/unit.
 *
 * Расширение .mts, а не .ts: конфиг написан в ESM, а ближайший package.json
 * не объявляет "type": "module" — Vite грузил бы файл как CommonJS.
 */
export default defineConfig({
  plugins: [react()],
  // Алиас `@/…` берётся из tsconfig.json — Vite умеет это сам, отдельный
  // плагин не нужен.
  resolve: { tsconfigPaths: true },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["tests/unit/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/unit/setup.ts"],
  },
});
