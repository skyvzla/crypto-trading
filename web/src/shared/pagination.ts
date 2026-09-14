import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import type { LocationQuery } from 'vue-router'
import type { Page, PageParams } from '@/api/types'

export interface UrlPaginationOptions {
  defaultSize: number
  maxSize?: number
  pageKey?: string
  pageSizeKey?: string
}

export interface UrlPaginationApply {
  current?: number
  pageSize?: number
}

/** 把不可信的 URL 数值收敛成正整数，并限制后端允许的每页条数。 */
export function positiveInt(value: unknown, fallback: number, max = Number.MAX_SAFE_INTEGER): number {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? Math.min(parsed, max) : fallback
}

/**
 * 统一的分页状态读取器。
 *
 * 页面负责把返回的状态合并进自己的 query；这个 composable 只负责读取、恢复和
 * 计算 offset，避免分页本身与筛选条件各自写一遍地址栏。
 */
export function useUrlPagination({
  defaultSize,
  maxSize = 1000,
  pageKey = 'page',
  pageSizeKey = 'page_size',
}: UrlPaginationOptions) {
  const route = useRoute()
  const page = ref(positiveInt(route.query[pageKey], 1))
  const pageSize = ref(positiveInt(route.query[pageSizeKey], defaultSize, maxSize))
  const offset = computed(() => (page.value - 1) * pageSize.value)

  function read(query: LocationQuery = route.query) {
    return {
      page: positiveInt(query[pageKey], 1),
      pageSize: positiveInt(query[pageSizeKey], defaultSize, maxSize),
    }
  }

  function restore(query: LocationQuery = route.query) {
    const next = read(query)
    page.value = next.page
    pageSize.value = next.pageSize
  }

  /** 换每页条数时回到第一页，否则跳到目标页。 */
  function apply(next: UrlPaginationApply) {
    const nextSize = positiveInt(next.pageSize, pageSize.value, maxSize)
    page.value = nextSize === pageSize.value ? positiveInt(next.current, page.value) : 1
    pageSize.value = nextSize
  }

  const paginationQuery = computed(() => ({
    [pageKey]: page.value,
    [pageSizeKey]: pageSize.value,
  }))

  return { page, pageSize, offset, paginationQuery, restore, apply }
}

export async function collectPageItems<T>(
  fetchPage: (params: Required<PageParams>) => Promise<Page<T>>,
  pageSize = 1000,
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
