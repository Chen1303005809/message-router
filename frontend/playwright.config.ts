import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.PLAYWRIGHT_BASE_URL || 'http://127.0.0.1:5187'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL,
    channel: 'chrome',
    ...devices['Desktop Chrome'],
    trace: 'retain-on-failure',
  },
  ...(process.env.PLAYWRIGHT_BASE_URL
    ? {}
    : {
        webServer: {
          command: 'npm run dev -- --host 127.0.0.1 --port 5187',
          url: baseURL,
          reuseExistingServer: !process.env.CI,
          timeout: 30_000,
        },
      }),
})
