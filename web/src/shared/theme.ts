import type { InjectionKey, Ref } from 'vue'

/**
 * 深色模式开关。由 App.vue 提供，图表组件注入后据此重建调色板。
 *
 * 用 InjectionKey 而不是裸字符串，provide/inject 两侧的类型才会被检查，
 * 键名写错也会在编译期暴露。
 */
export const IS_DARK_THEME: InjectionKey<Readonly<Ref<boolean>>> = Symbol('isDarkTheme')
