<script setup lang="ts">
import { Activity, ArrowRight, CircleDot, Globe2, MessageCircle, Plus, SlidersHorizontal } from 'lucide-vue-next'
import type { NotificationEvent, NotificationOverview, Page } from '@/api/types'
import MetricTile from '@/features/operations/MetricTile.vue'
import { formatShortTime, statusBadge, statusLabel } from './presentation'
import NotificationSectionHeader from './NotificationSectionHeader.vue'

defineProps<{
  overview: NotificationOverview
  enabledEndpointCount: number
  retryDeliveryCount: number
  deadDeliveryCount: number
  events: Page<NotificationEvent>
}>()

const emit = defineEmits<{
  'open-activity': [view: 'events' | 'deliveries']
  'new-connector': [type?: 'telegram' | 'webhook']
  'new-policy': []
}>()
</script>

<template>
  <section class="view-panel overview-view" aria-labelledby="overview-heading">
    <NotificationSectionHeader id="overview-heading" kicker="CONTROL ROOM" title="概览" note="配置变更即时生效，历史投递保留原始快照" />

    <div class="metric-grid notification-metrics">
      <MetricTile label="启用连接器" :value="`${overview.enabled_connectors}/${overview.connectors}`" hint="Telegram / Webhook" />
      <MetricTile label="活跃端点" :value="`${enabledEndpointCount}/${overview.endpoints}`" hint="独立地址隔离" />
      <MetricTile label="路由策略" :value="String(overview.policies)" :hint="`${overview.groups} 个职责组`" />
      <MetricTile label="待处理投递" :value="String(retryDeliveryCount)" :hint="deadDeliveryCount ? `${deadDeliveryCount} 条死信` : '当前无死信'" :tone="retryDeliveryCount > 0 ? 'warning' : 'neutral'" />
    </div>

    <div class="overview-grid">
      <section class="data-card health-panel">
        <div class="data-card-heading"><div><CircleDot :size="16" class="panel-icon" /><h3>投递状态</h3></div><span class="quiet-value">近期开关</span></div>
        <div class="health-list">
          <div><a-badge status="success" text="已发送" /><strong>{{ overview.deliveries.sent }}</strong></div>
          <div><a-badge status="processing" text="待发送" /><strong>{{ overview.deliveries.pending }}</strong></div>
          <div><a-badge status="warning" text="重试中" /><strong>{{ overview.deliveries.retry }}</strong></div>
          <div><a-badge status="error" text="死信" /><strong>{{ overview.deliveries.dead }}</strong></div>
        </div>
        <a-button type="link" class="panel-link" @click="emit('open-activity', 'deliveries')">查看投递队列 <ArrowRight :size="14" /></a-button>
      </section>
      <section class="data-card quick-panel">
        <div class="data-card-heading"><div><SlidersHorizontal :size="16" class="panel-icon" /><h3>快速配置</h3></div><span class="quiet-value">常用动作</span></div>
        <button type="button" class="quick-action" @click="emit('new-connector', 'telegram')"><span class="quick-mark telegram"><MessageCircle :size="15" /></span><span><strong>添加 Telegram Bot</strong><small>一个 Bot 可承载多个群组 / Topic</small></span><span class="quick-arrow"><Plus :size="16" /></span></button>
        <button type="button" class="quick-action" @click="emit('new-connector', 'webhook')"><span class="quick-mark webhook"><Globe2 :size="15" /></span><span><strong>添加 Webhook</strong><small>同一鉴权配置可挂多个 URL</small></span><span class="quick-arrow"><Plus :size="16" /></span></button>
        <button type="button" class="quick-action" @click="emit('new-policy')"><span class="quick-mark policy"><SlidersHorizontal :size="15" /></span><span><strong>新建路由策略</strong><small>按事件模式和重要级别选择职责组</small></span><span class="quick-arrow"><Plus :size="16" /></span></button>
      </section>
    </div>

    <section class="data-card recent-panel">
      <div class="data-card-heading"><div><Activity :size="16" class="panel-icon" /><h3>最近事件</h3></div><a-button type="link" class="panel-link" @click="emit('open-activity', 'events')">全部事件 <ArrowRight :size="14" /></a-button></div>
      <div v-if="events.items.length" class="event-list">
        <div v-for="event in events.items.slice(0, 5)" :key="event.id" class="event-row"><span class="event-severity"><a-badge :status="statusBadge(event.severity)" /></span><div class="event-copy"><strong>{{ event.title }}</strong><small>{{ event.event_type }} · {{ event.source }}</small></div><a-badge :status="statusBadge(event.routing_status)" :text="statusLabel(event.routing_status)" /><time>{{ formatShortTime(event.occurred_at) }}</time></div>
      </div>
      <a-empty v-else description="暂无通知事件" />
    </section>
  </section>
