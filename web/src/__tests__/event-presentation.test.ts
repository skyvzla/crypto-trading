import { describe, expect, it } from 'vitest'
import { eventDisplayName, eventParameterRows, formatEventValue } from '@/features/backtests/eventPresentation'

describe('回测事件展示', () => {
  it('使用中文（英文）事件名并为未知事件保留原始类型', () => {
    expect(eventDisplayName({ type: 'signal_triggered', title: 'signal_triggered' })).toBe('信号触发（signal_triggered）')
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
        tier_prices: ['1.1', '1.2']
      }
    })

    expect(rows.slice(0, 3).map((row) => row.key)).toEqual(['rise_5s', 'volume_multiple_5s', 'tier_prices'])
    expect(rows.find((row) => row.key === 'rise_5s')).toMatchObject({
      value: '6.00%', reference: '5 秒涨幅门槛：5.00%'
    })
    expect(rows.find((row) => row.key === 'volume_multiple_5s')).toMatchObject({
      value: '6.5', reference: '5 秒量比门槛：5'
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
      { oi_stop_oi_rise_pct: 10, oi_stop_loss_pct: 3 }
    )
    expect(rows.find((row) => row.key === 'd_oi_pct')?.reference).toBe('持仓量止损涨幅门槛：10%')
    expect(rows.find((row) => row.key === 'loss_pct')?.reference).toBe('持仓量止损亏损门槛：3%')
  })
})
