import { describe, expect, it } from 'vitest'
import { replayEquity } from '@/features/backtests/equityReplay'
import type { BacktestEquityTrade } from '@/api/types'

function trade(overrides: Partial<BacktestEquityTrade>): BacktestEquityTrade {
  return {
    id: String(overrides.id || 'trade'),
    symbol: String(overrides.symbol || 'BTCUSDT'),
    entry_time: overrides.entry_time ?? 1_000,
    exit_time: overrides.exit_time ?? 2_000,
    entry_price: overrides.entry_price ?? 100,
    exit_price: overrides.exit_price ?? 90,
    net_pnl: overrides.net_pnl ?? 100,
    gross_pnl: overrides.gross_pnl ?? 100,
    entry_notional: overrides.entry_notional ?? 1_000,
    ...overrides,
  }
}

describe('账户收益回放', () => {
  it('盈利按比例复投，亏损只从交易资金池扣减，并扣除双边费用', () => {
    const result = replayEquity(
      [
        trade({ id: 'one', gross_return: 0.1 }),
        trade({ id: 'two', entry_time: 3_000, exit_time: 4_000, gross_return: -0.1 }),
      ],
      {
        initialBalance: 1_000,
        initialPosition: 500,
        reinvestRatio: 0.5,
        minimumBalance: 0,
        feeRate: 0.0004,
        slippageRate: 0.001,
      },
    )

    expect(result.rows[0].positionAmount).toBe(500)
    expect(result.rows[0].costRate).toBeCloseTo(0.0028)
    expect(result.rows[0].balanceAfter).toBeCloseTo(1_048.6)
    expect(result.rows[0]).toMatchObject({
      tradingCapitalAfter: 524.3,
      reserveCapitalAfter: 524.3,
      reinvestedProfit: 24.3,
    })
    expect(result.rows[1].positionAmount).toBeCloseTo(524.3)
    expect(result.rows[1]).toMatchObject({ reserveCapitalAfter: 524.3 })
    expect(result.rows[1].tradingCapitalAfter).toBeCloseTo(470.40196)
    expect(result.finalBalance).toBeCloseTo(994.70196)
  })

  it('亏损不会动用锁定储备，达到最低交易资金池后终止后续交易', () => {
    const result = replayEquity(
      [
        trade({ id: 'loss', gross_return: -0.95 }),
        trade({ id: 'later', entry_time: 3_000, exit_time: 4_000, gross_return: 1 }),
      ],
      {
        initialBalance: 1_000,
        initialPosition: 500,
        reinvestRatio: 0.5,
        minimumBalance: 100,
        feeRate: 0,
        slippageRate: 0,
      },
    )

    expect(result.liquidated).toBe(true)
    expect(result.rows[0]).toMatchObject({ tradingCapitalAfter: 25, reserveCapitalAfter: 500 })
    expect(result.finalBalance).toBeCloseTo(525)
    expect(result.rows[1]).toMatchObject({ status: 'skipped', skipReason: 'liquidated' })
  })

  it('按策略单持仓规则忽略旧仓退出前的新信号', () => {
    const result = replayEquity(
      [
        trade({ id: 'active', signal_time: 1_000, entry_time: 2_000, exit_time: 10_000, gross_return: 0.1 }),
        trade({ id: 'overlap', signal_time: 5_000, entry_time: 6_000, exit_time: 7_000, gross_return: 1 }),
        trade({ id: 'next', signal_time: 11_000, entry_time: 12_000, exit_time: 13_000, gross_return: 0.1 }),
      ],
      {
        initialBalance: 1_000,
        initialPosition: 1_000,
        reinvestRatio: 1,
        minimumBalance: 0,
        feeRate: 0,
        slippageRate: 0,
      },
    )

    expect(result.rows.map((row) => row.status)).toEqual(['executed', 'skipped', 'executed'])
    expect(result.rows[1].skipReason).toBe('overlap')
    expect(result.finalBalance).toBeCloseTo(1_210)
  })
})
