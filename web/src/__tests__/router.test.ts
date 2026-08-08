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
        'stats',
        'symbols',
        'universe',
        'admissions'
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
    expect(router.resolve('/positions').name).toBe('positions')
    expect(router.resolve('/stats').name).toBe('stats')
  })
})
