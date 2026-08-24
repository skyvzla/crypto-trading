import { computed, onActivated, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { LocationQuery } from 'vue-router'
import { formatLedgerClock } from '@/shared/time'

export interface OperationFilters {
  account_id: string
  strategy_id: string
  symbol: string
}

/** 只保留填写过的字段，空串不进 URL 也不进请求。 */
export type LedgerFilterQuery = Partial<Record<keyof OperationFilters, string>>

function readFilters(query: LocationQuery): OperationFilters {
  return {
    account_id: String(query.account_id ?? ''),
    strategy_id: String(query.strategy_id ?? ''),
    symbol: String(query.symbol ?? '')
  }
}

function toFilterQuery(filters: OperationFilters): LedgerFilterQuery {
  const result: LedgerFilterQuery = {}
  for (const key of ['account_id', 'strategy_id', 'symbol'] as const) {
    const value = filters[key].trim()
    if (value) result[key] = value
  }
  return result
}

/**
 * 账户 / 策略 / 交易对筛选条件，以 URL 为唯一状态源。
 *
 * URL 而不是 store 承载筛选状态：链接可分享、可收藏、可后退，
 * 页面之间跳转时把 query 带过去就完成了「共享筛选」。
 *
 * 返回的 `restore()` 供被 KeepAlive 缓存的页面在重新激活时调用，
 * 否则缓存实例会继续显示上次的筛选值，与地址栏不一致。
 */
export function useOperationFilters() {
  const route = useRoute()
  const filters = ref<OperationFilters>(readFilters(route.query))
  const query = computed(() => toFilterQuery(filters.value))

  function restore() {
    filters.value = readFilters(route.query)
  }

  return { filters, query, restore }
}

export interface LedgerLoaderContext {
  /**
   * 是否已被更晚的一次加载取代。
   *
   * 快速切换筛选或分页时会并发多个请求，先发的可能后到；
   * 写回数据前必须判断，否则界面会显示上一次条件的结果。
   */
  isStale: () => boolean
}

export interface LedgerLoaderOptions {
  /** 抛出的异常不是 Error 时展示的兜底文案。 */
  fallbackMessage: string
  /**
   * 是否在挂载时自动加载，默认 true。
   * 详情页由路由参数驱动，自己 watch 更准确，可设为 false 避免重复请求。
   */
  loadOnMount?: boolean
  /** 页面重新激活（KeepAlive）时是否重新加载，默认 true。 */
  reloadOnActivate?: boolean
  /** 重新激活前先把 URL 状态同步回本地 ref。 */
  onActivate?: () => void
}

/**
 * 运营页统一的加载状态机：loading / error / refreshedAt + 并发防串。
 *
 * 同时收口 KeepAlive 的重新激活语义——这些页面被 `<KeepAlive>` 缓存，
 * 只挂载一次，靠 onActivated 才能在返回时与地址栏重新对齐。
 */
export function useLedgerLoader(
  load: (context: LedgerLoaderContext) => Promise<void>,
  options: LedgerLoaderOptions
) {
  const loading = ref(false)
  const error = ref<string | null>(null)
  const refreshedAt = ref<string | null>(null)
  let sequence = 0
  let activated = false

  async function run(): Promise<void> {
    const current = ++sequence
    const isStale = () => current !== sequence
    loading.value = true
    error.value = null
    try {
      await load({ isStale })
      if (isStale()) return
      refreshedAt.value = formatLedgerClock()
    } catch (caught) {
      if (isStale()) return
      error.value = caught instanceof Error ? caught.message : options.fallbackMessage
    } finally {
      if (!isStale()) loading.value = false
    }
  }

  onMounted(() => {
    if (options.loadOnMount === false) return
    void run()
  })
  onActivated(() => {
    // onMounted 与 onActivated 在首次进入时都会触发，跳过第一次避免重复请求。
    if (!activated) {
      activated = true
      return
    }
    if (options.reloadOnActivate === false) return
    options.onActivate?.()
    void run()
  })

  return { loading, error, refreshedAt, reload: run }
}

/** 把不可信的 URL 数值收敛成正整数。 */
export function positiveInt(value: unknown, fallback: number, max = Number.MAX_SAFE_INTEGER): number {
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? Math.min(parsed, max) : fallback
}

export interface PageParamsOptions {
  defaultSize: number
  maxSize?: number
}

/**
 * 分页状态。页码与每页条数都从 URL 读，越界值一律收敛而不是原样透传给后端。
 */
export function usePageParams({ defaultSize, maxSize = 1000 }: PageParamsOptions) {
  const route = useRoute()
  const page = ref(positiveInt(route.query.page, 1))
  const pageSize = ref(positiveInt(route.query.page_size, defaultSize, maxSize))
  const offset = computed(() => (page.value - 1) * pageSize.value)

  function restore() {
    page.value = positiveInt(route.query.page, 1)
    pageSize.value = positiveInt(route.query.page_size, defaultSize, maxSize)
  }

  /** 表格分页变化：换每页条数时回到第一页，否则跳到目标页。 */
  function apply(next: { current?: number; pageSize?: number }) {
    const nextSize = positiveInt(next.pageSize, pageSize.value, maxSize)
    page.value = nextSize === pageSize.value ? positiveInt(next.current, page.value) : 1
    pageSize.value = nextSize
  }

  return { page, pageSize, offset, restore, apply }
}

/** 写回地址栏的归一化：空串与 undefined 都不进 URL。 */
function toQuery(parts: Record<string, string | number | undefined>): Record<string, string> {
  const query: Record<string, string> = {}
  for (const [key, value] of Object.entries(parts)) {
    if (value === undefined || value === '') continue
    query[key] = String(value)
  }
  return query
}

/** 把筛选、分页与页面自定义参数合并写回地址栏。 */
export function useQuerySync() {
  const router = useRouter()
  return async function syncQuery(parts: Record<string, string | number | undefined>): Promise<void> {
    await router.replace({ query: toQuery(parts) })
  }
}

/**
 * 地址栏是否已经是页面状态该写出来的样子。
 *
 * 页面用它区分两种 query 变化：一致 = 自己刚写回的（紧接着会自己 reload，
 * 不该再取一次数），不一致 = 地址栏被外部改了（手改 URL、打开分享链接）。
 * 这样就不需要记录「这次是谁写的」。
 *
 * 只做无状态比较，页面放哪些参数进 URL 由页面自己声明。
 */
export function isQuerySynced(
  current: LocationQuery,
  parts: Record<string, string | number | undefined>
): boolean {
  const expected = toQuery(parts)
  for (const key of new Set([...Object.keys(expected), ...Object.keys(current)])) {
    const raw = current[key]
    const actual = Array.isArray(raw) ? raw[0] : raw
    if ((actual == null ? '' : String(actual)) !== (expected[key] ?? '')) return false
  }
  return true
}
