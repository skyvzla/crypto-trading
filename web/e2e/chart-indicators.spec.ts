import { expect, test, type Locator, type Page, type Route } from '@playwright/test'

const researchId = 'research-e2e'
const tradeId = 'trade-e2e'
const baseTimeSeconds = Math.floor(Date.parse('2026-01-01T00:00:00Z') / 1000)

const initialSettings = {
  main: {
    ema: {
      enabled: false,
      lines: [
        { period: 9, color: '#f5c451' },
        { period: 21, color: '#66b3ff' },
      ],
    },
    ma: {
      enabled: false,
      lines: [
        { period: 5, color: '#f59e0b' },
        { period: 10, color: '#22c55e' },
        { period: 20, color: '#3b82f6' },
      ],
    },
    boll: {
      enabled: false,
      period: 20,
      deviation: 2,
      colors: {
        upper: '#ef4444',
        middle: '#eab308',
        lower: '#22c55e',
      },
    },
  },
  sub: {
    volume: {
      enabled: true,
      ma_lines: [
        { period: 5, color: '#f5c451' },
        { period: 20, color: '#4da3ff' },
      ],
    },
    macd: {
      enabled: false,
      fast_period: 12,
      slow_period: 26,
      signal_period: 9,
      colors: {
        dif: '#4da3ff',
        dea: '#f5c451',
        histogram_up: '#2ebd85',
        histogram_down: '#f05252',
      },
    },
    kdj: {
      enabled: false,
      period: 9,
      colors: {
        k: '#4da3ff',
        d: '#f5c451',
        j: '#d98bff',
      },
    },
    rsi: {
      enabled: false,
      lines: [
        { period: 6, color: '#f5c451' },
        { period: 12, color: '#4da3ff' },
        { period: 24, color: '#d98bff' },
      ],
    },
    atr: {
      enabled: false,
      period: 14,
      color: '#14b8a6',
    },
  },
}

function makeTrade() {
  return {
    id: tradeId,
    trade_id: tradeId,
    research_id: researchId,
    strategy_id: 'spike_short',
    symbol: 'BTCUSDT',
    side: 'LONG',
    signal_time: '2026-01-01T07:55:00Z',
    entry_time: '2026-01-01T08:00:00Z',
    entry_price: 101,
    average_entry_price: 101,
    signal_price: 100.5,
    invalid_price: 95,
    exit_time: '2026-01-01T09:00:00Z',
    exit_price: 104,
    net_pnl: 3,
    net_return: 0.0297,
    winner: true,
    exit_reason: 'take_profit',
    filled_tier_count: 1,
    holding_seconds: 3600,
    tier_prices: [101, 100, 99],
    orders: [
      { id: 'order-1', tier: 1, price: 101, quantity: 1, status: 'filled', created_time: '2026-01-01T08:00:00Z' },
      { id: 'order-2', tier: 2, price: 100, quantity: 1, status: 'pending', created_time: '2026-01-01T08:00:00Z' },
      { id: 'order-3', tier: 3, price: 99, quantity: 1, status: 'pending', created_time: '2026-01-01T08:00:00Z' },
    ],
    fills: [
      {
        id: 'fill-1',
        tier: 1,
        time: '2026-01-01T08:00:00Z',
        price: 101,
        quantity: 1,
        side: 'buy',
      },
    ],
    parameters: { entry_window: 30 },
    metrics: { range_pct: 4.2 },
    attributes: { spike_high: 103 },
    strategy_data: { box_breakthrough: 100.8 },
  }
}

