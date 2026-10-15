<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, provide, ref } from 'vue'
import {
  ArrowLeftRight,
  Bell,
  ChartNoAxesCombined,
  FlaskConical,
  LayoutDashboard,
  ListChecks,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  Sun,
  Tags,
  WalletCards,
} from 'lucide-vue-next'
import { RouterLink, RouterView, useRoute, type RouteLocationRaw } from 'vue-router'
import { theme as antdTheme, type MenuProps } from 'ant-design-vue'
import { useHealthStore } from '@/stores/health'
import { DARK_UI_COLORS, IS_DARK_THEME, LIGHT_UI_COLORS } from '@/shared/theme'
import { STORAGE_KEYS, readStored, writeStored } from '@/shared/storage'
import { formatLedgerClock } from '@/shared/time'

/** 账本连通性探测间隔。够快能发现服务中断，又不至于把 /health 打满。 */
const HEALTH_POLL_INTERVAL_MS = 30_000

const route = useRoute()
const health = useHealthStore()
const siderCollapsed = ref(false)
const themeMode = ref<'light' | 'dark'>('light')
const isDarkTheme = computed(() => themeMode.value === 'dark')
const uiColors = computed(() => (isDarkTheme.value ? DARK_UI_COLORS : LIGHT_UI_COLORS))
const providerTheme = computed(() => ({
  algorithm: isDarkTheme.value ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: uiColors.value.primary,
    colorInfo: uiColors.value.info,
    colorSuccess: uiColors.value.success,
    colorWarning: uiColors.value.warning,
    colorError: uiColors.value.error,
    colorTextLightSolid: uiColors.value.solidText,
    borderRadius: 6,
    fontSize: 16,
    fontFamily: 'var(--font-family-sans)',
  },
  components: {
    Menu: {
      fontSize: 14,
    },
  },
}))
provide(IS_DARK_THEME, isDarkTheme)

function menuItem(key: string, label: string, to: string, icon: typeof LayoutDashboard) {
  return {
    key,
    label: h(RouterLink, { to }, { default: () => label }),
    icon: h(icon),
  }
}

const sideMenuOptions: MenuProps['items'] = [
  {
    type: 'group',
    key: 'runtime-group',
    label: '运行中心',
    children: [
      menuItem('overview', '运行总览', '/overview', LayoutDashboard),
      menuItem('positions', '持仓与订单', '/positions', WalletCards),
      menuItem('trades', '成交复盘', '/trades', ArrowLeftRight),
    ],
  },
  {
    type: 'group',
    key: 'analysis-group',
    label: '分析复盘',
    children: [
      menuItem('performance', '绩效分析', '/performance', ChartNoAxesCombined),
      menuItem('backtests', '回测复盘', '/backtests', FlaskConical),
    ],
  },
  {
    type: 'group',
    key: 'risk-group',
    label: '策略与风控',
    children: [menuItem('strategy-risk', '策略风控', '/strategy-risk', ShieldCheck)],
  },
  {
    type: 'group',
    key: 'data-group',
    label: '基础数据',
    children: [
      menuItem('universe', '交易对管理', '/universe', ListChecks),
      menuItem('categories', '分类管理', '/categories', Tags),
    ],
  },
  {
    type: 'group',
    key: 'system-group',
    label: '系统管理',
    children: [menuItem('notifications', '通知中心', '/notifications', Bell)],
  },
]

const GROUP_LABELS: Record<string, string> = {
  overview: '运行中心',
  positions: '运行中心',
  trades: '运行中心',
  performance: '分析复盘',
  backtests: '分析复盘',
  'strategy-risk': '策略与风控',
  universe: '基础数据',
  categories: '基础数据',
  notifications: '系统管理',
}

/**
 * 侧栏高亮项。
 *
 * 收益日历和 Campaign 明细是下钻页面，没有独立菜单入口，
 * 因此高亮它们的来源页，避免侧栏在这些路由上完全不选中。
 */
const activeKey = computed(() => {
  const name = String(route.name ?? '')
  if (name.startsWith('backtest')) return 'backtests'
  if (name.startsWith('notifications')) return 'notifications'
  if (name === 'campaign-trade-detail') return 'trades'
  if (name === 'calendar') return 'overview'
  return name
})

/**
 * 全局面包屑路径：固定在 Layout Header 左侧，
 * 一级页面显示「所属分组 / 页面名」，下钻页面自动生成可点击的层级链。
 */
