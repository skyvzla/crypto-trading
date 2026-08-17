import type { Page, PageParams } from '@/api/types'

export async function collectPageItems<T>(
  fetchPage: (params: Required<PageParams>) => Promise<Page<T>>,
  pageSize = 1000
): Promise<Page<T>> {
  const items: T[] = []
  let total = 0

  do {
    const page = await fetchPage({ limit: pageSize, offset: items.length })
    total = page.total
    if (!page.items.length && items.length < total) {
      throw new Error(`分页读取在 ${items.length}/${total} 条时未继续返回数据`)
    }
    items.push(...page.items)
  } while (items.length < total)

  return { items, total, limit: pageSize, offset: 0 }
}
