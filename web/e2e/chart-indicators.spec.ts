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
    entry_fill_count: 1,
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

async function bollingerFillPixelCount(page: Page) {
  return page.locator('.candlestick-host canvas').evaluateAll((canvases) =>
    canvases.reduce((total, canvas) => {
      const context = canvas.getContext('2d')
      if (!context) return total
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
      let matches = 0
      for (let index = 0; index < pixels.length; index += 4) {
        const red = pixels[index]
        const green = pixels[index + 1]
        const blue = pixels[index + 2]
        const alpha = pixels[index + 3]
        const transparentLayerMatch = alpha >= 24 && alpha <= 38 && red <= 80 && green >= 150 && blue >= 50
        const lightCanvasMatch =
          alpha >= 245 && red >= 220 && red <= 240 && green >= 242 && green <= 252 && blue >= 228 && blue <= 242
        if (transparentLayerMatch || lightCanvasMatch) matches += 1
      }
      return total + matches
    }, 0),
  )
}

async function assertDisplaySettingsLayout(modal: Locator) {
  const displayPanel = modal.locator('[data-testid="display-tab-panel"]')
  await expect(displayPanel).toBeVisible()
  await expect(modal.getByRole('tab', { name: '显示', exact: true })).toHaveAttribute('aria-selected', 'true')

  const geometry = await displayPanel.evaluate((panel) => {
    const rectangle = (element: Element) => {
      const rect = element.getBoundingClientRect()
      return { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width }
    }
    const intervalRow = panel.querySelector('[data-testid="default-interval-row"]')!
    const spacingRow = panel.querySelector('[data-testid="default-bar-spacing-row"]')!
    const spacingControl = spacingRow.querySelector('.zoom-setting-control')!
    const slider = spacingRow.querySelector('.ant-slider')!
    const spacingInput = spacingRow.querySelector('.ant-input-number-group-wrapper, .ant-input-number')!
    const rowParts = [intervalRow, spacingRow].map((row) => {
      const children = Array.from(row.children)
      return { label: rectangle(children[0]), control: rectangle(children[1]) }
    })
    const panels = Array.from(panel.querySelectorAll<HTMLElement>('.settings-panel')).map((element) => ({
      rect: rectangle(element),
      borderWidth: getComputedStyle(element).borderTopWidth,
      background: getComputedStyle(element).backgroundColor,
    }))
    return {
      interval: rectangle(intervalRow),
      spacing: rectangle(spacingRow),
      rowParts,
      panels,
      spacingControls: {
        wrapper: rectangle(spacingControl),
        slider: rectangle(slider),
        input: rectangle(spacingInput),
        marks: Array.from(spacingRow.querySelectorAll('.ant-slider-mark-text')).map(rectangle),
      },
      overflow: { scrollWidth: panel.scrollWidth, clientWidth: panel.clientWidth },
    }
  })

  expect(geometry.spacing.top).toBeGreaterThanOrEqual(geometry.interval.bottom - 1)
  expect(Math.abs(geometry.interval.left - geometry.spacing.left)).toBeLessThanOrEqual(1)
  expect(Math.abs(geometry.interval.width - geometry.spacing.width)).toBeLessThanOrEqual(1)
  expect(geometry.overflow.scrollWidth).toBeLessThanOrEqual(geometry.overflow.clientWidth + 1)
  expect(geometry.panels).toHaveLength(2)
  expect(geometry.panels.every((panel) => panel.borderWidth !== '0px' && panel.background !== 'rgba(0, 0, 0, 0)')).toBe(
    true,
  )
  for (const parts of geometry.rowParts) {
    const separated = parts.label.right <= parts.control.left + 1 || parts.label.bottom <= parts.control.top + 1
    expect(separated, 'display setting label overlaps its control').toBe(true)
  }

  const controls = geometry.spacingControls
  const controlsSeparated =
    controls.slider.right <= controls.input.left + 1 ||
    controls.input.right <= controls.slider.left + 1 ||
    controls.slider.bottom <= controls.input.top + 1 ||
    controls.input.bottom <= controls.slider.top + 1
  expect(controlsSeparated, 'bar spacing slider overlaps its numeric input').toBe(true)

  for (const control of [controls.slider, controls.input, ...controls.marks]) {
    expect(control.left).toBeGreaterThanOrEqual(geometry.spacing.left - 1)
    expect(control.right).toBeLessThanOrEqual(geometry.spacing.right + 1)
    expect(control.top).toBeGreaterThanOrEqual(geometry.spacing.top - 1)
    expect(control.bottom).toBeLessThanOrEqual(geometry.spacing.bottom + 1)
  }
  for (const mark of controls.marks) {
    const overlapsInput =
      mark.left < controls.input.right - 1 &&
      mark.right > controls.input.left + 1 &&
      mark.top < controls.input.bottom - 1 &&
      mark.bottom > controls.input.top + 1
    expect(overlapsInput, 'bar spacing mark overlaps its numeric input').toBe(false)
  }
}