const headerBreadcrumbs = computed<Array<{ label: string; to?: RouteLocationRaw }>>(() => {
  const name = String(route.name ?? '')
  const groupLabel = GROUP_LABELS[activeKey.value] ?? '控制台'
  const currentTitle = String(route.meta.title ?? '运行账本')
  const researchId = typeof route.params.researchId === 'string' ? route.params.researchId : ''
  const symbol = typeof route.params.symbol === 'string' ? route.params.symbol : ''

  if (name === 'calendar') {
    return [
      { label: groupLabel },
      { label: '运行总览', to: { path: '/overview', query: route.query } },
      { label: '收益日历' },
    ]
  }
  if (name === 'campaign-trade-detail') {
    return [
      { label: groupLabel },
      {
        label: '成交复盘',
        to: {
          path: '/trades',
          query: {
            account_id: route.query.account_id,
            strategy_id: route.query.strategy_id,
            symbol: route.query.symbol,
          },
        },
      },
      { label: String(route.query.symbol || 'Campaign 明细') },
    ]
  }
  if (name.startsWith('backtest') && name !== 'backtests') {
    const items: Array<{ label: string; to?: RouteLocationRaw }> = [
      { label: groupLabel },
      { label: '回测复盘', to: '/backtests' },
    ]
    if (name === 'backtest-reports') {
      items.push({ label: '分析报表' })
    } else if (name === 'backtest-report-detail') {
      items.push(
        { label: '分析报表', to: `/backtests/${encodeURIComponent(researchId)}/reports` },
        { label: String(route.params.reportType || '报表详情') },
      )
    } else if (name === 'backtest-symbols') {
      items.push({ label: '交易对数据' })
    } else if (name === 'backtest-trades') {
      items.push(
        { label: '交易对数据', to: `/backtests/${encodeURIComponent(researchId)}/symbols` },
        { label: symbol || '交易记录' },
      )
    } else if (name === 'backtest-equity') {
      items.push({ label: '收益曲线' })
    } else if (name === 'backtest-trade-replay') {
      if (route.query.from === 'equity') {
        items.push(
          { label: '收益曲线', to: `/backtests/${encodeURIComponent(researchId)}/equity` },
          { label: '单笔复盘' },
        )
      } else {
        items.push(
          { label: '交易对数据', to: `/backtests/${encodeURIComponent(researchId)}/symbols` },
          { label: '单笔复盘' },
        )
      }
    }
    return items
  }
  return [{ label: groupLabel }, { label: currentTitle }]
})

/** 徽标颜色与文案来自同一处，避免出现「文案异常但徽标常绿」。 */
const healthPresentation = computed(
  () =>
    ({
      healthy: { status: 'success' as const, label: '账本服务正常' },
      unhealthy: { status: 'error' as const, label: '账本服务异常' },
      unknown: { status: 'default' as const, label: '未探测' },
    })[health.status],
)
const healthCheckedAt = computed(() =>
  health.checkedAt ? `最近探测 ${formatLedgerClock(new Date(health.checkedAt))}` : '尚未探测',
)

function applyTheme(mode: 'light' | 'dark') {
  themeMode.value = mode
  document.documentElement.dataset.theme = mode
  writeStored(STORAGE_KEYS.theme, mode)
}
function toggleTheme() {
  applyTheme(isDarkTheme.value ? 'light' : 'dark')
}
function toggleSider() {
  siderCollapsed.value = !siderCollapsed.value
}

let healthTimer: ReturnType<typeof setInterval> | null = null
onMounted(() => {
  applyTheme(readStored(STORAGE_KEYS.theme) === 'dark' ? 'dark' : 'light')
  void health.check()
  healthTimer = setInterval(() => void health.check(), HEALTH_POLL_INTERVAL_MS)
})
onBeforeUnmount(() => {
  if (healthTimer !== null) clearInterval(healthTimer)
  healthTimer = null
})
</script>

