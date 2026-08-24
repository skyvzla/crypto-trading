import { computed, reactive, ref, type Ref } from 'vue'
import { notificationApi } from '@/api/client'
import type { NotificationDelivery, NotificationEvent, Page, PageParams } from '@/api/types'
import { errorMessage } from './useVersionedCollection'

/** 事件与投递列表每页条数。 */
const ACTIVITY_PAGE_SIZE = 8

export interface ActivityDeps {
  /** 展示加载错误；传空串表示清除上一次的提示。 */
  setError: (message: string) => void
}

/**
 * 事件流与投递队列。
 *
 * 这两张表共用一套筛选 + 分页交互，但查的是不同资源，
 * 所以各自持有自己的 Page 状态、自己的 loading 和自己的请求序号。
 */
export function useNotificationActivity(deps: ActivityDeps) {
  const emptyPage = <T>(): Page<T> => ({ items: [], total: 0, limit: ACTIVITY_PAGE_SIZE, offset: 0 })
  const events = ref<Page<NotificationEvent>>(emptyPage<NotificationEvent>())
  const deliveries = ref<Page<NotificationDelivery>>(emptyPage<NotificationDelivery>())
  const eventFilters = reactive({ event_type: '', severity: '', routing_status: '' })
  const deliveryFilters = reactive({ status: '', endpoint_id: '', event_id: '' })
  const eventsLoading = ref(false)
  const deliveriesLoading = ref(false)
  /** 任一张表在加载都算活动区在加载，供顶部刷新按钮显示。 */
  const activityLoading = computed(() => eventsLoading.value || deliveriesLoading.value)

  /**
   * 一张表的加载通道。
   *
   * 每次加载领一个序号，写回前确认自己仍是最新一次：连续翻页或反复改筛选会
   * 并发多个请求，先发的可能后到，不判断就会把旧条件的结果盖到新条件上。
   *
   * loading 也必须按表分开——两张表共用一个 ref 时，先返回的那个请求会把另一个
   * 仍在飞行中的请求的 loading 关掉，表格看起来加载完了其实还在等。
   */
  function createChannel<T>(
    target: Ref<Page<T>>,
    loading: Ref<boolean>,
    fetch: (paging: PageParams) => Promise<Page<T>>,
    fallbackMessage: string
  ) {
    let sequence = 0
    return async function load(offset = target.value.offset): Promise<void> {
      const current = ++sequence
      // 乐观推进 offset，分页器立刻跟手；权威值由响应写回。
      if (offset !== target.value.offset) target.value = { ...target.value, offset }
      loading.value = true
      try {
        const page = await fetch({ limit: target.value.limit, offset })
        if (current !== sequence) return
        target.value = page
        deps.setError('')
      } catch (error) {
        if (current !== sequence) return
        deps.setError(errorMessage(error, fallbackMessage))
      } finally {
        if (current === sequence) loading.value = false
      }
    }
  }

  const loadEvents = createChannel(
    events,
    eventsLoading,
    (paging) => notificationApi.events({ ...paging, ...eventFilters }),
    '通知事件加载失败'
  )
  const loadDeliveries = createChannel(
    deliveries,
    deliveriesLoading,
    (paging) => notificationApi.deliveries({ ...paging, ...deliveryFilters }),
    '通知投递加载失败'
  )

  function changeEventPage(page: number) {
    void loadEvents((page - 1) * events.value.limit)
  }

  function changeDeliveryPage(page: number) {
    void loadDeliveries((page - 1) * deliveries.value.limit)
  }

  /** 把测试通知产生的事件与投递插到列表顶部，省掉一次手动刷新。 */
  function prependTestResult(event: NotificationEvent | null, incoming: NotificationDelivery[]) {
    if (event) {
      const items = [event, ...events.value.items.filter((row) => row.id !== event.id)]
      events.value = { ...events.value, items, total: Math.max(events.value.total, items.length) }
    }
    if (incoming.length) {
      const byId = new Map(incoming.map((row) => [row.id, row]))
      const items = [...incoming, ...deliveries.value.items.filter((row) => !byId.has(row.id))]
      deliveries.value = { ...deliveries.value, items, total: Math.max(deliveries.value.total, items.length) }
    }
  }

  function replaceDelivery(updated: NotificationDelivery) {
    deliveries.value = {
      ...deliveries.value,
      items: deliveries.value.items.map((row) => (row.id === updated.id ? updated : row))
    }
  }

  return {
    events,
    deliveries,
    eventFilters,
    deliveryFilters,
    eventsLoading,
    deliveriesLoading,
    activityLoading,
    loadEvents,
    loadDeliveries,
    changeEventPage,
    changeDeliveryPage,
    prependTestResult,
    replaceDelivery,
    pageSize: ACTIVITY_PAGE_SIZE
  }
}
