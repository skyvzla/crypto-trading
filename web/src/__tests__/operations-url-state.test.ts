import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { router } from '@/router'
import { operationsApi } from '@/api/operations'
import App from '@/App.vue'
import OverviewView from '@/views/OverviewView.vue'
import FilterBar from '@/features/operations/FilterBar.vue'

/**
 * 单独成文件是必需的：这里断言的是「一次交互引发几次请求」，
 * 而同一文件里没有 unmount 的历史实例也会响应路由变化，
 * 混在一起数出来的次数没有意义。
 *
 * 同理，每个用例都先取完计数再 unmount，最后才断言——断言失败会中断用例，
 * 写在 unmount 之前就会把组件泄漏给后面的用例。
 */

beforeEach(async () => {
  vi.restoreAllMocks()
  vi.spyOn(operationsApi, 'accounts').mockResolvedValue({ items: [], total: 0, limit: 1000, offset: 0 })
  await router.push('/overview')
  await router.isReady()
})

function stubOverview() {
  vi.spyOn(operationsApi, 'health').mockResolvedValue({
    status: 'healthy',
    service: 'ledger',
    timestamp: '2026-08-16T00:00:00Z',
  })
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
    avg_loss: '0',
  })
  vi.spyOn(operationsApi, 'orders').mockResolvedValue({ items: [], total: 0, limit: 1, offset: 0 })
  vi.spyOn(operationsApi, 'trades').mockResolvedValue({ items: [], total: 0, limit: 6, offset: 0 })
  vi.spyOn(operationsApi, 'dailyPnl').mockResolvedValue([])
  return vi.spyOn(operationsApi, 'runtimeStatus').mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 })
}

/**
 * 持仓与订单页的取数调用。
 *
 * 运行总览也会调 positions（只取 total，limit 1），所以必须按每页条数区分，
 * 否则数到的是两个页面的和。
 */
function positionPageCalls(spy: { mock: { calls: unknown[][] } }) {
  return spy.mock.calls.filter((call) => (call[0] as { limit?: number } | undefined)?.limit === 25).length
}

describe('筛选写回地址栏', () => {
  it('已经在本页时地址栏被外部改动也会重新对齐并取数', async () => {
    stubOverview()
    const positions = vi
      .spyOn(operationsApi, 'positions')
      .mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })

    await router.push('/positions?account_id=first')
    const wrapper = mount(App)
    await flushPromises()
    const callsAtFirst = positionPageCalls(positions)
    const firstParams = positions.mock.calls.at(-1)?.[0]

    // 手改地址栏、或打开一条带不同筛选的分享链接：同路由只变 query，
    // 组件既不重新挂载也不重新 activate。
    await router.push('/positions?account_id=second')
    await flushPromises()
    const callsAfterEdit = positionPageCalls(positions)
    const secondParams = positions.mock.calls.at(-1)?.[0]
    wrapper.unmount()

    expect(firstParams).toMatchObject({ account_id: 'first' })
    expect(secondParams).toMatchObject({ account_id: 'second' })
    expect(callsAfterEdit).toBe(callsAtFirst + 1)
  })

  it('切走之后被缓存的实例不再响应地址栏', async () => {
    const runtimeStatus = stubOverview()
    const positions = vi
      .spyOn(operationsApi, 'positions')
      .mockResolvedValue({ items: [], total: 0, limit: 25, offset: 0 })

    await router.push('/positions?account_id=first')
    const wrapper = mount(App)
    await flushPromises()
    const callsOnPositions = positionPageCalls(positions)

    // 离场时 route.query 同样会变。持仓页此时已被 KeepAlive 缓存，
    // 不能把这次变化当成自己的筛选变了而在后台白跑一次请求。
    await router.push('/overview?account_id=third')
    await flushPromises()
    const callsAfterLeaving = positionPageCalls(positions)
    const overviewParams = runtimeStatus.mock.calls.at(-1)?.[0]
    wrapper.unmount()

    expect(overviewParams).toMatchObject({ account_id: 'third' })
    expect(callsAfterLeaving).toBe(callsOnPositions)
  })

  it('一次应用筛选只打一次请求', async () => {
    const runtimeStatus = stubOverview()
    vi.spyOn(operationsApi, 'positions').mockResolvedValue({ items: [], total: 0, limit: 1, offset: 0 })

    const wrapper = mount(OverviewView)
    await flushPromises()
    const callsAtMount = runtimeStatus.mock.calls.length

    // applyFilters = 写回 URL + 一次显式 reload。写回本身不该再触发一次加载，
    // 否则每次点「筛选」都是两个请求。
    const filterBar = wrapper.findComponent(FilterBar)
    filterBar.vm.$emit('update:modelValue', { account_id: 'acct-7', strategy_id: '', symbol: '' })
    filterBar.vm.$emit('apply')
    await flushPromises()
    const callsAfterApply = runtimeStatus.mock.calls.length
    const params = runtimeStatus.mock.calls.at(-1)?.[0]
    wrapper.unmount()

    expect(params).toMatchObject({ account_id: 'acct-7' })
    expect(callsAfterApply).toBe(callsAtMount + 1)
  })
})
