import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { router } from '@/router'
import { operationsApi } from '@/api/operations'
import OverviewView from '@/views/OverviewView.vue'
import CalendarView from '@/views/CalendarView.vue'
import CategoryManagementView from '@/views/CategoryManagementView.vue'
import PerformanceView from '@/views/PerformanceView.vue'
import TradeReviewView from '@/views/TradeReviewView.vue'
import StrategyRiskView from '@/views/StrategyRiskView.vue'
import UniverseView from '@/views/UniverseView.vue'

beforeEach(async () => {
  vi.restoreAllMocks()
  vi.spyOn(operationsApi, 'accounts').mockResolvedValue({
    items: [], total: 0, limit: 1000, offset: 0
  })
  await router.push('/overview')
  await router.isReady()
})

describe('operations views', () => {
  it('overview defaults closed PnL to all accounts while floating PnL stays account-scoped', async () => {
    vi.spyOn(operationsApi, 'health').mockResolvedValue({ status: 'healthy', service: 'ledger', timestamp: '2026-08-16T00:00:00Z' })
    vi.spyOn(operationsApi, 'runtimeStatus').mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 })
    vi.spyOn(operationsApi, 'pnl').mockResolvedValue({
      account_id: 'acct',
      strategy_id: null,
      symbol: null,
      total_trades: 0,
      total_commission: '0',
      total_realized_pnl: '0',
      total_unrealized_pnl: '0',
      net_pnl: '0',
      win_count: 0,
      loss_count: 0,
      win_rate: 0,
      avg_win: '0',
      avg_loss: '0'
    })
    vi.spyOn(operationsApi, 'positions').mockResolvedValue({ items: [], total: 0, limit: 1, offset: 0 })
    vi.spyOn(operationsApi, 'orders').mockResolvedValue({ items: [], total: 0, limit: 1, offset: 0 })
    vi.spyOn(operationsApi, 'trades').mockResolvedValue({ items: [], total: 0, limit: 6, offset: 0 })
    const dailyPnl = vi.spyOn(operationsApi, 'dailyPnl').mockResolvedValue([])

    const wrapper = mount(OverviewView)
    await flushPromises()

    expect(wrapper.text()).not.toContain('填写账户后显示收益')
    expect(wrapper.text()).toContain('本月没有已实现收益记录')
    expect(dailyPnl.mock.calls[0][0]).not.toHaveProperty('account_id')
    expect(operationsApi.pnl).not.toHaveBeenCalled()
  })

  it('performance keeps the account boundary visible instead of mixing accounts', () => {
    const wrapper = mount(PerformanceView)
    expect(wrapper.text()).toContain('请选择账户 ID')
    expect(wrapper.text()).toContain('绩效分析按账户归属')
  })

  it('keeps current floating PnL and selected daily PnL scoped to the chosen account', async () => {
    await router.push('/overview?account_id=acct')
    vi.spyOn(operationsApi, 'health').mockResolvedValue({ status: 'healthy', service: 'ledger', timestamp: '2026-08-16T00:00:00Z' })
    vi.spyOn(operationsApi, 'runtimeStatus').mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 })
    const pnl = vi.spyOn(operationsApi, 'pnl').mockResolvedValue({
      account_id: 'acct', strategy_id: null, symbol: null, total_trades: 0,
      total_commission: '0', total_realized_pnl: '0', total_unrealized_pnl: '0',
      net_pnl: '0', win_count: 0, loss_count: 0, win_rate: 0, avg_win: '0', avg_loss: '0'
    })
    vi.spyOn(operationsApi, 'positions').mockResolvedValue({ items: [], total: 0, limit: 1, offset: 0 })
    vi.spyOn(operationsApi, 'orders').mockResolvedValue({ items: [], total: 0, limit: 1, offset: 0 })
    vi.spyOn(operationsApi, 'trades').mockResolvedValue({ items: [], total: 0, limit: 6, offset: 0 })
    const dailyPnl = vi.spyOn(operationsApi, 'dailyPnl').mockResolvedValue([])

    const wrapper = mount(OverviewView)
    await flushPromises()

    expect(wrapper.text()).toContain('账户 acct')
    expect(pnl).toHaveBeenCalledWith(expect.objectContaining({ account_id: 'acct' }))
    expect(dailyPnl).toHaveBeenCalledWith(expect.objectContaining({ account_id: 'acct' }))
  })

  it('shows a retry control when the account directory cannot be read', async () => {
    vi.mocked(operationsApi.accounts).mockRejectedValue(new Error('unavailable'))

    const wrapper = mount(PerformanceView)
    await flushPromises()

    expect(wrapper.text()).toContain('账户列表读取失败')
    expect(wrapper.find('[aria-label="重新读取账户列表"]').exists()).toBe(true)
  })

  it('performance consumes authoritative breakdown rows in Shanghai time', async () => {
    await router.push('/performance?account_id=acct&tab=breakdown&group_by=symbol')
    vi.spyOn(operationsApi, 'performance').mockResolvedValue({
      account_id: 'acct', strategy_id: null, symbol: null, start_date: '2026-08-01', end_date: '2026-08-30', timezone: 'Asia/Shanghai',
      total_trades: 1, total_fills: 2, win_count: 1, loss_count: 0, flat_count: 0, win_rate: 1,
      avg_win: '5', avg_loss: '0', payoff_ratio: null, expectancy: '5', profit_factor: null,
      total_commission: '0.1', total_realized_pnl: '5.1', net_pnl: '5', max_drawdown: '0',
      candidate_campaigns: 1, excluded_campaigns: 0, unattributed_fills: 0,
      metric_scope: 'closed campaigns'
    })
    vi.spyOn(operationsApi, 'dailyPnl').mockResolvedValue([])
    const breakdown = vi.spyOn(operationsApi, 'performanceBreakdown').mockResolvedValue({
      account_id: 'acct', strategy_id: null, symbol: null, category_key: null, subcategory_key: null, side: null,
      start_date: '2026-08-01', end_date: '2026-08-30', timezone: 'Asia/Shanghai', group_by: 'symbol',
      dimension_available: true, dimension_note: null, available_dimensions: ['symbol', 'category', 'subcategory', 'side'],
      metric_scope: 'closed campaigns', items: [{
        dimension_key: 'BTCUSDT', dimension_label: 'BTCUSDT', total_trades: 1, total_fills: 2,
        win_count: 1, loss_count: 0, flat_count: 0, win_rate: 1, avg_win: '5', avg_loss: '0',
        payoff_ratio: null, expectancy: '5', profit_factor: null, total_commission: '0.1',
        total_realized_pnl: '5.1', net_pnl: '5', max_drawdown: '0', candidate_campaigns: 1, excluded_campaigns: 0
      }]
    })

    const wrapper = mount(PerformanceView)
    await flushPromises()

    expect(breakdown).toHaveBeenCalledWith(expect.objectContaining({
      account_id: 'acct', timezone: 'Asia/Shanghai', group_by: 'symbol'
    }))
    expect(wrapper.text()).toContain('BTCUSDT')
    expect(wrapper.text()).toContain('按权威账本维度分组')
    expect(wrapper.text()).toContain('净 PnL 仅扣 USDT 手续费；资金费不可用')
  })

  it('trade review sends calendar drill-down dates to the backend', async () => {
    await router.push('/trades?account_id=acct&date=2026-08-01&page=3&page_size=50')
    const campaigns = vi.spyOn(operationsApi, 'campaigns').mockResolvedValue({ items: [], total: 200, limit: 50, offset: 100, unattributed_fills: 0 })

    mount(TradeReviewView)
    await flushPromises()

    expect(campaigns).toHaveBeenCalledWith(expect.objectContaining({
      account_id: 'acct', start_date: '2026-08-01', end_date: '2026-08-01', timezone: 'Asia/Shanghai', limit: 50, offset: 100
    }))
  })

  it('keeps campaign identity, symbol, and status in separate review-table cells', async () => {
    await router.push('/trades?account_id=acct')
    vi.spyOn(operationsApi, 'campaigns').mockResolvedValue({
      items: [{ account_id: 'acct', strategy_id: 's', symbol: 'BTCUSDT', campaign_id: 'campaign-with-a-long-identifier', side: 'SHORT', fill_count: 2, sell_quantity: '1', buy_quantity: '1', total_commission: '0.1', commission_asset: 'USDT', gross_realized_pnl: '10', net_realized_pnl: '9.9', first_fill_at: '2026-08-01T00:00:00Z', last_fill_at: '2026-08-01T00:01:00Z', closed_at: '2026-08-01T00:01:00Z', has_open_quantity: false, pnl_facts_complete: true }],
      total: 1,
      limit: 50,
      offset: 0,
      unattributed_fills: 0
    })

    const wrapper = mount(TradeReviewView)
    await flushPromises()

    const symbol = wrapper.find('.campaign-symbol')
    const campaign = wrapper.find('.campaign-button')
    const status = wrapper.find('.campaign-status')
    const symbolCell = symbol.element.closest('td')
    const campaignCell = campaign.element.closest('td')
    const statusCell = status.element.closest('td')

    expect(symbol.text()).toBe('BTCUSDT')
    expect(campaign.text()).toBe('campaign-with-a-long-identifier')
    expect(status.text()).toBe('已结束')
    expect(symbolCell).not.toBeNull()
    expect(campaignCell).not.toBe(symbolCell)
    expect(statusCell).not.toBe(symbolCell)
    expect(statusCell).not.toBe(campaignCell)
    expect(wrapper.find('.filter-ledger > .trade-date-filter').text()).toContain('成交日期')
    expect(wrapper.text()).not.toContain('上海自然日')
  })

  it('clears the slotted trade-date filter together with common filters', async () => {
    await router.push('/trades?account_id=acct&date=2026-08-01')
    const campaigns = vi.spyOn(operationsApi, 'campaigns').mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0, unattributed_fills: 0 })

    const wrapper = mount(TradeReviewView)
    await flushPromises()
    await wrapper.get('[aria-label="清空筛选"]').trigger('click')
    await flushPromises()

    const request = campaigns.mock.calls.at(-1)?.[0]
    expect(request).toBeDefined()
    expect(request).not.toHaveProperty('start_date')
    expect(request).not.toHaveProperty('end_date')
    expect(request).not.toHaveProperty('timezone')
  })

  it('does not subtract non-USDT commission from realized PnL', async () => {
    await router.push('/trades?account_id=acct')
    vi.spyOn(operationsApi, 'campaigns').mockResolvedValue({
      items: [{ account_id: 'acct', strategy_id: 's', symbol: 'BTCUSDT', campaign_id: 'campaign', side: 'SHORT', fill_count: 2, sell_quantity: '1', buy_quantity: '1', total_commission: '0.1', commission_asset: 'BNB', gross_realized_pnl: '10', net_realized_pnl: null, first_fill_at: '2026-08-01T00:00:00Z', last_fill_at: '2026-08-01T00:01:00Z', closed_at: '2026-08-01T00:01:00Z', has_open_quantity: false, pnl_facts_complete: false }],
      total: 1,
      limit: 50,
      offset: 0,
      unattributed_fills: 0
    })

    const wrapper = mount(TradeReviewView)
    await flushPromises()

    expect(wrapper.text()).toContain('不可用')
    expect(wrapper.text()).toContain('0.10 BNB')
    expect(wrapper.text()).not.toContain('9.90')
  })

  it('does not display an aggregate for mixed commission assets', async () => {
    await router.push('/trades?account_id=acct')
    vi.spyOn(operationsApi, 'campaigns').mockResolvedValue({
      items: [{ account_id: 'acct', strategy_id: 's', symbol: 'BTCUSDT', campaign_id: 'mixed-fees', side: 'SHORT', fill_count: 2, sell_quantity: '1', buy_quantity: '1', total_commission: null, commission_asset: null, gross_realized_pnl: '10', net_realized_pnl: null, first_fill_at: '2026-08-01T00:00:00Z', last_fill_at: '2026-08-01T00:01:00Z', closed_at: '2026-08-01T00:01:00Z', has_open_quantity: false, pnl_facts_complete: false }],
      total: 1,
      limit: 50,
      offset: 0,
      unattributed_fills: 0
    })

    const wrapper = mount(TradeReviewView)
    await flushPromises()

    expect(wrapper.text()).toContain('— 资产不一致')
    expect(wrapper.text()).not.toContain('0.11')
  })

  it('marks open campaign PnL as a provisional realized amount', async () => {
    await router.push('/trades?account_id=acct')
    vi.spyOn(operationsApi, 'campaigns').mockResolvedValue({
      items: [{ account_id: 'acct', strategy_id: 's', symbol: 'BTCUSDT', campaign_id: 'open', side: 'SHORT', fill_count: 2, sell_quantity: '2', buy_quantity: '1', total_commission: '0.2', commission_asset: 'USDT', gross_realized_pnl: '5', net_realized_pnl: null, first_fill_at: '2026-08-01T00:00:00Z', last_fill_at: '2026-08-01T00:01:00Z', closed_at: null, has_open_quantity: true, pnl_facts_complete: false }],
      total: 1,
      limit: 50,
      offset: 0,
      unattributed_fills: 0
    })
    vi.spyOn(operationsApi, 'campaignPnl').mockResolvedValue({
      account_id: 'acct', strategy_id: 's', symbol: 'BTCUSDT', campaign_id: 'open', trade_count: 2,
      sell_quantity: '2', sell_avg_price: '100', buy_quantity: '1', buy_avg_price: '95',
      total_commission: '0.2', commission_asset: 'USDT', gross_realized_pnl: '5', net_realized_pnl: '4.8',
      remaining_quantity: '1', has_open_quantity: true, acquired_at: null,
      first_fill_at: '2026-08-01T00:00:00Z', last_fill_at: '2026-08-01T00:01:00Z', closed_at: null,
      released_at: null, lifecycle_duration_ms: null
    })
    vi.spyOn(operationsApi, 'strategyAuditEvents').mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 })
    vi.spyOn(operationsApi, 'trades').mockResolvedValue({ items: [], total: 0, limit: 1000, offset: 0 })

    const wrapper = mount(TradeReviewView, { attachTo: document.body })
    await flushPromises()
    await wrapper.get('.campaign-button').trigger('click')
    await flushPromises()

    expect(document.body.textContent).toContain('当前净已实现 PnL')
    expect(document.body.textContent).toContain('仍有敞口')
    expect(document.body.textContent).toContain('不是最终轮次收益')
    wrapper.unmount()
  })

  it('defaults the calendar to the all-account aggregate and renders close-day counts', async () => {
    await router.push('/calendar?month=2026-08')
    const dailyPnl = vi.spyOn(operationsApi, 'dailyPnl').mockResolvedValue([{
      date: '2026-08-01', account_id: null, strategy_id: null, symbol: null, timezone: 'Asia/Shanghai', campaign_count: 2, fill_count: 5, trade_count: 5, realized_trade_count: 2, gross_realized_pnl: '10.2', total_commission: '0.2', commission_asset: 'USDT', net_pnl: '10', funding_fee: null, net_pnl_scope: 'realized PnL minus USDT commission; funding fee facts unavailable'
    }])

    const wrapper = mount(CalendarView)
    await flushPromises()

    expect(wrapper.text()).toContain('闭合 Campaign')
    expect(wrapper.text()).toContain('2 Campaign · 5 fills')
    expect(wrapper.text()).toContain('closed_at 上海自然日')
    expect(dailyPnl.mock.calls[0][0]).not.toHaveProperty('account_id')
  })

  it('loads unclassified symbols from the backend and marks a failed sync', async () => {
    vi.spyOn(operationsApi, 'categoriesPage').mockResolvedValue({
      items: [{ category_key: 'cat', source: 'binance', category_type: 'CATEGORY', code: 'cat', name: 'Category', parent_key: null, active: true, synced_at: '2026-08-16T00:00:00Z', symbol_count: 1 }],
      total: 1, limit: 1000, offset: 0
    })
    vi.spyOn(operationsApi, 'symbolSyncStatus').mockResolvedValue({ initialized: true, status: 'FAILED', last_attempt_at: '2026-08-16T00:00:00Z', last_success_at: '2026-08-15T00:00:00Z', synced_symbols: 1, last_error: 'sync failed', stale: true, effective_universe_ready: false, max_age_hours: 24 })
    const exchangeSymbols = vi.spyOn(operationsApi, 'exchangeSymbols').mockImplementation(async (query) => ({
      items: [{ symbol: query?.offset ? 'PAGE2USDT' : 'ORPHANUSDT', pair: 'ORPHANUSDT', contract_type: 'PERPETUAL', status: 'TRADING', onboard_date: null, delivery_date: null, base_asset: 'ORPHAN', quote_asset: 'USDT', margin_asset: 'USDT', underlying_type: null, active: true, synced_at: '2026-08-16T00:00:00Z', global_enabled: true, global_admission_version: 0 }],
      total: 51,
      limit: query?.limit ?? 50,
      offset: query?.offset ?? 0
    }))

    const wrapper = mount(CategoryManagementView)
    await flushPromises()

    expect(wrapper.find('.status-strip').classes()).toContain('error')
    const expandButton = wrapper.get('button.expand-control')
    expect(expandButton.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('.expand-control[role="button"]').exists()).toBe(false)
    await expandButton.trigger('click')
    expect(expandButton.attributes('aria-expanded')).toBe('false')

    const button = wrapper.findAll('button').find((item) => item.text().includes('未分类交易对'))
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()

    expect(exchangeSymbols).toHaveBeenCalledWith({ unclassified: true, limit: 50, offset: 0 })
    expect(wrapper.text()).toContain('ORPHANUSDT')
    expect(wrapper.text()).toContain('NO ACTIVE CATEGORY ASSOCIATION')
    expect(router.currentRoute.value.query.unclassified).toBe('true')

    await wrapper.get('.ant-pagination-item-2').trigger('click')
    await flushPromises()
    expect(exchangeSymbols).toHaveBeenLastCalledWith({ unclassified: true, limit: 50, offset: 50 })
    expect(wrapper.text()).toContain('PAGE2USDT')
    expect(router.currentRoute.value.query.detail_page).toBe('2')
  })

  it('loads every exchange-symbol page before applying local filters', async () => {
    await router.push('/universe?status=TRADING')
    const symbol = (name: string) => ({ symbol: name, pair: name, contract_type: 'PERPETUAL', status: 'TRADING', onboard_date: null, delivery_date: null, base_asset: name, quote_asset: 'USDT', margin_asset: 'USDT', underlying_type: null, active: true, synced_at: '2026-08-16T00:00:00Z', global_enabled: true, global_admission_version: 0 })
    const exchangeSymbols = vi.spyOn(operationsApi, 'exchangeSymbols')
      .mockResolvedValueOnce({ items: [symbol('AUSDT')], total: 2, limit: 1000, offset: 0 })
      .mockResolvedValueOnce({ items: [symbol('BUSDT')], total: 2, limit: 1000, offset: 1 })
    vi.spyOn(operationsApi, 'categoriesPage').mockResolvedValue({ items: [], total: 0, limit: 1000, offset: 0 })
    vi.spyOn(operationsApi, 'symbolSyncStatus').mockResolvedValue({ initialized: true, status: 'SUCCESS', last_attempt_at: null, last_success_at: '2026-08-16T00:00:00Z', synced_symbols: 2, last_error: null, stale: false, effective_universe_ready: true, max_age_hours: 24 })

    const wrapper = mount(UniverseView)
    await flushPromises()

    expect(exchangeSymbols).toHaveBeenNthCalledWith(1, { limit: 1000, offset: 0 })
    expect(exchangeSymbols).toHaveBeenNthCalledWith(2, { limit: 1000, offset: 1 })
    expect(wrapper.text()).toContain('2 / 2 · 已逐页完整载入')
  })

  it('strategy risk renders effective-universe reasons from the backend response', async () => {
    vi.spyOn(operationsApi, 'runtimeStatus').mockResolvedValue({
      items: [{ account_id: 'acct', strategy_id: 'spike-short', instance_id: 'i', mode: 'testnet', status: 'running', effective_status: 'running', entry_enabled: true, halted: false, halt_reason: null, gate_conditions: {}, started_at: '2026-08-16T00:00:00Z', heartbeat_at: '2026-08-16T00:00:00Z', stopped_at: null }], total: 1, limit: 1000, offset: 0
    })
    vi.spyOn(operationsApi, 'categoriesPage').mockResolvedValue({ items: [{ category_key: 'cat', source: 'binance', category_type: 'CATEGORY', code: 'cat', name: 'Category', parent_key: null, active: true, synced_at: '2026-08-16T00:00:00Z', symbol_count: 1 }], total: 1, limit: 1000, offset: 0 })
    vi.spyOn(operationsApi, 'strategyAdmissionsPage').mockResolvedValue({ items: [], total: 0, limit: 1000, offset: 0 })
    const universePreview = vi.spyOn(operationsApi, 'universePreview')
      .mockResolvedValueOnce({ strategy_id: 'spike-short', freeze_days: 15, total_symbols: 2, effective_symbols: 2, excluded_symbols: 0, total: 2, items: [{ symbol: 'BTCUSDT', effective: true, exclusion_reasons: [], blocked_category_keys: [] }], limit: 1000, offset: 0 })
      .mockResolvedValueOnce({ strategy_id: 'spike-short', freeze_days: 15, total_symbols: 2, effective_symbols: 2, excluded_symbols: 0, total: 2, items: [{ symbol: 'ETHUSDT', effective: true, exclusion_reasons: [], blocked_category_keys: [] }], limit: 1000, offset: 1 })
    vi.spyOn(operationsApi, 'strategyAdmissionAudits').mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 })

    const wrapper = mount(StrategyRiskView)
    await flushPromises()

    expect(wrapper.text()).toContain('最终有效交易池')
    expect(wrapper.text()).toContain('BTCUSDT')
    expect(wrapper.text()).toContain('ETHUSDT')
    expect(wrapper.text()).toContain('通过交易所、全局与策略分类门禁')
    expect(universePreview).toHaveBeenNthCalledWith(2, 'spike-short', expect.objectContaining({ limit: 1000, offset: 1 }))
  })
})