</template>

<style scoped lang="scss">
.view-panel { min-width: 0; }
.data-card-heading > div { display: flex; align-items: center; gap: 7px; min-width: 0; }
.data-card-heading h3 { margin: 0; font-size: var(--font-size-md); letter-spacing: 0; }
.panel-icon { color: var(--color-primary); }
.quiet-value { color: var(--muted); font-size: var(--font-size-xs); }
.notification-metrics { margin-bottom: 14px; }
.overview-grid { display: grid; grid-template-columns: minmax(0, .9fr) minmax(0, 1.1fr); gap: 14px; margin-bottom: 14px; }
.health-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 4px 13px 7px; }
.health-list > div { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 9px 0; border-bottom: 1px solid color-mix(in srgb, var(--muted) 16%, transparent); color: var(--muted); font-size: var(--font-size-sm); }
.health-list > div:nth-last-child(-n+2) { border-bottom: 0; }
.health-list strong { color: var(--text); font: var(--font-size-sm) var(--font-family-mono); }
.health-list span { display: inline-flex; align-items: center; gap: 7px; }
.panel-link { display: inline-flex; align-items: center; gap: 5px; }
.health-panel > .panel-link { padding: 6px 13px 12px; }
.quick-panel { padding-bottom: 3px; }
.quick-action { display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 9px; width: 100%; min-height: 50px; padding: 7px 13px; border: 0; border-bottom: 1px solid color-mix(in srgb, var(--muted) 15%, transparent); color: var(--text); background: transparent; text-align: left; cursor: pointer; }
.quick-action:last-child { border-bottom: 0; }
.quick-action:hover { background: var(--surface-hover); }
.quick-action > span:nth-child(2) { min-width: 0; }
.quick-action strong, .quick-action small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.quick-action strong { font-size: var(--font-size-sm); }
.quick-action small { margin-top: 2px; color: var(--muted); font-size: var(--font-size-xs); }
.quick-mark { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 6px; }
.quick-mark.telegram { color: var(--color-info); background: color-mix(in srgb, var(--color-info) 10%, transparent); }
.quick-mark.policy { color: var(--color-warning); background: color-mix(in srgb, var(--color-warning) 10%, transparent); }
.quick-mark.webhook { color: #0f766e; background: rgba(13, 148, 136, .1); }
:root[data-theme='dark'] .quick-mark.webhook { color: #5eead4; }
.quick-arrow { display: inline-flex; color: var(--muted); }
.recent-panel { overflow: hidden; }
.event-list { padding: 3px 13px; }
.event-row { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto 128px; align-items: center; gap: 9px; min-height: 47px; border-bottom: 1px solid color-mix(in srgb, var(--muted) 15%, transparent); }
.event-row:last-child { border-bottom: 0; }
.event-severity { display: grid; place-items: center; }
.event-copy { min-width: 0; }
.event-copy strong, .event-copy small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.event-copy strong { font-size: var(--font-size-sm); }
.event-copy small { margin-top: 2px; color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); }
.event-row time { color: var(--muted); font: var(--font-size-xs) var(--font-family-mono); text-align: right; white-space: nowrap; }

@media (max-width: 900px) {
  .overview-grid { grid-template-columns: 1fr; }
  .event-row { grid-template-columns: 12px minmax(0, 1fr) auto; }
  .event-row time { display: none; }
}
@media (max-width: 600px) {
  .health-list { grid-template-columns: 1fr; }
  .health-list > div:nth-last-child(-n+2) { border-bottom: 1px solid color-mix(in srgb, var(--muted) 16%, transparent); }
  .health-list > div:last-child { border-bottom: 0; }
}
</style>
