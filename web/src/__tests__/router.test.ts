import { describe, it, expect } from 'vitest'
import { router } from '@/router'

describe('router', () => {
  const names = router
    .getRoutes()
    .filter((r) => r.name)
    .map((r) => r.name)

  it('registers all planned views', () => {
    expect(names).toEqual(
      expect.arrayContaining([
        'overview',
        'calendar',
        'positions',
        'trades',
        'performance',
        'stats',
        'symbols',
        'universe',
        'admissions',
        'strategy-risk',
        'categories',
        'notifications',
        'backtests',
        'backtest-reports',
        'backtest-report-detail',
        'backtest-symbols',
        'backtest-symbol-trades',
        'backtest-equity-replay',
        'backtest-trade-replay'
      ])
    )
  })

  // resolve() 不跟随 redirect，重定向只能靠真实导航验证。
  it('redirects root to overview', async () => {
    await router.push('/')
    await router.isReady()
    expect(router.currentRoute.value.name).toBe('overview')
  })

  it('catches unknown paths and redirects to overview', async () => {
    await router.push('/random/garbage')
    await router.isReady()
    expect(router.currentRoute.value.name).toBe('overview')
  })

  it('resolves deep links correctly', () => {
    expect(router.resolve('/backtests').href).toBe('#/backtests')
    expect(router.resolve('/positions').name).toBe('positions')
    expect(router.resolve('/stats').name).toBe('stats')
    expect(router.resolve('/performance').name).toBe('performance')
    expect(router.resolve('/strategy-risk').name).toBe('strategy-risk')
    expect(router.resolve('/categories').name).toBe('categories')
    expect(router.resolve('/notifications').name).toBe('notifications')
    expect(router.resolve('/backtests').name).toBe('backtests')
    expect(router.resolve('/backtests/r-1/reports').name).toBe('backtest-reports')
    expect(router.resolve('/backtests/r-1/reports/pnl_bucket').name).toBe('backtest-report-detail')
    expect(router.resolve('/backtests/r-1/symbols').name).toBe('backtest-symbols')
    expect(router.resolve('/backtests/r-1/symbols/AKEUSDT/trades').name).toBe('backtest-symbol-trades')
    expect(router.resolve('/backtests/r-1/equity').name).toBe('backtest-equity-replay')
    expect(router.resolve('/backtests/r-1/trades/t-1').name).toBe('backtest-trade-replay')
  })
})
