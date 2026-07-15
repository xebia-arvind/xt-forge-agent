
import { defineConfig } from '@playwright/test';
import dotenv from 'dotenv';
dotenv.config();

const isCI = !!process.env.CI;
const runId = process.env.RUN_ID || `run_${Date.now()}`;

export default defineConfig({
  testDir: './tests',
  timeout: Number(process.env.TIMEOUT) || 60000,
  workers: isCI ? 1 : undefined,
  reporter: [
    ['html', { open: 'never', outputFolder: `playwright-report/${runId}` }],
    ['json', { outputFile: 'playwright-report/results.json' }],
    ['list']
  ],
  use: {
    baseURL: process.env.BASE_URL,
    headless: process.env.HEADLESS === "true",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    trace: "retain-on-failure"
  },

});