<template>
  <a-config-provider :theme="providerTheme">
    <a-layout has-sider class="app-layout">
      <a-layout-sider
        v-model:collapsed="siderCollapsed"
        collapsible
        :trigger="null"
        breakpoint="md"
        :collapsed-width="56"
        class="app-sider"
      >
        <div class="brand">
          <span class="brand-mark">TL</span>
          <span class="brand-name">Trade Ledger</span>
        </div>
        <a-menu :selected-keys="[activeKey]" :items="sideMenuOptions" mode="inline" theme="dark" class="side-menu" />
      </a-layout-sider>
      <a-layout class="app-body">
        <a-layout-header class="app-header">
          <div class="header-actions">
            <div class="header-left">
              <a-tooltip :title="siderCollapsed ? '展开菜单' : '收起菜单'">
                <a-button
                  type="text"
                  shape="circle"
                  class="sider-toggle"
                  :aria-label="siderCollapsed ? '展开菜单' : '收起菜单'"
                  @click="toggleSider"
                >
                  <template #icon>
                    <PanelLeftOpen v-if="siderCollapsed" :size="17" />
                    <PanelLeftClose v-else :size="17" />
                  </template>
                </a-button>
              </a-tooltip>
              <a-breadcrumb class="header-breadcrumb">
                <a-breadcrumb-item v-for="crumb in headerBreadcrumbs" :key="crumb.label">
                  <RouterLink v-if="crumb.to" :to="crumb.to">{{ crumb.label }}</RouterLink>
                  <span v-else>{{ crumb.label }}</span>
                </a-breadcrumb-item>
              </a-breadcrumb>
            </div>
            <div class="header-right">
              <a-tooltip :title="healthCheckedAt" placement="bottom">
                <div class="header-health">
                  <a-badge :status="healthPresentation.status" />
                  <span>{{ healthPresentation.label }}</span>
                </div>
              </a-tooltip>
              <a-tooltip :title="isDarkTheme ? '切换浅色模式' : '切换深色模式'">
                <a-button
                  type="text"
                  shape="circle"
                  class="theme-toggle"
                  :aria-label="isDarkTheme ? '切换浅色模式' : '切换深色模式'"
                  @click="toggleTheme"
                >
                  <template #icon><Sun v-if="isDarkTheme" :size="17" /><Moon v-else :size="17" /></template>
                </a-button>
              </a-tooltip>
            </div>
          </div>
        </a-layout-header>
        <a-layout-content class="workspace">
          <RouterView v-slot="{ Component }">
            <KeepAlive :max="12">
              <component :is="Component" />
            </KeepAlive>
          </RouterView>
        </a-layout-content>
      </a-layout>
    </a-layout>
  </a-config-provider>
</template>

<style scoped lang="scss">
.app-sider {
  --sider-text-color: rgba(255, 255, 255, 0.72);
  --sider-muted-color: rgba(255, 255, 255, 0.48);
  --sider-border-color: rgba(255, 255, 255, 0.1);
  --menu-group-font-size: var(--type-meta);
  background: var(--sider-bg) !important;
  border-right: 1px solid var(--sider-border-color);
}
.app-sider :deep(.ant-menu-dark) {
  background: transparent;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 48px;
  padding: 0 16px;
  border-bottom: 1px solid var(--sider-border-color);
}
.app-layout {
  height: 100%;
  background: var(--app-bg);
}
.app-body {
  min-width: 0;
  min-height: 0;
}
.app-sider :deep(.ant-layout-sider-children) {
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.brand-mark {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--color-gold) 72%, transparent);
  background: color-mix(in srgb, var(--color-gold) 12%, transparent);
  color: color-mix(in srgb, var(--color-gold) 72%, white);
  font-weight: 700;
  font-size: var(--type-meta);
}
.brand-name {
  color: var(--sider-text-color);
  font-size: var(--type-primary);
  font-weight: 600;
  white-space: nowrap;
}
.app-sider.ant-layout-sider-collapsed .brand {
  justify-content: center;
  padding-inline: 0;
}
.app-sider.ant-layout-sider-collapsed .brand-name {
  display: none;
}
.app-header {
  height: 48px;
  line-height: 48px;
  padding: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}
.header-actions {
  display: flex;
  height: 100%;
  align-items: center;
  justify-content: space-between;
  padding-inline: 12px 16px;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}
.header-health {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: var(--type-meta);
}
.sider-toggle,
.theme-toggle {
  color: var(--text);
}
.workspace {
  flex: 1;
  min-height: 0;
  padding: 16px 24px 32px;
  overflow: auto;
}
.side-menu :deep(.ant-menu-item-group-title) {
  padding-top: 16px;
  padding-bottom: 4px;
  color: var(--sider-muted-color);
  font: var(--menu-group-font-size) var(--font-family-mono);
  letter-spacing: 0;
}
.app-sider.ant-layout-sider-collapsed .side-menu :deep(.ant-menu-item-group-title) {
  height: var(--menu-group-font-size);
  padding: 8px 0 0;
  overflow: hidden;
  color: transparent;
}
@media (max-width: 900px) {
  /* 原先由 base.scss 用 !important 覆盖，现在归组件自己管。 */
  .workspace {
    padding: 14px;
  }
}
@media (max-width: 640px) {
  .header-health span {
    display: none;
  }
}
@media (max-width: 767px) {
  .sider-toggle {
    display: none;
  }
  .app-sider {
    flex: 0 0 56px !important;
    width: 56px !important;
    min-width: 56px !important;
    max-width: 56px !important;
  }
  .app-sider .brand {
    justify-content: center;
    padding-inline: 0;
  }
  .app-sider .brand-name,
  .app-sider .side-menu :deep(.ant-menu-title-content),
  .app-sider .side-menu :deep(.ant-menu-item-group-title) {
    display: none;
  }
  .app-sider .side-menu :deep(.ant-menu-item) {
    padding-inline: 20px !important;
  }
}
</style>
