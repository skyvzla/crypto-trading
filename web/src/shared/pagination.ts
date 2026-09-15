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

export interface CollectPageItemsOptions {
  /** 逐页累加的条数上限；越界即抛错，避免把整张长表一次性读进内存。 */
  maxItems?: number
  /** 请求页数上限；正常情况下先被 maxItems 拦住，这里只作为兜底。 */
  maxPages?: number
}

/**
 * 默认条数上限。
 *
 * 配置类列表（账户、交易对、通知目标）远小于这个量级；持续增长的事实表
 * （成交、事件）必须走服务端分页，不该靠逐页取全。
 */
export const DEFAULT_MAX_ITEMS = 50_000

/** 默认页数上限。 */
export const DEFAULT_MAX_PAGES = 200

/** 越界文案要点名上限、可用总数和已读条数，否则页面只能显示一句无从下手的报错。 */
function overflowMessage(maxItems: number, loaded: number, total: number): string {
  return `分页读取超过上限 ${maxItems} 条，已读取 ${loaded} 条（本次可用 ${total} 条）；请改用服务端分页，或显式调大 maxItems。`
}

/**
 * 逐页取全一个列表。
 *
 * 只适用于天然有界、且必须整份载入才能完成本地筛选的配置类列表；
 * 有增长可能的事实表请改用服务端分页（limit / offset + a-pagination）。
 */
export async function collectPageItems<T>(
  fetchPage: (params: Required<PageParams>) => Promise<Page<T>>,
  pageSize = 1000,
  { maxItems = DEFAULT_MAX_ITEMS, maxPages = DEFAULT_MAX_PAGES }: CollectPageItemsOptions = {},
): Promise<Page<T>> {
  const items: T[] = []
  let total = 0
  let pages = 0

  do {
    if (items.length >= maxItems) {
      throw new Error(overflowMessage(maxItems, items.length, total))
    }
    if (pages >= maxPages) {
      throw new Error(
        `分页读取超过上限 ${maxPages} 页，已读取 ${items.length} 条（本次可用 ${total} 条）；请改用服务端分页。`,
      )
    }
    const page = await fetchPage({ limit: pageSize, offset: items.length })
    pages += 1
    total = page.total
    if (!page.items.length && items.length < total) {
      throw new Error(`分页读取在 ${items.length}/${total} 条时未继续返回数据`)
    }
    items.push(...page.items)
    if (items.length > maxItems) {
      throw new Error(overflowMessage(maxItems, items.length, total))
    }
  } while (items.length < total)

  return { items, total, limit: pageSize, offset: 0 }
}
