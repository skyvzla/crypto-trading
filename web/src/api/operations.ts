import { api } from '@/api/client'
import { LEDGER_TIMEZONE } from '@/shared/time'
import type {
  AdmissionUpdate,
  CampaignPage,
  CampaignPnL,
  CampaignQuery,
  DailyPnL,
  DailyPnLQuery,
  ExchangeCategory,
  ExchangeSymbol,
  ExchangeSymbolQuery,
  ExchangeSymbolSyncStatus,
  Health,
  LedgerAccount,
  LedgerOrder,
  LedgerPosition,
  LedgerTrade,
  OrderQuery,
  Page,
  PageParams,
  PerformanceQuery,
  PerformanceBreakdownQuery,
  PerformanceBreakdownResponse,
  PerformanceSummary,
  PnLQuery,
  PnLSummary,
  PositionQuery,
  RuntimeStatusQuery,
  StrategyAuditEvent,
  StrategyCapitalStatus,
  StrategyCapitalStatusQuery,
  StrategyCategoryAdmission,
  StrategyCategoryAdmissionAudit,
  StrategyRuntimeStatus,
  SymbolGlobalAdmission,
  SymbolGlobalAdmissionAudit,
  TradeQuery,
  UniversePreview,
  UniversePreviewQuery,
} from '@/api/types'

const segment = (value: string) => encodeURIComponent(value)

export interface StrategyAuditQuery extends PageParams {
  account_id?: string
  strategy_id?: string
  symbol?: string
  event_type?: string
  campaign_id?: string
}

export interface StrategyAdmissionAuditQuery extends PageParams {
  strategy_id?: string
}

export interface SymbolAdmissionAuditQuery extends PageParams {
  symbol?: string
}

export const operationsApi = {
  health: () => api.get<Health>('/health'),

  runtimeStatus: (query: RuntimeStatusQuery = {}) =>
    api.get<Page<StrategyRuntimeStatus>>('/strategy-runtime-status', { ...query }),

  capitalStatus: (query: StrategyCapitalStatusQuery) =>
    api.get<StrategyCapitalStatus>('/strategy-capital-status', { ...query }),

  accounts: (query: PageParams = {}) => api.get<Page<LedgerAccount>>('/accounts', { ...query }),

  pnl: (query: PnLQuery) => api.get<PnLSummary>('/pnl', { ...query }),

  dailyPnl: (query: DailyPnLQuery) =>
    api.get<DailyPnL[]>('/pnl/daily', {
      ...query,
      timezone: query.timezone ?? LEDGER_TIMEZONE,
    }),

  performance: (query: PerformanceQuery) => api.get<PerformanceSummary>('/performance', { ...query }),

  performanceBreakdown: (query: PerformanceBreakdownQuery) =>
    api.get<PerformanceBreakdownResponse>('/performance/breakdown', { ...query }),

  positions: (query: PositionQuery = {}) => api.get<Page<LedgerPosition>>('/positions', { ...query }),

  orders: (query: OrderQuery = {}) => api.get<Page<LedgerOrder>>('/orders', { ...query }),

  trades: (query: TradeQuery = {}) => api.get<Page<LedgerTrade>>('/trades', { ...query }),

  campaigns: (query: CampaignQuery = {}) =>
    api.get<CampaignPage>('/campaigns', {
      ...query,
      timezone: query.timezone ?? LEDGER_TIMEZONE,
    }),

  campaignPnl: (campaignId: string, query: { account_id: string; strategy_id: string }) =>
    api.get<CampaignPnL>(`/campaigns/${segment(campaignId)}/pnl`, query),

  exchangeSymbols: (query: ExchangeSymbolQuery = {}) =>
    api.get<Page<ExchangeSymbol>>('/exchange-symbols', { ...query }),

  symbolCategories: (symbol: string) => api.get<ExchangeCategory[]>(`/exchange-symbols/${segment(symbol)}/categories`),

  categories: (activeOnly = true) => api.get<ExchangeCategory[]>('/exchange-categories', { active_only: activeOnly }),

  categoriesPage: (activeOnly = true, query: PageParams = {}) =>
    api.get<Page<ExchangeCategory>>('/exchange-categories/page', {
      active_only: activeOnly,
      ...query,
    }),

  categorySymbols: (categoryKey: string, query: PageParams = {}) =>
    api.get<Page<ExchangeSymbol>>(`/exchange-categories/${segment(categoryKey)}/symbols`, { ...query }),

  symbolSyncStatus: () => api.get<ExchangeSymbolSyncStatus>('/exchange-symbol-sync/status'),

  symbolAdmission: (symbol: string) => api.get<SymbolGlobalAdmission>(`/exchange-symbols/${segment(symbol)}/admission`),

  updateSymbolAdmission: (symbol: string, update: AdmissionUpdate) =>
    api.put<SymbolGlobalAdmission>(`/exchange-symbols/${segment(symbol)}/admission`, update),

  symbolAdmissionAudits: (query: SymbolAdmissionAuditQuery = {}) =>
    api.get<Page<SymbolGlobalAdmissionAudit>>('/symbol-global-admission-audit', { ...query }),

  strategyAdmissions: (strategyId: string) =>
    api.get<StrategyCategoryAdmission[]>(`/strategy-category-admissions/${segment(strategyId)}`),

  strategyAdmissionsPage: (strategyId: string, query: PageParams = {}) =>
    api.get<Page<StrategyCategoryAdmission>>(`/strategy-category-admissions/${segment(strategyId)}/page`, { ...query }),

  updateStrategyAdmission: (strategyId: string, categoryKey: string, update: AdmissionUpdate) =>
    api.put<StrategyCategoryAdmission>(
      `/strategy-category-admissions/${segment(strategyId)}/${segment(categoryKey)}`,
      update,
    ),

  strategyAdmissionAudits: (query: StrategyAdmissionAuditQuery = {}) =>
    api.get<Page<StrategyCategoryAdmissionAudit>>('/strategy-category-admission-audit', { ...query }),

  universePreview: (strategyId: string, query: UniversePreviewQuery = {}) =>
    api.get<UniversePreview>(`/strategy-category-admissions/${segment(strategyId)}/universe-preview`, { ...query }),

  strategyAuditEvents: (query: StrategyAuditQuery = {}) =>
    api.get<Page<StrategyAuditEvent>>('/strategy-audit-events', { ...query }),
}
