<script setup lang="ts">
import { computed, h } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { useRoute } from 'vue-router'
import { Tag, type TableColumnsType } from 'ant-design-vue'
import { backtestApi } from '@/api/backtests'
import type { BacktestFill, BacktestOrder } from '@/api/types'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import BacktestEventDetails from './components/BacktestEventDetails.vue'
import JsonDetails from '@/features/backtests/JsonDetails.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import TradeReplayChartPanel from '@/features/backtests/TradeReplayChartPanel.vue'
import { formatDateTime, formatNumber, formatPercent, pnlClass } from '@/shared/format'
import { eventDisplayName, resolvePricePrecision } from './components/eventPresentation'

const route = useRoute()
const researchId = computed(() => (typeof route.params.researchId === 'string' ? route.params.researchId : ''))
const tradeId = computed(() => (typeof route.params.tradeId === 'string' ? route.params.tradeId : ''))

const routeReady = computed(() => Boolean(researchId.value && tradeId.value))
const tradeQuery = useQuery({
  queryKey: computed(() => ['backtest-trade', researchId.value, tradeId.value]),
  queryFn: () => backtestApi.trade(researchId.value, tradeId.value),
  enabled: routeReady,
})
const eventsQuery = useQuery({
  queryKey: computed(() => ['backtest-events', researchId.value, tradeId.value]),
  queryFn: () => backtestApi.events(researchId.value, tradeId.value),
  enabled: routeReady,
})
const strategyId = computed(() => tradeQuery.data.value?.strategy_id || '')
const schemaQuery = useQuery({
  queryKey: computed(() => ['backtest-strategy-schema', strategyId.value]),
  queryFn: () => backtestApi.strategySchema(strategyId.value),
  enabled: computed(() => Boolean(strategyId.value)),
})

const symbolPricePrecision = computed(() => {
  const trade = tradeQuery.data.value
  if (!trade) return 2
  const samplePrices: Array<number | string | null | undefined> = [
    trade.entry_price,
    trade.average_entry_price,
    trade.signal_price,
    trade.invalid_price,
    trade.exit_price,
  ]
  if (Array.isArray(trade.tier_prices)) {
    samplePrices.push(...trade.tier_prices)
  }
  return resolvePricePrecision(trade.symbol, samplePrices)
})

