import { darkTheme, type GlobalThemeOverrides } from 'naive-ui'

/** 蓝色深色主题。色板集中在此处，组件不再各自写死颜色。 */
export const palette = {
  primary: '#3b82f6',
  primaryHover: '#60a5fa',
  primaryPressed: '#2563eb',
  primarySuppl: '#1d4ed8',
  base: '#0b1220',
  surface: '#111a2b',
  elevated: '#16213a',
  line: '#22304a',
  text: '#e6edf7',
  muted: '#8ea3c0',
  good: '#34d399',
  bad: '#f87171',
  warn: '#fbbf24'
} as const

export const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: palette.primary,
    primaryColorHover: palette.primaryHover,
    primaryColorPressed: palette.primaryPressed,
    primaryColorSuppl: palette.primarySuppl,
    successColor: palette.good,
    errorColor: palette.bad,
    warningColor: palette.warn,
    bodyColor: palette.base,
    cardColor: palette.surface,
    modalColor: palette.surface,
    popoverColor: palette.elevated,
    tableColor: palette.surface,
    tableHeaderColor: palette.elevated,
    borderColor: palette.line,
    dividerColor: palette.line,
    textColorBase: palette.text,
    textColor1: palette.text,
    textColor2: '#c7d5ea',
    textColor3: palette.muted,
    borderRadius: '6px',
    fontFamily:
      '"Inter", "Noto Sans SC", -apple-system, "Segoe UI", sans-serif',
    fontFamilyMono:
      '"JetBrains Mono", "IBM Plex Mono", "Noto Sans Mono", monospace'
  },
  Layout: {
    color: palette.base,
    siderColor: palette.surface,
    headerColor: palette.surface,
    headerBorderColor: palette.line,
    siderBorderColor: palette.line
  },
  Menu: {
    itemTextColorActive: palette.primaryHover,
    itemTextColorActiveHover: palette.primaryHover,
    itemIconColorActive: palette.primaryHover,
    itemColorActive: 'rgba(59, 130, 246, 0.14)',
    itemColorActiveHover: 'rgba(59, 130, 246, 0.2)'
  },
  DataTable: {
    thTextColor: palette.muted,
    tdColorHover: palette.elevated,
    borderColor: palette.line
  }
}

export { darkTheme }
