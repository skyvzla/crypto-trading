import { describe, expect, it } from 'vitest'
import { formatNumber, formatPercent, pnlClass } from '@/features/backtests/format'

describe('回测数字格式化', () => {
  it('兼容 PostgreSQL NUMERIC 的字符串响应', () => {
    expect(formatNumber('6068.242806862', 2)).toBe('6,068.24')
    expect(formatPercent('0.6165')).toBe('61.65%')
    expect(pnlClass('-10.5')).toBe('value-negative')
  })
})
