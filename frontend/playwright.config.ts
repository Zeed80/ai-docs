import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: "http://127.0.0.1:13000",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command:
        "cd .. && APP_ENV=development AUTO_CREATE_SCHEMA=true DATABASE_URL=sqlite:///./data/e2e.db STORAGE_ROOT=./data/e2e-storage python3 -m uvicorn backend.app.main:app --host 127.0.0.1 --port 18000",
      url: "http://127.0.0.1:18000/health",
      reuseExistingServer: true,
      timeout: 20_000,
    },
    {
      command: "NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:18000 npx next dev --hostname 127.0.0.1 --port 13000",
      url: "http://127.0.0.1:13000",
      reuseExistingServer: true,
      timeout: 30_000,
    },
  ],
});
