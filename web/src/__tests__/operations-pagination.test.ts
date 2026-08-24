import { describe, expect, it, vi } from 'vitest'
import { collectPageItems } from '@/shared/pagination'

describe('operations pagination', () => {
  it('continues loading until the server total is complete', async () => {
    const fetchPage = vi.fn(async ({ limit, offset }: { limit: number; offset: number }) => ({
      items: offset === 0 ? ['A', 'B'] : ['C'],
      total: 3,
      limit,
      offset
    }))

    const result = await collectPageItems(fetchPage, 2)

    expect(fetchPage).toHaveBeenNthCalledWith(1, { limit: 2, offset: 0 })
    expect(fetchPage).toHaveBeenNthCalledWith(2, { limit: 2, offset: 2 })
    expect(result.items).toEqual(['A', 'B', 'C'])
  })

  it('fails visibly when a server page stops before its declared total', async () => {
    const fetchPage = vi.fn(async ({ limit, offset }: { limit: number; offset: number }) => ({
      items: offset === 0 ? ['A'] : [],
      total: 2,
      limit,
      offset
    }))

    await expect(collectPageItems(fetchPage, 1)).rejects.toThrow('分页读取在 1/2 条时未继续返回数据')
  })
})
