import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '@/api/client'
import type { Health } from '@/api/types'

/** 账本服务连通性。骨架阶段只提供手动探测，轮询节奏等接口定稿后再加。 */
export const useHealthStore = defineStore('health', () => {
  const status = ref<'unknown' | 'healthy' | 'unhealthy'>('unknown')
  const checkedAt = ref<string | null>(null)

  async function check() {
    try {
      const health = await api.get<Health>('/health')
      status.value = health.status === 'healthy' ? 'healthy' : 'unhealthy'
    } catch {
      status.value = 'unhealthy'
    } finally {
      checkedAt.value = new Date().toISOString()
    }
  }

  return { status, checkedAt, check }
})
