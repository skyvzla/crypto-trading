import { reactive, ref } from 'vue'
import { notificationApi } from '@/api/client'
import type { NotificationDelivery, NotificationEvent, Page } from '@/api/types'
import { errorMessage } from './useVersionedCollection'

/** 事件与投递列表每页条数。 */
const ACTIVITY_PAGE_SIZE = 8

export interface ActivityDeps {
  reportError: (message: string) => void
}

/**
 * 事件流与投递队列。
 *
 * 这两张表共用一套筛选 + 分页交互，但查的是不同资源，
 * 所以各自持有自己的 Page 状态。
 */
export function useNotificationActivity(deps: ActivityDeps) {
  const emptyPage = <T>(): Page<T> => ({ items: [], total: 0, limit: ACTIVITY_PAGE_SIZE, offset: 0 })
  const events = ref<Page<NotificationEvent>>(emptyPage<NotificationEvent>())
  const deliveries = ref<Page<NotificationDelivery>>(emptyPage<NotificationDelivery>())
  const eventFilters = reactive({ event_type: '', severity: '', routing_status: '' })
  const deliveryFilters = reactive({ status: '', endpoint_id: '', event_id: '' })
  const activityLoading = ref(false)

  async function loadEvents() {
    activityLoading.value = true
    try {
      events.value = await notificationApi.events({
        limit: events.value.limit,
        offset: events.value.offset,
        ...eventFilters
      })
    } catch (error) {
      deps.reportError(errorMessage(error, '通知事件加载失败'))
    } finally {
      activityLoading.value = false
    }
  }

  async function loadDeliveries() {
    activityLoading.value = true
    try {
      deliveries.value = await notificationApi.deliveries({
        limit: deliveries.value.limit,
        offset: deliveries.value.offset,
        ...deliveryFilters
      })
    } catch (error) {
      deps.reportError(errorMessage(error, '通知投递加载失败'))
    } finally {
      activityLoading.value = false
    }
  }

  function changeEventPage(page: number) {
    events.value = { ...events.value, offset: (page - 1) * events.value.limit }
    void loadEvents()
  }

  function changeDeliveryPage(page: number) {
    deliveries.value = { ...deliveries.value, offset: (page - 1) * deliveries.value.limit }
    void loadDeliveries()
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
