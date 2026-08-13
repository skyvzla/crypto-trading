import { api, ApiError } from '@/api/client'
import type {
  BacktestCandlesResponse,
  BacktestEvent,
  BacktestReportPage,
  BacktestResearch,
  BacktestReplayParameterSet,
  BacktestReplayTradesResponse,
  BacktestStrategyDescriptor,
  BacktestSymbolSummary,
  BacktestTradeDetail,
  BacktestTradeSummary,
  Page,
  ReportDescriptor
} from '@/api/types'

const segment = (value: string) => encodeURIComponent(value)

export const backtestApi = {
  researches: (limit: number, offset: number) =>
    api.get<Page<BacktestResearch>>('/backtest-researches', { limit, offset }),
  replayParameterSets: (researchId: string) =>
    api.get<{ items: BacktestReplayParameterSet[] }>(
      `/backtest-researches/${segment(researchId)}/replay-parameter-sets`
    ),
  replayTrades: (researchId: string, parameters: Record<string, unknown>) =>
    api.get<BacktestReplayTradesResponse>(
      `/backtest-researches/${segment(researchId)}/replay-trades`,
      { parameters: JSON.stringify(parameters) }
    ),
  reports: (researchId: string) =>
    api.get<{ items: ReportDescriptor[] }>(`/backtest-researches/${segment(researchId)}/reports`),
  report: (researchId: string, type: string, limit: number, offset: number, sortBy?: string, sortOrder?: string) =>
    api.get<BacktestReportPage>(
      `/backtest-researches/${segment(researchId)}/reports/${segment(type)}`,
      { limit, offset, ...(sortBy ? { sort_by: sortBy, sort_order: sortOrder || 'desc' } : {}) }
    ),
  symbols: (researchId: string, limit: number, offset: number, symbolFilter = '', sortBy = 'net_pnl', sortOrder = 'desc') =>
    api.get<Page<BacktestSymbolSummary>>(
      `/backtest-researches/${segment(researchId)}/symbols`,
      { limit, offset, symbol_filter: symbolFilter || undefined, sort_by: sortBy, sort_order: sortOrder }
    ),
  trades: (researchId: string, symbol: string, limit: number, offset: number, filters: {
    winner?: boolean
    exit_reason?: string
    min_pnl?: number
    max_pnl?: number
    sort_by?: string
    sort_order?: 'asc' | 'desc'
  } = {}) =>
    api.get<Page<BacktestTradeSummary>>(
      `/backtest-researches/${segment(researchId)}/symbols/${segment(symbol)}/trades`,
      { limit, offset, ...filters }
    ),
  trade: (researchId: string, tradeId: string) =>
    api.get<BacktestTradeDetail>(
      `/backtest-researches/${segment(researchId)}/trades/${segment(tradeId)}`
    ),
  events: (researchId: string, tradeId: string) =>
    api.get<{ items: BacktestEvent[] }>(
      `/backtest-researches/${segment(researchId)}/trades/${segment(tradeId)}/events`
    ),
  candles: (query: {
    research_id: string
    symbol: string
    interval: string
    start_ms: number
    end_ms: number
    source: 'binance' | 'archive'
  }) => api.get<BacktestCandlesResponse>('/backtest-candles', query),
  strategySchema: async (strategyId: string): Promise<BacktestStrategyDescriptor | null> => {
    try {
      return await api.get<BacktestStrategyDescriptor>(
        `/backtest-strategies/${segment(strategyId)}/schema`
      )
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null
      throw error
    }
  }
}