function makeCandles(interval: string) {
  const step = interval === '1s' ? 1 : 300
  return Array.from({ length: 240 }, (_, index) => {
    const time = baseTimeSeconds + index * step
    const close = 98 + index * 0.035 + Math.sin(index / 4) * 1.2
    const open = close - Math.sin(index / 3) * 0.45
    return {
      time,
      open: Number(open.toFixed(4)),
      high: Number((Math.max(open, close) + 0.7).toFixed(4)),
      low: Number((Math.min(open, close) - 0.7).toFixed(4)),
      close: Number(close.toFixed(4)),
      volume: 1_000 + index * 12 + Math.abs(Math.sin(index / 5)) * 500,
    }
  })
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

async function installApiMocks(page: Page) {
  let resolvePut!: (body: unknown) => void
  const putSeen = new Promise<unknown>((resolve) => {
    resolvePut = resolve
  })

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const pathname = url.pathname

    if (pathname === `/api/v1/backtest-researches/${researchId}/trades/${tradeId}`) {
      await fulfillJson(route, makeTrade())
      return
    }
    if (pathname === `/api/v1/backtest-researches/${researchId}/trades/${tradeId}/events`) {
      await fulfillJson(route, {
        items: [
          {
            id: 1,
            time: '2026-01-01T07:55:00Z',
            type: 'spike_detected',
            title: '尖峰确认',
            description: '回测测试事件',
            price: 100.5,
            data: { range_pct: 4.2 },
          },
        ],
      })
      return
    }
    if (pathname === '/api/v1/backtest-strategies/spike_short/schema') {
      await fulfillJson(route, { strategy_id: 'spike_short', label: '尖峰策略', chart_overlays: [] })
      return
    }
    if (pathname === '/api/v1/backtest-candles') {
      const interval = url.searchParams.get('interval') ?? '5m'
      await fulfillJson(route, {
        symbol: url.searchParams.get('symbol') ?? 'BTCUSDT',
        interval,
        source: url.searchParams.get('source') === 'archive' ? 'archive' : 'binance',
        candles: makeCandles(interval),
      })
      return
    }
    if (pathname === '/api/v1/chart-settings') {
      if (request.method() === 'PUT') {
        const body = request.postDataJSON()
        resolvePut(body)
        await fulfillJson(route, { ...body, updated_at: '2026-01-01T00:00:00Z' })
      } else {
        await fulfillJson(route, initialSettings)
      }
      return
    }

    await route.continue()
  })

  return { putSeen }
}

async function setColorInput(locator: Locator, value: string) {
  await locator.evaluate((input, nextValue) => {
    const element = input as HTMLInputElement
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set
    setter?.call(element, nextValue)
    element.dispatchEvent(new Event('input', { bubbles: true }))
    element.dispatchEvent(new Event('change', { bubbles: true }))
  }, value)
}

async function assertChartIsVisibleAndSettled(page: Page, minimumLabels = 1) {
  const chart = page.locator('.candlestick-host')
  await expect(chart).toBeVisible()
  await expect.poll(async () => chart.locator('canvas').count()).toBeGreaterThan(0)
  await expect.poll(async () => chart.locator('.indicator-hover-label').count()).toBeGreaterThanOrEqual(minimumLabels)

  const canvasState = await chart.locator('canvas').evaluateAll((canvases) =>
    canvases.map((canvas) => {
      const context = canvas.getContext('2d')
      if (!context || canvas.width === 0 || canvas.height === 0) return false
      const sampleWidth = Math.min(canvas.width, 320)
      const sampleHeight = Math.min(canvas.height, 240)
      const pixels = context.getImageData(0, 0, sampleWidth, sampleHeight).data
      for (let index = 0; index < pixels.length; index += 4) {
        if (pixels[index] || pixels[index + 1] || pixels[index + 2] || pixels[index + 3]) return true
      }
      return false
    }),
  )
  expect(canvasState.some(Boolean)).toBe(true)

  const geometry = await chart.evaluate((element) => {
    const chartRect = element.getBoundingClientRect()
    const labels = Array.from(element.querySelectorAll<HTMLElement>('.indicator-hover-label')).map((label) => {
      const rect = label.getBoundingClientRect()
      return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }
    })
    const overflow = { scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }
    return { chartRect: { left: chartRect.left, top: chartRect.top }, labels, overflow }
  })

  expect(geometry.labels.length).toBeGreaterThanOrEqual(minimumLabels)
  expect(
    geometry.labels.every(
      (label) => label.left >= geometry.chartRect.left && label.left < geometry.chartRect.left + 48,
    ),
  ).toBe(true)
  for (let first = 0; first < geometry.labels.length; first += 1) {
    for (let second = first + 1; second < geometry.labels.length; second += 1) {
      const left = geometry.labels[first]
      const right = geometry.labels[second]
      const separated =
        left.right <= right.left || right.right <= left.left || left.bottom <= right.top || right.bottom <= left.top
      expect(separated, `indicator labels ${first} and ${second} overlap`).toBe(true)
    }
  }
  expect(geometry.overflow.scrollWidth).toBeLessThanOrEqual(geometry.overflow.clientWidth + 1)
}

