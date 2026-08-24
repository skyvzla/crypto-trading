import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { NotificationActivityKey, NotificationViewKey } from './types'

const VIEW_ROUTE_NAMES: Record<NotificationViewKey, string> = {
  overview: 'notifications',
  connectors: 'notifications-connectors',
  groups: 'notifications-groups',
  policies: 'notifications-policies',
  activity: 'notifications-activity'
}

/**
 * 通知中心的分区导航。
 *
 * 用 useRoute/useRouter 而不是直接 import router 单例：组件在测试或其他
 * 挂载点下也能工作，也不会在 router 尚未就绪时读到空路由
 * （之前直接用单例会触发 vue-router 的 R0020 告警）。
 */
export function useNotificationNavigation() {
  const route = useRoute()
  const router = useRouter()

  const view = computed<NotificationViewKey>({
    get: () => {
      const current = String(route.name ?? '')
      const matched = Object.entries(VIEW_ROUTE_NAMES).find(([, name]) => name === current)
      return (matched?.[0] as NotificationViewKey) ?? 'overview'
    },
    set: (next) => {
      if (String(route.name ?? '') === VIEW_ROUTE_NAMES[next]) return
      void router.push({ name: VIEW_ROUTE_NAMES[next] })
    }
  })

  const activityView = computed<NotificationActivityKey>({
    get: () => (route.query.tab === 'deliveries' ? 'deliveries' : 'events'),
    set: (next) => {
      void router.push({ name: VIEW_ROUTE_NAMES.activity, query: { ...route.query, tab: next } })
    }
  })

  /** 从概览直接跳到事件或投递队列。 */
  function openActivity(next: NotificationActivityKey) {
    void router.push({ name: VIEW_ROUTE_NAMES.activity, query: { tab: next } })
  }

  return { view, activityView, openActivity }
}
