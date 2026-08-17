import { describe, expect, it } from 'vitest'
import { sideLabel } from '@/features/operations/format'

describe('运营页方向格式化', () => {
  it('将交易与持仓方向统一显示为买卖', () => {
    expect(sideLabel('BUY')).toBe('买 / BUY')
    expect(sideLabel('LONG')).toBe('买 / BUY')
    expect(sideLabel('SELL')).toBe('卖 / SELL')
    expect(sideLabel('SHORT')).toBe('卖 / SELL')
  })
})
