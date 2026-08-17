<script setup lang="ts">
import { computed, h, onMounted, provide, ref } from 'vue'
import { ArrowLeftRight, Bell, CalendarDays, ChartNoAxesCombined, Coins, FlaskConical, LayoutDashboard, ListChecks, Moon, Sun, Tags, WalletCards } from 'lucide-vue-next'
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

const sideMenuOptions: MenuProps['items'] = [
  { key: 'overview', label: '运行总览', to: '/overview', icon: LayoutDashboard },
  { key: 'calendar', label: '收益日历', to: '/calendar', icon: CalendarDays },
  { key: 'positions', label: '持仓', to: '/positions', icon: WalletCards },
  { key: 'trades', label: '成交与买卖点', to: '/trades', icon: ArrowLeftRight },
  { key: 'stats', label: '胜率与盈亏比', to: '/stats', icon: ChartNoAxesCombined },
  { key: 'symbols', label: '交易对统计', to: '/symbols', icon: Coins },
  { key: 'backtests', label: '回测复盘', to: '/backtests', icon: FlaskConical },
  { key: 'universe', label: '交易对管理', to: '/universe', icon: ListChecks },
  { key: 'admissions', label: 'Subcategory 管理', to: '/admissions', icon: Tags },
  { key: 'notifications', label: '通知中心', to: '/notifications', icon: Bell }
].map((item) => ({
  key: item.key,
  label: h(RouterLink, { to: item.to }, { default: () => item.label }),
  icon: item.icon ? h(item.icon) : undefined
}))

const activeKey = computed(() => String(route.name ?? '').startsWith('backtest') ? 'backtests' : String(route.name ?? ''))
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
      <a-layout-sider collapsible breakpoint="md" class="app-sider">
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
  background: var(--primary);
  color: #fff;
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
.header-actions { display: flex; height: 100%; align-items: center; justify-content: flex-end; padding-inline: 20px; }
.theme-toggle { color: var(--text); }
.workspace {
  flex: 1;
  min-height: 0;
  padding: 20px 24px 32px;
  overflow: auto;
}
</style>