const allAttributes = computed(() => ({
  ...(tradeQuery.data.value?.parameters || {}),
  ...(tradeQuery.data.value?.strategy_data || {}),
  ...(tradeQuery.data.value?.metrics || {}),
  ...(tradeQuery.data.value?.attributes || {}),
}))
const orders = computed(() => tradeQuery.data.value?.orders || [])
const fills = computed(() => tradeQuery.data.value?.fills || [])
function orderKey(order: BacktestOrder): string {
  return order.order_id || order.id
}
function orderFills(order: BacktestOrder): BacktestFill[] {
  const key = orderKey(order)
  return fills.value.filter((fill) => fill.order_id === key)
}
function orderSideLabel(side: string | null | undefined): string {
  const normalized = side?.toUpperCase()
  if (normalized === 'BUY') return '买'
  if (normalized === 'SELL') return '卖'
  return side || '-'
}
function orderModeLabel(order: BacktestOrder): string {
  if (order.reduce_only === true) return '只减仓'
  if (order.reduce_only === false) return '开仓'
  return '-'
}
function orderTypeLabel(order: BacktestOrder): string {
  return order.order_type || order.type || '-'
}
function orderCompletedTime(order: BacktestOrder): string | number | null {
  if (order.completed_time !== undefined && order.completed_time !== null) {
    return order.completed_time
  }
  if (order.status?.toUpperCase() === 'FILLED') {
    return order.fill_time || null
  }
  return order.cancel_time || order.fill_time || null
}
function fillTime(fill: BacktestFill): string | number | null {
  return fill.time || fill.fill_time || null
}
function fillMakerLabel(fill: BacktestFill): string {
  if (fill.is_maker === true) return 'Maker'
  if (fill.is_maker === false) return 'Taker'
  return '-'
}
const orderColumns: TableColumnsType<BacktestOrder> = [
  {
    title: '订单 ID',
    key: 'order_id',
    width: 220,
    customRender: ({ record }) => orderKey(record),
  },
  {
    title: '创建时间',
    key: 'created_time',
    width: 180,
    customRender: ({ record }) => formatDateTime(record.created_time || record.created_at),
  },
  {
    title: '方向',
    key: 'side',
    width: 80,
    customRender: ({ record }) => orderSideLabel(record.side),
  },
  {
    title: '开仓/只减仓',
    key: 'reduce_only',
    width: 90,
    customRender: ({ record }) => orderModeLabel(record),
  },
  {
    title: '类型',
    key: 'type',
    width: 100,
    customRender: ({ record }) => orderTypeLabel(record),
  },
  {
    title: '委托价',
    key: 'price',
    width: 120,
    customRender: ({ record }) => formatNumber(record.price, symbolPricePrecision.value),
  },
  {
    title: '委托数量',
    key: 'quantity',
    width: 120,
    customRender: ({ record }) => formatNumber(record.quantity, 8),
  },
  {
    title: '成交数量',
    key: 'filled_quantity',
    width: 120,
    customRender: ({ record }) => formatNumber(record.filled_quantity, 8),
  },
  {
    title: '成交均价',
    key: 'avg_fill_price',
    width: 120,
    customRender: ({ record }) => formatNumber(record.avg_fill_price, symbolPricePrecision.value),
  },
  {
    title: '状态',
    key: 'status',
    width: 130,
    customRender: ({ record }) =>
      h(Tag, { color: record.status === 'FILLED' ? 'success' : 'default' }, () => record.status || '-'),
  },
  {
    title: '完成/最后成交时间',
    key: 'completed_time',
    width: 180,
    customRender: ({ record }) => formatDateTime(orderCompletedTime(record)),
  },
]
function orderRowExpandable(order: BacktestOrder): boolean {
  return orderFills(order).length > 0
}
const fillColumns: TableColumnsType<BacktestFill> = [
  {
    title: '时间',
    key: 'time',
    width: 180,
    customRender: ({ record }) => formatDateTime(fillTime(record)),
  },
  {
    title: '价格',
    key: 'price',
    width: 120,
    customRender: ({ record }) => formatNumber(record.price, symbolPricePrecision.value),
  },
  {
    title: '数量',
    key: 'quantity',
    width: 120,
    customRender: ({ record }) => formatNumber(record.quantity, 8),
  },
  {
    title: '手续费',
    key: 'commission',
    width: 140,
    customRender: ({ record }) =>
      record.commission_asset
        ? `${formatNumber(record.commission, 8)} ${record.commission_asset}`
        : formatNumber(record.commission, 8),
  },
  {
    title: 'Maker/Taker',
    key: 'maker_taker',
    width: 130,
    customRender: ({ record }) => fillMakerLabel(record),
  },
  {
    title: 'Fill ID',
    key: 'id',
    width: 220,
    customRender: ({ record }) => record.fill_id || record.id,
  },
]
const rootTo = computed(() => ({ path: '/backtests', query: route.query }))
const symbolsTo = computed(() => ({
  path: `/backtests/${encodeURIComponent(researchId.value)}/symbols`,
  query: route.query,
}))
const equityTo = computed(() => ({
  path: `/backtests/${encodeURIComponent(researchId.value)}/equity`,
}))
const displayTradeId = computed(() => tradeQuery.data.value?.trade_id || tradeId.value)
const signalId = computed(() => tradeQuery.data.value?.campaign_id || null)
const openedFromEquity = computed(() => route.query.from === 'equity')
const backTo = computed(() => {
  if (openedFromEquity.value) return equityTo.value
  return tradeQuery.data.value
    ? {
        path: `/backtests/${encodeURIComponent(researchId.value)}/symbols/${encodeURIComponent(tradeQuery.data.value.symbol)}/trades`,
        query: route.query,
      }
    : symbolsTo.value
})
const crumbs = computed(() =>
  openedFromEquity.value
    ? [{ label: '回测复盘', to: rootTo.value }, { label: '收益曲线', to: equityTo.value }, { label: '单笔复盘' }]
    : [{ label: '回测复盘', to: rootTo.value }, { label: '交易对数据', to: symbolsTo.value }, { label: '单笔复盘' }],
)
</script>

