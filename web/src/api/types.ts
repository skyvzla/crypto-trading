// 后端接口仍在调整，这里只固化当前 /api/v1 已存在且稳定的字段。
// Decimal 在 JSON 中是字符串，保持 string 以避免精度丢失。

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface Health {
  status: string
  service: string
  timestamp: string
}

export interface LedgerFilters {
  account_id?: string
  strategy_id?: string
  symbol?: string
}
