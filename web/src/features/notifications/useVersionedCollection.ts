import { message } from 'ant-design-vue'
import type { Ref } from 'vue'

/** 通知中心四类配置的共同形状：稳定 id、启用开关、乐观锁版本。 */
export interface VersionedItem {
  id: string
  enabled: boolean
  version: number
}

export interface VersionedCollectionConfig<T extends VersionedItem> {
  items: Ref<T[]>
  /** 消息文案里的资源名，例如「连接器」。 */
  label: string
  /** 只改启用状态的更新请求。 */
  update: (item: T, enabled: boolean) => Promise<T>
  remove: (item: T) => Promise<void>
  /** 删除失败时的补充说明，通常指出前置依赖。 */
  removeHint?: string
  /** 删除成功后清理本地对该条目的引用（级联）。 */
  onRemoved?: (item: T) => void
  /** 写操作成功后刷新聚合计数。 */
  afterChange: () => Promise<void>
}

export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * 版本化配置集合的通用写操作。
 *
 * connector / endpoint / group / policy 四类资源的启用切换、删除和
 * 保存后回写逻辑完全一致，只有 API 方法和文案不同，因此收敛到这里，
 * 避免同一段乐观更新 + 回滚代码写四遍。
 */
export function useVersionedCollection<T extends VersionedItem>(config: VersionedCollectionConfig<T>) {
  /** 立刻反映开关状态，失败再回滚——否则开关会有明显的延迟感。 */
  async function toggle(item: T, enabled: boolean): Promise<void> {
    const previous = item.enabled
    item.enabled = enabled
    try {
      const updated = await config.update(item, enabled)
      config.items.value = config.items.value.map((row) => (row.id === item.id ? updated : row))
      await config.afterChange()
    } catch (error) {
      item.enabled = previous
      message.error(errorMessage(error, `${config.label}状态更新失败`))
    }
  }

  async function destroy(item: T): Promise<void> {
    try {
      await config.remove(item)
      config.items.value = config.items.value.filter((row) => row.id !== item.id)
      config.onRemoved?.(item)
      message.success(`${config.label}已删除`)
      await config.afterChange()
    } catch (error) {
      message.error(errorMessage(error, config.removeHint ?? `${config.label}删除失败`))
    }
  }

  /** 新建时追加、编辑时替换。 */
  function upsert(result: T, editingId: string | null): void {
    config.items.value = editingId
      ? config.items.value.map((row) => (row.id === editingId ? result : row))
      : [...config.items.value, result]
  }

  return { toggle, destroy, upsert }
}
