<script setup lang="ts">
import { computed, h, onMounted } from 'vue'
import { FlaskConical } from 'lucide-vue-next'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import type { MenuProps } from 'ant-design-vue'
import { useHealthStore } from '@/stores/health'

const route = useRoute()
const health = useHealthStore()

const menuOptions: MenuProps['items'] = [
  { key: 'overview', label: '运行总览', to: '/overview' },
  { key: 'calendar', label: '收益日历', to: '/calendar' },
  { key: 'positions', label: '持仓', to: '/positions' },
  { key: 'trades', label: '成交与买卖点', to: '/trades' },
  { key: 'stats', label: '胜率与盈亏比', to: '/stats' },
  { key: 'symbols', label: '交易对统计', to: '/symbols' },
  { key: 'backtests', label: '回测复盘', to: '/backtests', icon: FlaskConical },
  { key: 'universe', label: '交易对管理', to: '/universe' },
  { key: 'admissions', label: 'Subcategory 管理', to: '/admissions' }
].map((item) => ({
  key: item.key,
  label: h(RouterLink, { to: item.to }, { default: () => item.label }),
  icon: item.icon ? h(item.icon) : undefined
}))

const activeKey = computed(() => String(route.name ?? '').startsWith('backtest') ? 'backtests' : String(route.name ?? ''))
const title = computed(() => String(route.meta.title ?? ''))
const healthLabel = computed(
  () =>
    ({
      healthy: '账本服务正常',
      unhealthy: '账本服务异常',
      unknown: '未探测'
    })[health.status]
)

onMounted(health.check)
</script>

<template>
  <a-layout has-sider class="app-layout">
      <a-layout-sider collapsible :width="220" :collapsed-width="56">
        <div class="brand">
          <span class="brand-mark">TL</span>
          <span class="brand-name">Trade Ledger</span>
        </div>
        <a-menu :selected-keys="[activeKey]" :items="menuOptions" mode="inline" />
        <div class="rail-status">
          <a-badge status="processing" />
          <span>{{ healthLabel }}</span>
        </div>
      </a-layout-sider>
      <a-layout>
        <a-layout-header class="topbar">
          <h1>{{ title }}</h1>
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
</template>

<style scoped lang="scss">
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 56px;
  padding: 0 18px;
  border-bottom: 1px solid var(--line);
}
.app-layout,
.app-layout > :deep(.ant-layout) {
  min-height: 100%;
}
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
  font-size: 15px;
  font-weight: 600;
}
.rail-status {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: auto;
  padding: 16px 18px;
  border-top: 1px solid var(--line);
  font-size: 12px;
}
.topbar {
  display: flex;
  align-items: center;
  height: 56px;
  padding: 0 24px;
  background: #fff;
  border-bottom: 1px solid var(--line);
}
.topbar {
  h1 { margin: 0; font-size: 17px; font-weight: 600; }
}
.workspace {
  flex: 1;
  min-height: 0;
  padding: 20px 24px 32px;
  overflow: auto;
}
</style>
