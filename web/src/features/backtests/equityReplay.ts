import type { BacktestEquityTrade } from '@/api/types'

export interface EquityReplaySettings {
  initialBalance: number
  initialPosition: number
  reinvestRatio: number
  minimumBalance: number
  feeRate: number
  slippageRate: number
}

export interface EquityReplayRow extends BacktestEquityTrade {
  sequence: number
  status: 'executed' | 'skipped'
  skipReason?: 'overlap' | 'liquidated' | 'open'
  balanceBefore: number
  positionAmount: number
  grossReturn: number
  costRate: number
  feeAmount: number
  slippageAmount: number
  netReturn: number
  replayPnl: number
  reinvestedProfit: number
  tradingCapitalAfter: number
  reserveCapitalAfter: number
  balanceAfter: number
  drawdown: number
}

export interface EquityPoint {
  time: number
  value: number
  row?: EquityReplayRow
}

export interface EquityReplayResult {
  rows: EquityReplayRow[]
  points: EquityPoint[]
  finalBalance: number
  netProfit: number
  returnRate: number
  maxDrawdown: number
  minimumObservedBalance: number
  finalTradingCapital: number
  finalReserveCapital: number
  executedCount: number
  skippedCount: number
  liquidated: boolean
  liquidationTime: number | null
}

function timestamp(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined) return null
  const numeric = typeof value === 'number' ? value : Number(value)
  if (Number.isFinite(numeric)) return numeric
  const parsed = Date.parse(String(value))
  return Number.isFinite(parsed) ? parsed : null
}

function finite(value: number | null | undefined, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

export function replayEquity(
  trades: BacktestEquityTrade[],
  settings: EquityReplaySettings
): EquityReplayResult {
  const ordered = [...trades].sort((left, right) => {
    const leftTime = timestamp(left.entry_time) ?? timestamp(left.signal_time) ?? 0
    const rightTime = timestamp(right.entry_time) ?? timestamp(right.signal_time) ?? 0
    return leftTime - rightTime || left.symbol.localeCompare(right.symbol) || left.id.localeCompare(right.id)
  })
  const initialBalance = Math.max(0, finite(settings.initialBalance))
  const initialPosition = Math.min(initialBalance, Math.max(0, finite(settings.initialPosition)))
  const reinvestRatio = Math.min(1, Math.max(0, finite(settings.reinvestRatio)))
  const minimumBalance = Math.max(0, finite(settings.minimumBalance))
  const feeRate = Math.max(0, finite(settings.feeRate))
  const slippageRate = Math.max(0, finite(settings.slippageRate))
  const costRate = 2 * (feeRate + slippageRate)
  const rows: EquityReplayRow[] = []
  const points: EquityPoint[] = []
  let tradingCapital = initialPosition
  let reserveCapital = initialBalance - initialPosition
  let balance = initialBalance
  let peak = initialBalance
  let maxDrawdown = 0
  let minimumObservedBalance = initialBalance
  let activeUntil = -Infinity
  let liquidated = tradingCapital <= minimumBalance
  let liquidationTime: number | null = liquidated ? (timestamp(ordered[0]?.entry_time) ?? null) : null

  const firstTime = timestamp(ordered[0]?.entry_time)
  if (firstTime !== null) points.push({ time: firstTime - 1, value: initialBalance })

  ordered.forEach((trade, index) => {
    const entryTime = timestamp(trade.entry_time) ?? timestamp(trade.signal_time) ?? 0
    const exitTime = timestamp(trade.exit_time)
    const balanceBefore = tradingCapital + reserveCapital
    const grossReturn = finite(
      trade.gross_return,
      trade.entry_notional && trade.entry_notional > 0 && trade.gross_pnl !== null && trade.gross_pnl !== undefined
        ? trade.gross_pnl / trade.entry_notional
        : finite(trade.net_return)
    )
    const base = {
      ...trade,
      sequence: index + 1,
      balanceBefore,
      positionAmount: 0,
      grossReturn,
      costRate,
      feeAmount: 0,
      slippageAmount: 0,
      netReturn: 0,
      replayPnl: 0,
      reinvestedProfit: 0,
      tradingCapitalAfter: tradingCapital,
      reserveCapitalAfter: reserveCapital,
      balanceAfter: balanceBefore,
      drawdown: peak > 0 ? (balanceBefore - peak) / peak : 0
    }

    if (liquidated) {
      rows.push({ ...base, status: 'skipped', skipReason: 'liquidated' })
      return
    }
    if (entryTime < activeUntil) {
      rows.push({ ...base, status: 'skipped', skipReason: 'overlap' })
      return
    }
    if (exitTime === null) {
      rows.push({ ...base, status: 'skipped', skipReason: 'open' })
      return
    }

    const positionAmount = tradingCapital
    const feeAmount = positionAmount * feeRate * 2
    const slippageAmount = positionAmount * slippageRate * 2
    const netReturn = grossReturn - costRate
    const replayPnl = Math.max(-positionAmount, positionAmount * netReturn)
    const reinvestedProfit = replayPnl > 0 ? replayPnl * reinvestRatio : 0
    if (replayPnl > 0) {
      tradingCapital += reinvestedProfit
      reserveCapital += replayPnl - reinvestedProfit
    } else {
      tradingCapital += replayPnl
    }
    balance = tradingCapital + reserveCapital
    peak = Math.max(peak, balance)
    minimumObservedBalance = Math.min(minimumObservedBalance, balance)
    const drawdown = peak > 0 ? (balance - peak) / peak : 0
    maxDrawdown = Math.min(maxDrawdown, drawdown)
    activeUntil = exitTime

    const row: EquityReplayRow = {
      ...base,
      status: 'executed',
      positionAmount,
      feeAmount,
      slippageAmount,
      netReturn,
      replayPnl,
      reinvestedProfit,
      tradingCapitalAfter: tradingCapital,
      reserveCapitalAfter: reserveCapital,
      balanceAfter: balance,
      drawdown
    }
    rows.push(row)
    points.push({ time: exitTime, value: balance, row })

    if (tradingCapital <= minimumBalance) {
      liquidated = true
      liquidationTime = exitTime
    }
  })

  const executedCount = rows.filter((row) => row.status === 'executed').length
  return {
    rows,
    points,
    finalBalance: balance,
    netProfit: balance - initialBalance,
    returnRate: initialBalance > 0 ? (balance - initialBalance) / initialBalance : 0,
    maxDrawdown,
    minimumObservedBalance,
    finalTradingCapital: tradingCapital,
    finalReserveCapital: reserveCapital,
    executedCount,
    skippedCount: rows.length - executedCount,
    liquidated,
    liquidationTime
  }
}
