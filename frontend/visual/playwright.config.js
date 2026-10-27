import { defineConfig, devices } from "@playwright/test";
// Design review only: screenshots every screen and runs the axe contrast rule.
// Kept out of ./tests so the functional suite and CI stay unchanged.
export default defineConfig({
  testDir: ".",
  testMatch: "*.visual.js",
  workers: 1,
  retries: 0,
  timeout: 90000,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:5001",
    ...devices["Desktop Chrome"],
  },
});
