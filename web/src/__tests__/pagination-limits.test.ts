import { describe, expect, it, vi } from 'vitest'
import { collectPageItems, DEFAULT_MAX_ITEMS } from '@/shared/pagination'

/** 造一个「要多少有多少」的分页源，用来验证上限本身而不是某个接口的行为。 */
function unlimitedPageSource(total: number) {
  return vi.fn(async ({ limit, offset }: { limit: number; offset: number }) => ({
    items: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, index) => offset + index),
    total,
    limit,
    offset,
  }))
}

describe('collectPageItems 读取上限', () => {
  it('在条数超过 maxItems 时给出点名上限与可用总数的报错', async () => {
    const fetchPage = unlimitedPageSource(100)

    await expect(collectPageItems(fetchPage, 4, { maxItems: 10 })).rejects.toThrow(
      '分页读取超过上限 10 条，已读取 12 条（本次可用 100 条）；请改用服务端分页，或显式调大 maxItems。',
    )
  })

  it('在页数超过 maxPages 时给出点名页数上限的报错', async () => {
    const fetchPage = unlimitedPageSource(10)

    await expect(collectPageItems(fetchPage, 1, { maxItems: 100, maxPages: 2 })).rejects.toThrow(
      '分页读取超过上限 2 页，已读取 2 条（本次可用 10 条）；请改用服务端分页。',
    )
  })

  it('上限内的整段读取仍然逐页读完', async () => {
    const fetchPage = unlimitedPageSource(5)

    const result = await collectPageItems(fetchPage, 2, { maxItems: 10 })

    expect(result.items).toEqual([0, 1, 2, 3, 4])
    expect(result.total).toBe(5)
    expect(fetchPage).toHaveBeenCalledTimes(3)
    expect(fetchPage).toHaveBeenLastCalledWith({ limit: 2, offset: 4 })
  })

  it('默认上限足够高，配置类列表不会被误伤', () => {
    expect(DEFAULT_MAX_ITEMS).toBeGreaterThanOrEqual(10_000)
  })

  it('既不越过上限也不吞掉「服务端提前停止」的守卫', async () => {
    const fetchPage = vi.fn(async ({ limit, offset }: { limit: number; offset: number }) => ({
      items: offset === 0 ? [0] : [],
      total: 4,
      limit,
      offset,
    }))

    await expect(collectPageItems(fetchPage, 1)).rejects.toThrow('分页读取在 1/4 条时未继续返回数据')
  })
})
