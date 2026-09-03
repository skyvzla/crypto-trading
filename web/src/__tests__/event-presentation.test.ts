import { describe, expect, it } from 'vitest'
import {
  eventDisplayName,
  eventParameterGroups,
  eventParameterRows,
  formatEventValue,
  resolvePricePrecision,
} from '@/views/backtests/components/eventPresentation'

describe('回测事件展示', () => {
  it('使用中文（英文）事件名并为未知事件保留原始类型', () => {
    expect(eventDisplayName({ type: 'signal_triggered', title: 'signal_triggered' })).toBe(
      '信号触发（signal_triggered）',
    )
    expect(eventDisplayName({ type: 'custom_event', title: 'custom_event' })).toContain('（custom_event）')
  })

  it('主要指标优先，并把实际值与参考门槛放在同一行', () => {
    const rows = eventParameterRows({
      price: null,
      data: {
        strategy_version: 'spike-v2',
        rise_5s: '0.06',
        rise_threshold_5s: '0.05',
        volume_multiple_5s: '6.5',
        volume_threshold_5s: '5',
        tier_prices: ['1.1', '1.2'],
      },
    })

    expect(rows.slice(0, 3).map((row) => row.key)).toEqual(['rise_5s', 'volume_multiple_5s', 'tier_prices'])
    expect(rows.find((row) => row.key === 'rise_5s')).toMatchObject({
      value: '6.00%',
      threshold: '5.00%',
      reference: '5.00%',
    })
    expect(rows.find((row) => row.key === 'volume_multiple_5s')).toMatchObject({
      value: '6.5',
      threshold: '5',
      reference: '5',
    })
    expect(rows.some((row) => row.key === 'rise_threshold_5s')).toBe(false)
    expect(rows.find((row) => row.key === 'tier_prices')?.value).toBe('1档 1.1；2档 1.2')
  })

  it('格式化时间、布尔值和嵌套参数，不输出 JSON', () => {
    expect(formatEventValue('exit_required', true)).toBe('是')
    expect(formatEventValue('active_time', 1_750_000_000_000)).not.toContain('1750000000000')
    expect(formatEventValue('low_12h', '1.234567891')).toBe('1.23456789')
    expect(formatEventValue('rise_low', '1.1')).toBe('1.1')
    expect(formatEventValue('rise_low_lookback_minutes', 60)).toBe('60')
    expect(formatEventValue('max_rise_window_seconds', 60)).toBe('60')
    expect(formatEventValue('rise_window', '0.06')).toBe('6.00%')
    expect(formatEventValue('rise_60s_threshold', '0.4')).toBe('40.00%')
    expect(formatEventValue('ls_ratio', '1.25')).toBe('1.25')
    expect(formatEventValue('hard_stop_loss_pct', '0.08')).toBe('8.00%')
    expect(formatEventValue('hard_stop_confirm_ms', 5_000)).toBe('5秒')
    expect(formatEventValue('decision', 'reduce_half')).toBe('减半仓位（reduce_half）')
    expect(formatEventValue('metrics', { rise_5s: 0.06 })).toBe('5 秒涨幅：6.00%')
  })

  it('事件未携带门槛时从该笔交易参数补充参考值', () => {
    const rows = eventParameterRows(
      { price: null, data: { d_oi_pct: 12.5, loss_pct: 4.2 } },
      { oi_stop_oi_rise_pct: 10, oi_stop_loss_pct: 3 },
    )
    expect(rows.find((row) => row.key === 'd_oi_pct')?.threshold).toBe('10%')
    expect(rows.find((row) => row.key === 'loss_pct')?.threshold).toBe('3%')
  })

  it('将事件参数按类型划分成价格、涨幅量比、风控执行等分组', () => {
    const groups = eventParameterGroups({
      price: null,
      data: {
        trigger_price: 100,
        rise_5s: 0.05,
        volume_multiple_5s: 4.2,
        oi_change_pct: 1.5,
        scored_score: 88,
        action: 'hold',
      },
    })

    expect(groups.map((g) => g.id)).toEqual(['price', 'rise_volume', 'oi', 'risk_execution', 'decision'])
    expect(groups.find((g) => g.id === 'price')?.title).toBe('价格与标线')
    expect(groups.find((g) => g.id === 'rise_volume')?.title).toBe('涨幅与量比')
    expect(groups.find((g) => g.id === 'risk_execution')?.title).toBe('风控与执行')
  })

  it('根据交易对价格样本自适应推导精度并格式化价格', () => {
    expect(resolvePricePrecision('BTCUSDT', [65000.5, 64200.1])).toBe(2)
    expect(resolvePricePrecision('ETHUSDT', [2450.25, 2460.5])).toBe(2)
    expect(resolvePricePrecision('DOGEUSDT', [0.12345, 0.1234])).toBe(5)
    expect(resolvePricePrecision('MIDUSDT', [1.234567890123, 1.25])).toBe(4)
    expect(formatEventValue('trigger_price', 65000.5, 2)).toBe('65,000.5')
    expect(formatEventValue('trigger_price', 0.123456, 5)).toBe('0.12346')
  })

  it('压缩盈亏、数量和量能精度，不透传后端高精度 Decimal', () => {
    expect(formatEventValue('net_pnl', '86.17378087397086763774540848')).toBe('86.174')
    expect(formatEventValue('gross_pnl', '-1.23456')).toBe('-1.235')
    expect(formatEventValue('quantity', '123456.123456789')).toBe('123,456.123457')
    expect(formatEventValue('volume_5s', '12345.6789')).toBe('12.35k')
    expect(formatEventValue('median_volume_1s', 2_500_000)).toBe('2.5m')
    expect(formatEventValue('volume_5s', 999_999)).toBe('1m')
    expect(formatEventValue('volume_5s', 999_999_999)).toBe('1b')
    expect(formatEventValue('volume_multiple_5s', 12.3456)).toBe('12.35')
    expect(formatEventValue('scored_score', '88.123456789')).toBe('88.123457')
  })
})
