import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { notificationApi } from '@/api/client'
import type { NotificationDelivery, NotificationEvent, Page } from '@/api/types'
import App from '@/App.vue'
import NotificationsView from '@/views/NotificationsView.vue'
import { useNotificationActivity } from '@/features/notifications/useNotificationActivity'
import { router } from '@/router'
import { jsonResponse } from './httpMocks'

function response(body: unknown) {
  return jsonResponse(body)
}

function notificationResponse(url: string) {
  if (url.endsWith('/notifications/overview')) {
    return response({
      connectors: 1,
      enabled_connectors: 1,
      endpoints: 1,
      enabled_endpoints: 1,
      groups: 1,
      policies: 1,
      events: 1,
      recent_events: 1,
      deliveries: { pending: 0, sending: 0, retry: 0, sent: 2, dead: 0 }
    })
  }
  if (url.includes('/notifications/connectors')) {
    return response({ items: [{ id: 'c-1', name: 'ops-bot', type: 'telegram', secret_ref: 'TG_TOKEN', config: {}, enabled: true, version: 1 }], total: 1, limit: 1000, offset: 0 })
  }
  if (url.includes('/notifications/endpoints')) {
    return response({ items: [{ id: 'e-1', connector_id: 'c-1', name: 'ops-room', address: '-1001', config: {}, enabled: true, version: 1 }], total: 1, limit: 1000, offset: 0 })
  }
  if (url.includes('/notifications/groups')) return response({ items: [], total: 0, limit: 1000, offset: 0 })
  if (url.includes('/notifications/policies')) return response({ items: [], total: 0, limit: 1000, offset: 0 })
  if (url.includes('/notifications/events')) return response({ items: [], total: 0, limit: 8, offset: 0 })
  if (url.includes('/notifications/deliveries')) return response({ items: [], total: 0, limit: 8, offset: 0 })
  return response({})
}

beforeEach(async () => {
  vi.restoreAllMocks()
  await router.push('/notifications')
  await router.isReady()
})

describe('notification API', () => {
  it('posts connector configuration to the versioned endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ id: 'c-1' }))
    await notificationApi.createConnector({ name: 'ops', type: 'telegram', secret_ref: 'TG_TOKEN', enabled: true })
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/notifications/connectors',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'ops', type: 'telegram', secret_ref: 'TG_TOKEN', enabled: true }) })
    )
  })

  it('builds test and retry notification operations', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ ok: true }))
    await notificationApi.testEndpoint('endpoint-1')
    await notificationApi.retryDelivery('delivery-1')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/notifications/endpoints/endpoint-1/test')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/notifications/deliveries/delivery-1/retry')
  })
})

describe('notification route and view', () => {
  it('resolves the notification route', () => {
    expect(router.resolve('/notifications').name).toBe('notifications')
    expect(router.resolve('/notifications/connectors').name).toBe('notifications-connectors')
    expect(router.resolve('/notifications/groups').name).toBe('notifications-groups')
    expect(router.resolve('/notifications/policies').name).toBe('notifications-policies')
    expect(router.resolve('/notifications/activity?tab=deliveries').name).toBe('notifications-activity')
  })

  it('uses the shared operations page sizing without a private max width', () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => notificationResponse(String(input)))
    const wrapper = mount(NotificationsView)
    const page = wrapper.get('main')

    expect(page.classes()).toContain('operations-page')
    expect(getComputedStyle(page.element).maxWidth || 'none').toBe('none')
    wrapper.unmount()
  })

  it('loads the workbench and opens the connector form from the channel view', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => notificationResponse(String(input)))
    const wrapper = mount(NotificationsView)
    await flushPromises()

    expect(wrapper.text()).toContain('通知中心')
    expect(wrapper.text()).toContain('投递状态')
    await wrapper.findAll('[role="tab"]')[1].trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.name).toBe('notifications-connectors')
    expect(wrapper.text()).toContain('连接器与端点')
    expect(wrapper.text()).toContain('ops-bot')
    const addConnector = wrapper.findAll('button').find((node) => node.text().includes('添加连接器'))
    expect(addConnector).toBeDefined()
    await addConnector!.trigger('click')
    expect(document.body.textContent).toContain('新建连接器')
    expect(document.body.textContent).toContain('密钥引用')
    wrapper.unmount()
  })

  it('closes open dialogs when leaving the cached notification workspace', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => notificationResponse(String(input)))
    const removeTargetRoute = router.addRoute({
      path: '/notification-test-target',
      name: 'notification-test-target',
      component: { render: () => null }
    })
    await router.push('/notifications/connectors')
    const wrapper = mount(App, { attachTo: document.body })
    await flushPromises()

    const addConnector = wrapper.findAll('button').find((node) => node.text().includes('添加连接器'))
    expect(addConnector).toBeDefined()
    await addConnector!.trigger('click')
    await flushPromises()
    expect(document.querySelector('.ant-modal-wrap')).not.toBeNull()

    await router.push('/notification-test-target')
    await flushPromises()
    expect((document.querySelector('.ant-modal') as HTMLElement).style.display).toBe('none')
    wrapper.unmount()
    removeTargetRoute()
  })
})

