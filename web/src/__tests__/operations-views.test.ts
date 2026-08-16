import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { router } from '@/router'
import { operationsApi } from '@/api/operations'
import OverviewView from '@/views/OverviewView.vue'
import CategoryManagementView from '@/views/CategoryManagementView.vue'
import PerformanceView from '@/views/PerformanceView.vue'
import StrategyRiskView from '@/views/StrategyRiskView.vue'

beforeEach(async () => {
  vi.restoreAllMocks()
  await router.push('/overview')
  await router.isReady()
})

describe('operations views', () => {
  it('overview requires an explicit account before showing PnL', async () => {
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

    const wrapper = mount(OverviewView)
    await flushPromises()

    expect(wrapper.text()).toContain('填写账户后显示收益')
    expect(wrapper.text()).toContain('选择账户后读取本月数据')
    expect(operationsApi.pnl).not.toHaveBeenCalled()
  })

  it('performance keeps the account boundary visible instead of mixing accounts', () => {
    const wrapper = mount(PerformanceView)
    expect(wrapper.text()).toContain('请输入账户 ID')
    expect(wrapper.text()).toContain('不会跨账户混算')
  })

  it('loads unclassified symbols from the backend and marks a failed sync', async () => {
    vi.spyOn(operationsApi, 'categories').mockResolvedValue([
      { category_key: 'cat', source: 'binance', category_type: 'CATEGORY', code: 'cat', name: 'Category', parent_key: null, active: true, synced_at: '2026-08-16T00:00:00Z', symbol_count: 1 }
    ])
    vi.spyOn(operationsApi, 'symbolSyncStatus').mockResolvedValue({ initialized: true, status: 'FAILED', last_attempt_at: '2026-08-16T00:00:00Z', last_success_at: '2026-08-15T00:00:00Z', synced_symbols: 1, last_error: 'sync failed', stale: true, effective_universe_ready: false, max_age_hours: 24 })
    const exchangeSymbols = vi.spyOn(operationsApi, 'exchangeSymbols').mockResolvedValue({
      items: [{ symbol: 'ORPHANUSDT', pair: 'ORPHANUSDT', contract_type: 'PERPETUAL', status: 'TRADING', onboard_date: null, delivery_date: null, base_asset: 'ORPHAN', quote_asset: 'USDT', margin_asset: 'USDT', underlying_type: null, active: true, synced_at: '2026-08-16T00:00:00Z', global_enabled: true, global_admission_version: 0 }],
      total: 1,
      limit: 1000,
      offset: 0
    })

    const wrapper = mount(CategoryManagementView)
    await flushPromises()

    expect(wrapper.find('.status-strip').classes()).toContain('error')
    const button = wrapper.findAll('button').find((item) => item.text().includes('未分类交易对'))
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()

    expect(exchangeSymbols).toHaveBeenCalledWith({ unclassified: true, limit: 1000 })
    expect(wrapper.text()).toContain('ORPHANUSDT')
    expect(wrapper.text()).toContain('NO ACTIVE CATEGORY ASSOCIATION')
  })

  it('strategy risk renders effective-universe reasons from the backend response', async () => {
    vi.spyOn(operationsApi, 'runtimeStatus').mockResolvedValue({
      items: [{ account_id: 'acct', strategy_id: 'spike-short', instance_id: 'i', mode: 'testnet', status: 'running', effective_status: 'running', entry_enabled: true, halted: false, halt_reason: null, gate_conditions: {}, started_at: '2026-08-16T00:00:00Z', heartbeat_at: '2026-08-16T00:00:00Z', stopped_at: null }], total: 1, limit: 1000, offset: 0
    })
    vi.spyOn(operationsApi, 'categories').mockResolvedValue([{ category_key: 'cat', source: 'binance', category_type: 'CATEGORY', code: 'cat', name: 'Category', parent_key: null, active: true, synced_at: '2026-08-16T00:00:00Z', symbol_count: 1 }])
    vi.spyOn(operationsApi, 'strategyAdmissions').mockResolvedValue([])
    vi.spyOn(operationsApi, 'universePreview').mockResolvedValue({ strategy_id: 'spike-short', freeze_days: 15, total_symbols: 1, effective_symbols: 1, excluded_symbols: 0, items: [{ symbol: 'BTCUSDT', effective: true, exclusion_reasons: [], blocked_category_keys: [] }], limit: 1000, offset: 0 })
    vi.spyOn(operationsApi, 'strategyAdmissionAudits').mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 })

    const wrapper = mount(StrategyRiskView)
    await flushPromises()

    expect(wrapper.text()).toContain('最终有效交易池')
    expect(wrapper.text()).toContain('BTCUSDT')
    expect(wrapper.text()).toContain('通过交易所、全局与策略分类门禁')
  })
})