<template>
  <BacktestPage
    :title="tradeQuery.data.value ? `${tradeQuery.data.value.symbol} 单笔复盘` : '单笔复盘'"
    :eyebrow="displayTradeId"
    :back-to="backTo"
    :crumbs="crumbs"
  >
    <QueryPanel :pending="tradeQuery.isPending.value" :error="tradeQuery.error.value" @retry="tradeQuery.refetch()">
      <template v-if="tradeQuery.data.value">
        <div class="trade-summary-strip">
          <div>
            <span>首笔成交确认</span>
            <strong>{{ formatDateTime(tradeQuery.data.value.entry_time) }}</strong>
          </div>
          <div>
            <span>开仓均价</span>
            <strong>
              {{
                formatNumber(
                  tradeQuery.data.value.average_entry_price ?? tradeQuery.data.value.entry_price,
                  symbolPricePrecision,
                )
              }}
            </strong>
          </div>
          <div>
            <span>退出时间</span>
            <strong>{{ formatDateTime(tradeQuery.data.value.exit_time) }}</strong>
          </div>
          <div>
            <span>净盈亏</span>
            <strong :class="pnlClass(tradeQuery.data.value.net_pnl)">
              {{ formatNumber(tradeQuery.data.value.net_pnl, 3) }} U
            </strong>
          </div>
          <div>
            <span>收益率</span>
            <strong>{{ formatPercent(tradeQuery.data.value.net_return) }}</strong>
          </div>
          <div>
            <span>订单数</span>
            <strong>{{ orders.length }}</strong>
          </div>
          <div>
            <span>成交笔数</span>
            <strong>{{ fills.length }}</strong>
          </div>
          <div>
            <span>退出原因</span>
            <strong>{{ tradeQuery.data.value.exit_reason || '-' }}</strong>
          </div>
          <div>
            <span>交易 ID</span>
            <strong class="trade-identity">{{ displayTradeId }}</strong>
          </div>
          <div v-if="signalId">
            <span>信号 ID</span>
            <strong class="trade-identity">{{ signalId }}</strong>
          </div>
        </div>

        <TradeReplayChartPanel
          :trade="tradeQuery.data.value"
          :research-id="researchId"
          :overlays="schemaQuery.data.value?.chart_overlays"
        />

        <section class="detail-section trade-details-section">
          <h3>交易基准</h3>
          <a-descriptions :column="{ xs: 1, sm: 2, md: 2, lg: 4, xl: 4, xxl: 6 }" layout="vertical" bordered>
            <a-descriptions-item label="信号时间">
              {{ formatDateTime(tradeQuery.data.value.signal_time) }}
            </a-descriptions-item>
            <a-descriptions-item label="信号价格">
              {{ formatNumber(tradeQuery.data.value.signal_price, symbolPricePrecision) }}
            </a-descriptions-item>
            <a-descriptions-item label="失效价格">
              {{ formatNumber(tradeQuery.data.value.invalid_price, symbolPricePrecision) }}
            </a-descriptions-item>
          </a-descriptions>
        </section>
        <section class="detail-section order-details-section">
          <h3>订单明细</h3>
          <div class="table-frame order-table-frame">
            <a-table
              class="orders-table"
              :columns="orderColumns"
              :data-source="orders"
              :row-key="orderKey"
              :row-expandable="orderRowExpandable"
              :scroll="{ x: 1480 }"
              :pagination="false"
              size="middle"
            >
              <template #expandedRowRender="{ record }">
                <a-table
                  class="fills-table"
                  :columns="fillColumns"
                  :data-source="orderFills(record)"
                  row-key="id"
                  :pagination="false"
                  size="small"
                />
              </template>
            </a-table>
          </div>
        </section>
        <section class="detail-section timeline-section">
          <h3>事件时间线</h3>
          <div class="timeline-panel">
            <QueryPanel
              :pending="eventsQuery.isPending.value"
              :error="eventsQuery.error.value"
              :empty="eventsQuery.data.value?.items.length === 0"
              @retry="eventsQuery.refetch()"
            >
              <a-timeline class="timeline-events" aria-label="事件时间线">
                <a-timeline-item
                  v-for="(event, index) in eventsQuery.data.value?.items"
                  :key="event.id"
                  :data-sequence="index + 1"
                >
                  <template #dot>
                    <span class="event-sequence" :aria-label="`第 ${index + 1} 个事件`">
                      {{ index + 1 }}
                    </span>
                  </template>
                  <div class="event-heading">
                    <strong>{{ eventDisplayName(event) }}</strong>
                    <time>{{ formatDateTime(event.time) }}</time>
                  </div>
                  <BacktestEventDetails
                    :event="event"
                    :reference-data="allAttributes"
                    :price-precision="symbolPricePrecision"
                  />
                </a-timeline-item>
              </a-timeline>
            </QueryPanel>
          </div>
        </section>
        <section class="detail-section">
          <h3>策略扩展参数</h3>
          <a-tag v-if="schemaQuery.data.value === null" color="orange" class="schema-fallback">
            策略 Schema 不存在，显示原始 JSON
          </a-tag>
          <JsonDetails
            :value="allAttributes"
            :groups="schemaQuery.data.value?.detail_groups || schemaQuery.data.value?.groups"
            :fields="schemaQuery.data.value?.parameter_fields || schemaQuery.data.value?.fields"
          />
        </section>
      </template>
    </QueryPanel>
  </BacktestPage>
</template>
