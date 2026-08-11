<script setup lang="ts">
import { computed, h, onMounted } from 'vue'
import { ArrowLeftRight, CalendarDays, ChartNoAxesCombined, Coins, FlaskConical, LayoutDashboard, ListChecks, Tags, WalletCards } from 'lucide-vue-next'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import type { MenuProps } from 'ant-design-vue'
import { useHealthStore } from '@/stores/health'

const route = useRoute()
const health = useHealthStore()

const sideMenuOptions: MenuProps['items'] = [
  { key: 'overview', label: '运行总览', to: '/overview', icon: LayoutDashboard },
  { key: 'calendar', label: '收益日历', to: '/calendar', icon: CalendarDays },
  { key: 'positions', label: '持仓', to: '/positions', icon: WalletCards },
  { key: 'trades', label: '成交与买卖点', to: '/trades', icon: ArrowLeftRight },
  { key: 'stats', label: '胜率与盈亏比', to: '/stats', icon: ChartNoAxesCombined },
  { key: 'symbols', label: '交易对统计', to: '/symbols', icon: Coins },
  { key: 'backtests', label: '回测复盘', to: '/backtests', icon: FlaskConical },
  { key: 'universe', label: '交易对管理', to: '/universe', icon: ListChecks },
  { key: 'admissions', label: 'Subcategory 管理', to: '/admissions', icon: Tags }
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

onMounted(health.check)
</script>

<template>
  <a-layout has-sider class="app-layout">
      <a-layout-sider collapsible breakpoint="md" theme="light" class="app-sider">
        <div class="brand">
          <span class="brand-mark">TL</span>
          <span class="brand-name">Trade Ledger</span>
        </div>
        <a-menu :selected-keys="[activeKey]" :items="sideMenuOptions" mode="inline" class="side-menu" />
        <div class="rail-status">
          <a-badge status="processing" />
          <span>{{ healthLabel }}</span>
        </div>
      </a-layout-sider>
      <a-layout class="app-body">
        <a-layout-header class="app-header" />
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
  height: 64px;
  padding: 0 16px;
  border-bottom: 1px solid var(--line);
}
.app-layout { height: 100%; }
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
  border-top: 1px solid var(--line);
  font-size: 12px;
}
.app-header {
  padding: 0;
  background: #fff;
  border-bottom: 1px solid var(--line);
}
.workspace {
  flex: 1;
  min-height: 0;
  padding: 20px 24px 32px;
  overflow: auto;
}
</style>