test('单笔复盘支持主副图指标配置并保持图表布局稳定', async ({ page }, testInfo) => {
  const { putSeen } = await installApiMocks(page)
  await page.goto(`/#/backtests/${researchId}/trades/${tradeId}`)

  await expect(page.getByRole('heading', { name: 'BTCUSDT 单笔复盘' })).toBeVisible()
  await assertChartIsVisibleAndSettled(page)

  await page.locator('[aria-label="配置技术指标"]').click()
  const modal = page.locator('.ant-modal:visible')
  await expect(modal).toBeVisible()
  await expect(page.getByText('主图指标', { exact: true })).toBeVisible()
  await expect(page.getByText('副图指标', { exact: true })).toBeVisible()

  const maItem = page.locator('.indicator-list-item').filter({ hasText: '简单移动平均线' })
  await maItem.click()
  const maEditor = page.locator('section[aria-label="MA 参数"]')
  await expect(maEditor).toBeVisible()
  const maPeriod = maEditor.locator('.ant-input-number input').first()
  await maPeriod.fill('7')
  await maPeriod.press('Tab')
  await setColorInput(maEditor.locator('input[type="color"][aria-label="第 1 条线颜色"]'), '#dc2626')
  await maItem.getByRole('checkbox').check()
  await expect(maItem.getByRole('checkbox')).toBeChecked()

  const rsiItem = page.locator('.indicator-list-item').filter({ hasText: '相对强弱指标' })
  await rsiItem.click()
  const rsiEditor = page.locator('section[aria-label="RSI 参数"]')
  await expect(rsiEditor).toBeVisible()
  const rsiPeriod = rsiEditor.locator('.ant-input-number input').first()
  await rsiPeriod.fill('14')
  await rsiPeriod.press('Tab')
  await rsiItem.getByRole('checkbox').check()
  await expect(rsiItem.getByRole('checkbox')).toBeChecked()

  await modal.getByRole('button', { name: '保存设置' }).click()
  const saved = await putSeen
  expect(saved).toMatchObject({
    main: {
      ma: {
        enabled: true,
        lines: [
          { period: 7, color: '#dc2626' },
          { period: 10, color: '#22c55e' },
          { period: 20, color: '#3b82f6' },
        ],
      },
    },
    sub: {
      rsi: {
        enabled: true,
        lines: [
          { period: 14, color: '#f5c451' },
          { period: 12, color: '#4da3ff' },
          { period: 24, color: '#d98bff' },
        ],
      },
    },
  })
  await expect(page.locator('.ant-modal:visible')).toHaveCount(0)

  await assertChartIsVisibleAndSettled(page, 3)
  await expect(page.locator('.indicator-hover-label').first()).not.toContainText('MA7')
  await expect(page.locator('.indicator-hover-label').last()).not.toContainText('RSI14')
  await expect(page.locator('.indicator-hover-label').last()).toContainText(/-?\d/)

  const overflow = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
  }))
  expect(overflow.documentWidth).toBeLessThanOrEqual(overflow.viewportWidth + 1)
  await page.screenshot({ path: testInfo.outputPath(`${testInfo.project.name}-chart-indicators.png`), fullPage: true })
})
