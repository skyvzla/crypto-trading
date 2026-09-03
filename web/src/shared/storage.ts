/**
 * 本地存储键与安全读写。
 *
 * 键集中在这里，避免同一个键在多个组件里各写一遍字符串字面量；
 * 读写一律容错——隐私模式下 localStorage 会直接抛异常，
 * 此时功能降级为「仅本次会话有效」，不应影响页面渲染。
 */
export const STORAGE_KEYS = {
  theme: 'trade-ledger-theme',
  chartHeight: 'backtest-replay-chart-height-v1',
  indicatorPaneStretch: 'backtest-replay-indicator-pane-stretch-v1',
} as const

export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS]

export function readStored(key: StorageKey): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeStored(key: StorageKey, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // 浏览器禁用本地存储时保留本次会话的选择即可。
  }
}

export function removeStored(key: StorageKey): void {
  try {
    localStorage.removeItem(key)
  } catch {
    // 同上：读写失败不影响页面功能。
  }
}

/** 读取 JSON 对象形式的偏好；结构不符时回退到空对象。 */
export function readStoredRecord(key: StorageKey): Record<string, number> {
  const raw = readStored(key)
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as Record<string, number>) : {}
  } catch {
    return {}
  }
}
