<script setup lang="ts">
import { Activity, CircleDot, Globe2, MessageCircle, SlidersHorizontal } from 'lucide-vue-next'
import type { NotificationEvent, NotificationOverview, Page } from '@/api/types'

defineProps<{
  overview: NotificationOverview
  enabledEndpointCount: number
  retryDeliveryCount: number
  deadDeliveryCount: number
  events: Page<NotificationEvent>
  formatDate: (value: string | null | undefined) => string
  statusLabel: (value: string) => string
  statusBadge: (value: string) => 'success' | 'processing' | 'warning' | 'error' | 'default'
}>()

const emit = defineEmits<{
  'open-activity': [view: 'events' | 'deliveries']
  'new-connector': [type?: 'telegram' | 'webhook']
  'new-policy': []
}>()
</script>

<template>
  <section class="view-panel overview-view" aria-labelledby="overview-heading">
    <div class="section-heading">
      <div>
        <span class="section-kicker">CONTROL ROOM</span>
        <h2 id="overview-heading">概览</h2>
      </div>
      <span class="section-note">配置变更即时生效，历史投递保留原始快照</span>
    </div>

    <div class="metric-strip">
      <div class="metric-item"><span>启用连接器</span><strong>{{ overview.enabled_connectors }}<small>/{{ overview.connectors }}</small></strong><em>Telegram / Webhook</em></div>
      <div class="metric-item"><span>活跃端点</span><strong>{{ enabledEndpointCount }}<small>/{{ overview.endpoints }}</small></strong><em>独立地址隔离</em></div>
      <div class="metric-item"><span>路由策略</span><strong>{{ overview.policies }}</strong><em>{{ overview.groups }} 个职责组</em></div>
      <div class="metric-item" :class="{ 'metric-warning': retryDeliveryCount > 0 }"><span>待处理投递</span><strong>{{ retryDeliveryCount }}</strong><em>{{ deadDeliveryCount ? `${deadDeliveryCount} 条死信` : '当前无死信' }}</em></div>
    </div>

    <div class="overview-grid">
      <section class="surface-panel health-panel">
        <div class="panel-heading"><div><CircleDot :size="16" class="panel-icon" /><h3>投递状态</h3></div><span class="quiet-value">近期开关</span></div>
        <div class="health-list">
          <div><a-badge status="success" text="已发送" /><strong>{{ overview.deliveries.sent }}</strong></div>
          <div><a-badge status="processing" text="待发送" /><strong>{{ overview.deliveries.pending }}</strong></div>
          <div><a-badge status="warning" text="重试中" /><strong>{{ overview.deliveries.retry }}</strong></div>
          <div><a-badge status="error" text="死信" /><strong>{{ overview.deliveries.dead }}</strong></div>
        </div>
        <a-button type="link" class="panel-link" @click="emit('open-activity', 'deliveries')">查看投递队列 <span>→</span></a-button>
      </section>
      <section class="surface-panel quick-panel">
        <div class="panel-heading"><div><SlidersHorizontal :size="16" class="panel-icon" /><h3>快速配置</h3></div><span class="quiet-value">常用动作</span></div>
        <button type="button" class="quick-action" @click="emit('new-connector', 'telegram')"><span class="quick-mark telegram"><MessageCircle :size="15" /></span><span><strong>添加 Telegram Bot</strong><small>一个 Bot 可承载多个群组 / Topic</small></span><span class="quick-arrow">＋</span></button>
        <button type="button" class="quick-action" @click="emit('new-connector', 'webhook')"><span class="quick-mark webhook"><Globe2 :size="15" /></span><span><strong>添加 Webhook</strong><small>同一鉴权配置可挂多个 URL</small></span><span class="quick-arrow">＋</span></button>
        <button type="button" class="quick-action" @click="emit('new-policy')"><span class="quick-mark policy"><SlidersHorizontal :size="15" /></span><span><strong>新建路由策略</strong><small>按事件模式和重要级别选择职责组</small></span><span class="quick-arrow">＋</span></button>
      </section>
    </div>

    <section class="surface-panel recent-panel">
      <div class="panel-heading"><div><Activity :size="16" class="panel-icon" /><h3>最近事件</h3></div><a-button type="link" class="panel-link" @click="emit('open-activity', 'events')">全部事件 <span>→</span></a-button></div>
      <div v-if="events.items.length" class="event-list">
        <div v-for="event in events.items.slice(0, 5)" :key="event.id" class="event-row"><span class="event-severity"><a-badge :status="statusBadge(event.severity)" /></span><div class="event-copy"><strong>{{ event.title }}</strong><small>{{ event.event_type }} · {{ event.source }}</small></div><a-badge :status="statusBadge(event.routing_status)" :text="statusLabel(event.routing_status)" /><time>{{ formatDate(event.occurred_at) }}</time></div>
      </div>
      <a-empty v-else description="暂无通知事件" />
    </section>
  </section>
</template>
