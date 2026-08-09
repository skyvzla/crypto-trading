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

export interface ExchangeSymbol {
  symbol: string
  pair: string
  contract_type: string
  status: string
  onboard_date: string | null
  delivery_date: string | null
  base_asset: string | null
  quote_asset: string | null
  margin_asset: string | null
  underlying_type: string | null
  active: boolean
  synced_at: string
  global_enabled: boolean
  global_admission_version: number
}

export interface ExchangeCategory {
  category_key: string
  source: string
  category_type: 'CATEGORY' | 'SUBCATEGORY'
  code: string
  name: string
  parent_key: string | null
  active: boolean
  synced_at: string
}

export interface StrategyCategoryAdmission {
  strategy_id: string
  category_key: string
  enabled: boolean
  version: number
  updated_at: string
  updated_by: string
  reason: string | null
}
