import type { InjectionKey, Ref } from 'vue'

/** Ant Design 的 seed token 必须是可解析的实色，不能直接传 CSS var。 */
export const LIGHT_UI_COLORS = {
  primary: '#2563eb',
  info: '#1d4ed8',
  success: '#047857',
  warning: '#a16207',
  error: '#b91c1c',
  solidText: '#ffffff',
} as const

export const DARK_UI_COLORS = {
  primary: '#60a5fa',
  info: '#93c5fd',
  success: '#6ee7b7',
  warning: '#fbbf24',
  error: '#fca5a5',
  solidText: '#0b1220',
} as const

/**
 * 深色模式开关。由 App.vue 提供，图表组件注入后据此重建调色板。
 *
 * 用 InjectionKey 而不是裸字符串，provide/inject 两侧的类型才会被检查，
 * 键名写错也会在编译期暴露。
 */
export const IS_DARK_THEME: InjectionKey<Readonly<Ref<boolean>>> = Symbol('isDarkTheme')
