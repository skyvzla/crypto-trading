<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, provide, ref } from 'vue'
import {
  ArrowLeftRight,
  Bell,
  ChartNoAxesCombined,
  FlaskConical,
  Gauge,
  LayoutDashboard,
  ListChecks,
  Moon,
  ShieldCheck,
  Sun,
  Tags,
  WalletCards,
} from 'lucide-vue-next'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { theme as antdTheme, type MenuProps } from 'ant-design-vue'
import { useHealthStore } from '@/stores/health'
import { IS_DARK_THEME } from '@/shared/theme'
import { STORAGE_KEYS, readStored, writeStored } from '@/shared/storage'
import { formatLedgerClock } from '@/shared/time'

/** 账本连通性探测间隔。够快能发现服务中断，又不至于把 /health 打满。 */
const HEALTH_POLL_INTERVAL_MS = 30_000

const route = useRoute()
const health = useHealthStore()
const themeMode = ref<'light' | 'dark'>('light')
const isDarkTheme = computed(() => themeMode.value === 'dark')
const providerTheme = computed(() => ({
  algorithm: isDarkTheme.value ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
  token: {
    colorPrimary: '#3b82f6',
    colorInfo: '#2563eb',
    colorSuccess: '#059669',
    colorWarning: '#d97706',
    colorError: '#dc2626',
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
const pageTitle = computed(() => String(route.meta.title ?? '运行账本'))

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
      <a-layout-sider collapsible breakpoint="md" :collapsed-width="56" class="app-sider">
        <div class="brand">
          <span class="brand-mark">TL</span>
          <span class="brand-name">Trade Ledger</span>
        </div>
        <a-menu :selected-keys="[activeKey]" :items="sideMenuOptions" mode="inline" theme="dark" class="side-menu" />
        <a-tooltip :title="healthCheckedAt" placement="right">
          <div class="rail-status">
            <a-badge :status="healthPresentation.status" />
            <span>{{ healthPresentation.label }}</span>
          </div>
        </a-tooltip>
      </a-layout-sider>
      <a-layout class="app-body">
        <a-layout-header class="app-header">
          <div class="header-actions">
            <div class="header-context">
              <Gauge :size="15" />
              <span>{{ pageTitle }}</span>
              <i>LEDGER CONSOLE</i>
            </div>
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
  --sider-text-color: rgba(255, 255, 255, 0.65);
  --sider-border-color: rgba(255, 255, 255, 0.12);
  --menu-group-font-size: var(--type-meta);
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 64px;
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
  width: 30px;
  height: 30px;
  border-radius: 6px;
  border: 1px solid color-mix(in srgb, var(--color-gold) 72%, transparent);
  background: color-mix(in srgb, var(--color-gold) 12%, transparent);
  color: color-mix(in srgb, var(--color-gold) 72%, white);
  font-weight: 700;
  font-size: var(--type-secondary);
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
.app-sider.ant-layout-sider-collapsed .brand-name,
.app-sider.ant-layout-sider-collapsed .rail-status span {
  display: none;
}
.rail-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: auto;
  padding: 16px 18px;
  border-top: 1px solid var(--sider-border-color);
  color: var(--sider-text-color);
  font-size: var(--type-secondary);
}
.app-header {
  padding: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}
.header-actions {
  display: flex;
  height: 100%;
  align-items: center;
  justify-content: space-between;
  padding-inline: 20px;
}
.header-context {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: var(--type-secondary);
}
.header-context span {
  color: var(--text);
  font-weight: 600;
}
.header-context i {
  padding-left: 8px;
  border-left: 1px solid var(--line);
  color: var(--color-gold);
  font: var(--type-meta) var(--font-family-mono);
  font-style: normal;
  letter-spacing: 0;
}
.theme-toggle {
  color: var(--text);
}
.workspace {
  flex: 1;
  min-height: 0;
  padding: 20px 24px 32px;
  overflow: auto;
}
.side-menu :deep(.ant-menu-item-group-title) {
  padding-top: 17px;
  padding-bottom: 5px;
  color: color-mix(in srgb, var(--color-gold) 68%, transparent);
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
  .header-context i {
    display: none;
  }
}
@media (max-width: 767px) {
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
  .app-sider .rail-status span,
  .app-sider .side-menu :deep(.ant-menu-title-content),
  .app-sider .side-menu :deep(.ant-menu-item-group-title) {
    display: none;
  }
  .app-sider .side-menu :deep(.ant-menu-item) {
    padding-inline: 20px !important;
  }
  .app-sider :deep(.ant-layout-sider-trigger) {
    display: none;
  }
  .rail-status {
    justify-content: center;
    padding-inline: 0;
  }
}
</style>
