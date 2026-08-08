import { describe, it, expect } from 'vitest'
import { palette, themeOverrides, darkTheme } from '@/theme'

describe('theme', () => {
  it('palette contains all required tokens', () => {
    expect(palette).toHaveProperty('primary', '#3b82f6')
    expect(palette).toHaveProperty('primaryHover')
    expect(palette).toHaveProperty('base')
    expect(palette).toHaveProperty('surface')
    expect(palette).toHaveProperty('text')
    expect(palette).toHaveProperty('good')
    expect(palette).toHaveProperty('bad')
  })

  it('themeOverrides wires palette into naive-ui tokens', () => {
    const c = themeOverrides.common!
    expect(c.primaryColor).toBe(palette.primary)
    expect(c.bodyColor).toBe(palette.base)
    expect(c.textColorBase).toBe(palette.text)
  })

  it('exports darkTheme from naive-ui', () => {
    expect(darkTheme).toBeDefined()
    expect(darkTheme.name).toBeDefined()
  })
})
