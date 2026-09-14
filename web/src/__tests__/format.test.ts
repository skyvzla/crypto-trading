import { describe, expect, it } from 'vitest'
import { formatMoney, formatNumber, formatPercent, pnlClass, toNumberOrNull } from '@/shared/format'

describe('回测数字格式化', () => {
  it('兼容 PostgreSQL NUMERIC 的字符串响应', () => {
    expect(formatNumber('6068.242806862', 2)).toBe('6,068.24')
    expect(formatPercent('0.6165')).toBe('61.65%')
    expect(formatPercent(5)).toBe('500%')
    expect(pnlClass('-10.5')).toBe('value-negative')
  })

  it('把负零归一化，避免金额和百分比显示出负号', () => {
    expect(toNumberOrNull(-0)).toBe(0)
    expect(toNumberOrNull('-0.004')).toBe(-0.004)
    expect(formatMoney(-0)).toBe('0.00')
    expect(formatMoney(-0.004, 2)).toBe('0.00')
    expect(formatPercent(-0)).toBe('0%')
    expect(formatPercent(-0.00004, 0)).toBe('0%')
    expect(pnlClass(-0)).toBe('value-neutral')
  })
})