async function assertIndicatorPanelsSeparated(modal: Locator, viewportWidth: number) {
  const activePanel = modal.locator('.ant-tabs-tabpane-active')
  const list = activePanel.locator('.indicator-list')
  const editor = activePanel.locator('.indicator-editor')
  await expect(list).toBeVisible()
  await expect(editor).toBeVisible()
  const [listBox, editorBox] = await Promise.all([list.boundingBox(), editor.boundingBox()])
  expect(listBox).not.toBeNull()
  expect(editorBox).not.toBeNull()
  if (!listBox || !editorBox) return
  const separated = listBox.x + listBox.width <= editorBox.x + 1 || listBox.y + listBox.height <= editorBox.y + 1
  expect(separated, 'indicator list panel overlaps parameter panel').toBe(true)
  if (viewportWidth <= 720) expect(editorBox.y).toBeGreaterThanOrEqual(listBox.y + listBox.height - 1)
  else expect(editorBox.x).toBeGreaterThanOrEqual(listBox.x + listBox.width - 1)
}

test('单笔复盘支持主副图指标配置并保持图表布局稳定', async ({ page }, testInfo) => {
  if (testInfo.project.name === 'mobile') await page.setViewportSize({ width: 360, height: 844 })
  const { putSeen } = await installApiMocks(page)
  await page.goto(`/#/backtests/${researchId}/trades/${tradeId}`)

  await expect(page.getByRole('heading', { name: 'BTCUSDT 单笔复盘' })).toBeVisible()
  await assertChartIsVisibleAndSettled(page)

  await page.locator('[aria-label="图表设置"]').click()
  const modal = page.locator('.ant-modal:visible')
  await expect(modal).toBeVisible()
  await expect(modal.getByRole('tab')).toHaveCount(3)
  await expect(modal.getByRole('tab').allTextContents()).resolves.toEqual(['显示', '主图指标', '副图指标'])
  await expect(modal.getByRole('combobox', { name: '默认周期' })).toBeVisible()
  await assertDisplaySettingsLayout(modal)
  const modalOverflow = await modal.evaluate((element) => ({
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
  }))
  expect(modalOverflow.scrollWidth).toBeLessThanOrEqual(modalOverflow.clientWidth + 1)
  await page.screenshot({
    path: testInfo.outputPath(`${testInfo.project.name}-chart-settings-display.png`),
    fullPage: true,
  })
  const barSpacingInput = modal.locator('[data-testid="default-bar-spacing-row"] .ant-input-number input')
  await barSpacingInput.fill('12.5')
  await barSpacingInput.press('Tab')
  await modal.getByRole('checkbox', { name: '信号价' }).uncheck()
  await modal.locator('[aria-label="信号价线型"]').click()
  await page.locator('.ant-select-dropdown:visible').getByText('点线', { exact: true }).click()
  await modal.locator('.default-interval-select').click()
  await page.locator('.ant-select-dropdown:visible').getByText('15m', { exact: true }).click()

  await modal.getByRole('tab', { name: '主图指标', exact: true }).click()
  await expect(modal.locator('[data-testid="main-tab-panel"]')).toBeVisible()
  await expect(modal.locator('[data-testid="display-tab-panel"]')).toBeHidden()
  await expect(modal.locator('[data-testid="sub-tab-panel"]')).toHaveCount(0)
  await expect(modal.locator('.ant-tabs-tabpane-active .indicator-list-item strong')).toHaveText(['EMA', 'MA', 'BOLL'])
  await assertIndicatorPanelsSeparated(modal, page.viewportSize()?.width ?? 0)

  const maItem = modal.locator('.ant-tabs-tabpane-active .indicator-list-item').filter({ hasText: '简单移动平均线' })
  await maItem.click()
  const maEditor = page.locator('section[aria-label="MA 参数"]')
  await expect(maEditor).toBeVisible()
  const maPeriod = maEditor.locator('.ant-input-number input').first()
  await maPeriod.fill('7')
  await maPeriod.press('Tab')
  await setColorInput(maEditor.locator('input[type="color"][aria-label="第 1 条线颜色"]'), '#dc2626')
  await maItem.getByRole('checkbox').check()
  await expect(maItem.getByRole('checkbox')).toBeChecked()

  const emaItem = modal.locator('.ant-tabs-tabpane-active .indicator-list-item').filter({ hasText: '指数移动平均线' })
  await emaItem.getByRole('checkbox').check()
  await expect(emaItem.getByRole('checkbox')).toBeChecked()

  const bollItem = modal.locator('.ant-tabs-tabpane-active .indicator-list-item').filter({ hasText: '布林通道' })
  await bollItem.click()
  const bollEditor = page.locator('section[aria-label="BOLL 参数"]')
  await expect(bollEditor).toBeVisible()
  await expect(bollEditor.getByText('通道边界', { exact: true })).toBeVisible()
  await expect(bollEditor.getByText('中轨', { exact: true })).toBeVisible()
  await expect(bollEditor.getByText('通道填充', { exact: true })).toBeVisible()
  await bollItem.getByRole('checkbox').check()
  await expect(bollItem.getByRole('checkbox')).toBeChecked()

  await modal.getByRole('tab', { name: '副图指标', exact: true }).click()
  await expect(modal.locator('[data-testid="sub-tab-panel"]')).toBeVisible()
  await expect(modal.locator('[data-testid="main-tab-panel"]')).toHaveCount(0)
  await expect(modal.locator('.ant-tabs-tabpane-active .indicator-list-item strong')).toHaveText([
    'VOL',
    'MACD',
    'KDJ',
    'RSI',
    'ATR',
  ])
  await assertIndicatorPanelsSeparated(modal, page.viewportSize()?.width ?? 0)

  const rsiItem = modal.locator('.ant-tabs-tabpane-active .indicator-list-item').filter({ hasText: '相对强弱指标' })
  await rsiItem.click()
  const rsiEditor = page.locator('section[aria-label="RSI 参数"]')
  await expect(rsiEditor).toBeVisible()
  const rsiPeriod = rsiEditor.locator('.ant-input-number input').first()
  await rsiPeriod.fill('14')
  await rsiPeriod.press('Tab')
  await rsiItem.getByRole('checkbox').check()
  await expect(rsiItem.getByRole('checkbox')).toBeChecked()
  await page.screenshot({
    path: testInfo.outputPath(`${testInfo.project.name}-chart-settings-sub.png`),
    fullPage: true,
  })

  await modal.getByRole('button', { name: '保存设置' }).click()
  const saved = await putSeen
  expect(saved).toMatchObject({
    default_interval: '15m',
    display: {
      default_bar_spacing: 12.5,
      price_lines: {
        signal: { visible: false, style: 'dotted', width: 1 },
      },
    },
    main: {
      ema: {
        enabled: true,
      },
      ma: {
        enabled: true,
        lines: [
          { period: 7, color: '#dc2626' },
          { period: 10, color: '#22c55e' },
          { period: 20, color: '#3b82f6' },
        ],
      },
      boll: {
        enabled: true,
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
  const mainIndicatorLabel = page.locator('.indicator-hover-label').first()
  await expect(mainIndicatorLabel).toContainText('EMA(9)')
  await expect(mainIndicatorLabel).toContainText('MA(7)')
  await expect(mainIndicatorLabel).toContainText('BOLL UP')
  await expect(mainIndicatorLabel).toContainText('MID')
  await expect(mainIndicatorLabel).toContainText('DOWN')
  await expect(page.locator('.indicator-hover-label').last()).toContainText('RSI(14)')
  await expect(page.locator('.indicator-hover-label').last()).toContainText(/-?\d/)
  await expect.poll(() => bollingerFillPixelCount(page)).toBeGreaterThan(100)

  const overflow = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
  }))
  expect(overflow.documentWidth).toBeLessThanOrEqual(overflow.viewportWidth + 1)
  await page.screenshot({ path: testInfo.outputPath(`${testInfo.project.name}-chart-indicators.png`), fullPage: true })
})
