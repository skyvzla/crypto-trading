import { watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  isQuerySynced,
  useLedgerLoader,
  useQuerySync,
  type LedgerLoaderContext,
  type LedgerLoaderOptions,
  type RouteQueryParts,
} from './useOperationsView'

export interface RouteSyncedLoaderOptions extends Omit<LedgerLoaderOptions, 'onActivate'> {
  /**
   * 本页放进地址栏的内容。写回与「URL 是否被外部改动」共用这一处声明，
   * 所以它必须每次重新计算，不能把结果存下来当快照。
   */
  routeQuery: () => RouteQueryParts
  /** 把地址栏状态同步回本地 ref；重新激活前与地址栏被外部改动后都会调用。 */
  restoreFromRoute: () => void
}

/**
 * 「地址栏是唯一状态源」的运营页面统一装配：加载状态机 + URL 同步。
 *
 * 页面只声明两件事——`routeQuery()`（本页往地址栏里放什么）和
 * `restoreFromRoute()`（怎么把地址栏读回本地 ref）——其余的装配都由这里收口：
 *
 * - 用 `useLedgerLoader` 提供 loading / error / refreshedAt 与并发防串；
 *   页面挂载与 KeepAlive 重新激活时的取数、以及激活前先 `restoreFromRoute()`，
 *   都在其中。
 * - 监听 `route.query`，处理「已经在本页时地址栏被外部改动」：手改 URL、
 *   打开一条带不同筛选的分享链接。这种情况下组件既不重新挂载也不重新激活，
 *   只靠 onActivated 跟不上。
 * - 用 `syncRoute()` 把 `routeQuery()` 写回地址栏，替代各页面各写一遍的
 *   `syncQuery(routeQuery())`。
 *
 * 区分「自己写回的 query」与「地址栏被外部改动」靠比较而不是记账：自己写回的
 * 结果与 `routeQuery()` 一致，因此不会把一次应用筛选变成两次请求。这里的关键是
 * 比较对象必须是**当场重算**的 `routeQuery()`，而不是某次渲染留下的快照。
 *
 * `ownRoute` 在 setup 期取值：路由名变了说明已经切走，被 KeepAlive 缓存的旧实例
 * 不该再响应地址栏。
 */
export function useRouteSyncedLoader(
  load: (context: LedgerLoaderContext) => Promise<void>,
  options: RouteSyncedLoaderOptions,
) {
  const route = useRoute()
  const syncQuery = useQuerySync()
  const { routeQuery, restoreFromRoute } = options

  const { loading, error, errorStatus, refreshedAt, reload } = useLedgerLoader(load, {
    fallbackMessage: options.fallbackMessage,
    loadOnMount: options.loadOnMount,
    reloadOnActivate: options.reloadOnActivate,
    // 重新激活前先把地址栏同步回本地 ref，否则缓存实例会继续显示上次的筛选值。
    onActivate: restoreFromRoute,
  })

  const ownRoute = route.name
  watch(
    () => route.query,
    () => {
      if (route.name !== ownRoute || isQuerySynced(route.query, routeQuery())) return
      restoreFromRoute()
      void reload()
    },
  )

  /** 只把 `routeQuery()` 写回地址栏；要不要紧跟一次取数由页面决定。 */
  async function syncRoute(): Promise<void> {
    await syncQuery(routeQuery())
  }

  return { loading, error, errorStatus, refreshedAt, reload, syncRoute }
}
