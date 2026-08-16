<script setup lang="ts">
import { computed, h, onMounted, provide, ref } from 'vue'
import { ArrowLeftRight, ChartNoAxesCombined, FlaskConical, Gauge, LayoutDashboard, ListChecks, Moon, ShieldCheck, Sun, Tags, WalletCards } from 'lucide-vue-next'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { theme as antdTheme, type MenuProps } from 'ant-design-vue'
import { useHealthStore } from '@/stores/health'

const route = useRoute()
const health = useHealthStore()
const themeMode = ref<'light' | 'dark'>('light')
const isDarkTheme = computed(() => themeMode.value === 'dark')
const providerTheme = computed(() => ({
  algorithm: isDarkTheme.value ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
  token: { colorPrimary: '#3b82f6', borderRadius: 6, fontFamily: '"IBM Plex Sans", "Noto Sans SC", sans-serif' }
}))
provide('isDarkTheme', isDarkTheme)

function menuItem(key: string, label: string, to: string, icon: typeof LayoutDashboard) {
  return {
    key,
    label: h(RouterLink, { to }, { default: () => label }),
    icon: h(icon)
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
      menuItem('trades', '成交复盘', '/trades', ArrowLeftRight)
    ]
  },
  {
    type: 'group',
    key: 'analysis-group',
    label: '分析复盘',
    children: [
      menuItem('performance', '绩效分析', '/performance', ChartNoAxesCombined),
      menuItem('backtests', '回测复盘', '/backtests', FlaskConical)
    ]
  },
  {
    type: 'group',
    key: 'risk-group',
    label: '策略与风控',
    children: [menuItem('strategy-risk', '策略风控', '/strategy-risk', ShieldCheck)]
  },
  {
    type: 'group',
    key: 'data-group',
    label: '基础数据',
    children: [
      menuItem('universe', '交易对管理', '/universe', ListChecks),
      menuItem('categories', '分类管理', '/categories', Tags)
    ]
  }
]

const activeKey = computed(() => String(route.name ?? '').startsWith('backtest') ? 'backtests' : String(route.name ?? ''))
const pageTitle = computed(() => String(route.meta.title ?? '运行账本'))
const healthLabel = computed(
  () =>
    ({
      healthy: '账本服务正常',
      unhealthy: '账本服务异常',
      unknown: '未探测'
    })[health.status]
)

function applyTheme(mode: 'light' | 'dark') {
  themeMode.value = mode
  document.documentElement.dataset.theme = mode
  try {
    localStorage.setItem('trade-ledger-theme', mode)
  } catch {
    // 浏览器禁用本地存储时，仍保留本次会话的主题选择。
  }
}
function toggleTheme() { applyTheme(isDarkTheme.value ? 'light' : 'dark') }
onMounted(() => {
  try {
    applyTheme(localStorage.getItem('trade-ledger-theme') === 'dark' ? 'dark' : 'light')
  } catch {
    applyTheme('light')
  }
  health.check()
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
        <div class="rail-status">
          <a-badge status="processing" />
          <span>{{ healthLabel }}</span>
        </div>
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
              <a-button type="text" shape="circle" class="theme-toggle" :aria-label="isDarkTheme ? '切换浅色模式' : '切换深色模式'" @click="toggleTheme">
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
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 64px;
  padding: 0 16px;
  border-bottom: 1px solid var(--sider-border-color);
}
.app-layout { height: 100%; background: var(--app-bg); }
.app-body { min-width: 0; min-height: 0; }
.app-sider :deep(.ant-layout-sider-children) { display: flex; flex-direction: column; min-height: 0; }
.brand-mark {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 6px;
  border: 1px solid rgba(214, 168, 75, .72);
  background: rgba(214, 168, 75, .12);
  color: #e1ba68;
  font-weight: 700;
  font-size: 13px;
}
.brand-name {
  color: var(--sider-text-color);
  font-size: 15px;
  font-weight: 600;
  white-space: nowrap;
}
.app-sider.ant-layout-sider-collapsed .brand { justify-content: center; padding-inline: 0; }
.app-sider.ant-layout-sider-collapsed .brand-name,
.app-sider.ant-layout-sider-collapsed .rail-status span { display: none; }
.rail-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: auto;
  padding: 16px 18px;
  border-top: 1px solid var(--sider-border-color);
  color: var(--sider-text-color);
  font-size: 12px;
}
.app-header {
  padding: 0;
  background: var(--surface);
  border-bottom: 1px solid var(--line);
}
.header-actions { display: flex; height: 100%; align-items: center; justify-content: space-between; padding-inline: 20px; }
.header-context { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
.header-context span { color: var(--text); font-weight: 600; }
.header-context i { padding-left: 8px; border-left: 1px solid var(--line); color: #b38732; font: 9px "IBM Plex Mono", monospace; font-style: normal; letter-spacing: .08em; }
.theme-toggle { color: var(--text); }
.workspace {
  flex: 1;
  min-height: 0;
  padding: 20px 24px 32px;
  overflow: auto;
}
.side-menu :deep(.ant-menu-item-group-title) { padding-top: 17px; padding-bottom: 5px; color: rgba(214, 168, 75, .68); font: 9px "IBM Plex Mono", monospace; letter-spacing: .12em; }
.app-sider.ant-layout-sider-collapsed .side-menu :deep(.ant-menu-item-group-title) { height: 9px; padding: 8px 0 0; overflow: hidden; color: transparent; }
@media (max-width: 640px) {
  .header-context i { display: none; }
  .workspace { padding-inline: 12px; }
}
</style>