describe('notification activity channels', () => {
  function deferred<T>() {
    let resolve!: (value: T) => void
    const promise = new Promise<T>((settle) => { resolve = settle })
    return { promise, resolve }
  }

  function eventPage(ids: string[], offset = 0): Page<NotificationEvent> {
    return {
      items: ids.map((id) => ({ id })) as unknown as NotificationEvent[],
      total: 50,
      limit: 8,
      offset
    }
  }

  it('并发加载时只写回最新一次的结果', async () => {
    const activity = useNotificationActivity({ setError: () => undefined })
    const slow = deferred<Page<NotificationEvent>>()
    const fast = deferred<Page<NotificationEvent>>()
    vi.spyOn(notificationApi, 'events')
      .mockReturnValueOnce(slow.promise)
      .mockReturnValueOnce(fast.promise)

    const firstLoad = activity.loadEvents()
    const secondLoad = activity.loadEvents()

    // 后发的先到，再放先发的：迟到的那次必须被丢掉，
    // 否则界面会退回上一次筛选条件的结果。
    fast.resolve(eventPage(['new']))
    await secondLoad
    slow.resolve(eventPage(['stale']))
    await firstLoad

    expect(activity.events.value.items.map((item) => item.id)).toEqual(['new'])
  })

  it('事件与投递各自持有 loading，一方返回不影响另一方', async () => {
    const activity = useNotificationActivity({ setError: () => undefined })
    const events = deferred<Page<NotificationEvent>>()
    const deliveries = deferred<Page<NotificationDelivery>>()
    vi.spyOn(notificationApi, 'events').mockReturnValue(events.promise)
    vi.spyOn(notificationApi, 'deliveries').mockReturnValue(deliveries.promise)

    const eventsLoad = activity.loadEvents()
    const deliveriesLoad = activity.loadDeliveries()
    expect(activity.eventsLoading.value).toBe(true)
    expect(activity.deliveriesLoading.value).toBe(true)

    deliveries.resolve({ items: [], total: 0, limit: 8, offset: 0 })
    await deliveriesLoad

    // 投递返回不该把仍在飞行中的事件请求标成已完成。
    expect(activity.deliveriesLoading.value).toBe(false)
    expect(activity.eventsLoading.value).toBe(true)
    expect(activity.activityLoading.value).toBe(true)

    events.resolve(eventPage([]))
    await eventsLoad
    expect(activity.activityLoading.value).toBe(false)
  })

  it('改筛选后回到第一页，并在成功后清掉上一次的错误', async () => {
    const messages: string[] = []
    const activity = useNotificationActivity({ setError: (text) => messages.push(text) })
    const events = vi.spyOn(notificationApi, 'events')
      .mockRejectedValueOnce(new Error('后端不可用'))
      .mockResolvedValue(eventPage(['e-1']))

    activity.changeEventPage(3)
    await flushPromises()
    expect(events.mock.calls[0][0]).toMatchObject({ offset: 16 })
    expect(messages).toEqual(['后端不可用'])

    // 新条件下的结果集通常更短，留在第 3 页会直接落到空白上。
    activity.eventFilters.severity = 'critical'
    await activity.loadEvents(0)

    expect(events.mock.calls[1][0]).toMatchObject({ offset: 0, severity: 'critical' })
    expect(activity.events.value.offset).toBe(0)
    expect(messages.at(-1)).toBe('')
  })
})
