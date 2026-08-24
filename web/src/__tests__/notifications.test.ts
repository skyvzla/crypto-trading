import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { notificationApi } from '@/api/client'
import App from '@/App.vue'
import NotificationsView from '@/views/NotificationsView.vue'
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
