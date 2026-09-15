import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? 'test-results',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL ?? 'http://127.0.0.1:8001',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    navigationTimeout: 30_000,
    actionTimeout: 10_000,
  },
  projects: [
    {
      name: 'desktop',
      use: {
        ...devices['Desktop Chrome'],
        browserName: 'chromium',
        channel: 'chrome',
        viewport: { width: 1600, height: 1000 },
      },
    },
    {
      // 1024×768：antd Sider 200px + 工作区左右各 24px 之后内容列约 776px，
      // 是横向溢出最容易出现的一段（AGENTS.md 要求检查 1024px）。
      name: 'laptop',
      use: {
        browserName: 'chromium',
        channel: 'chrome',
        viewport: { width: 1024, height: 768 },
      },
    },
    {
      name: 'mobile',
      use: {
        browserName: 'chromium',
        channel: 'chrome',
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
})
