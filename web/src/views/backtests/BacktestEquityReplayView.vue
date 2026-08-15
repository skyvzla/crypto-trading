<script setup lang="ts">
import { computed, h, ref, watch } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { RotateCcw } from 'lucide-vue-next'
import { Tag, type TableColumnsType } from 'ant-design-vue'
import { useRoute } from 'vue-router'
import { backtestApi } from '@/api/backtests'
import BacktestPage from '@/features/backtests/BacktestPage.vue'
import EquityCurveChart from '@/features/backtests/EquityCurveChart.vue'
import QueryPanel from '@/features/backtests/QueryPanel.vue'
import { replayEquity, type EquityReplayRow } from '@/features/backtests/equityReplay'
import { formatNumber, formatPercent, formatTime, pnlClass } from '@/features/backtests/format'

const route = useRoute()
const researchId = computed(() => typeof route.params.researchId === 'string' ? route.params.researchId : '')
const initialBalance = ref(1000)
const initialPosition = ref(500)
const reinvestPercent = ref(50)
const minimumBalance = ref(100)
const feePercent = ref(0.04)
const slippagePercent = ref(0.1)
const selectedParameters = ref('')

const parameterSetsQuery = useQuery({
  queryKey: computed(() => ['backtest-replay-parameters', researchId.value]),
  queryFn: () => backtestApi.replayParameterSets(researchId.value),
  enabled: computed(() => Boolean(researchId.value))
})
const parameterOptions = computed(() => (parameterSetsQuery.data.value?.items || []).map((item, index) => ({
  value: JSON.stringify(item.parameters),
  label: `参数 ${index + 1} · ${item.trade_count} 笔 · ${formatNumber(item.net_pnl)} U`
})))
watch(parameterOptions, (options) => {
  if (!selectedParameters.value && options[0]) selectedParameters.value = options[0].value
}, { immediate: true })
const parsedParameters = computed<Record<string, unknown>>(() => {
  try { return selectedParameters.value ? JSON.parse(selectedParameters.value) : {} } catch { return {} }
})
const tradesQuery = useQuery({
  queryKey: computed(() => ['backtest-replay-trades', researchId.value, selectedParameters.value]),
  queryFn: () => backtestApi.replayTrades(researchId.value, parsedParameters.value),
  enabled: computed(() => Boolean(researchId.value && selectedParameters.value))
})
const result = computed(() => replayEquity(tradesQuery.data.value?.items || [], {
  initialBalance: initialBalance.value,
  initialPosition: initialPosition.value,
  reinvestRatio: reinvestPercent.value / 100,
  minimumBalance: minimumBalance.value,
  feeRate: feePercent.value / 100,
  slippageRate: slippagePercent.value / 100
}))

function resetSettings() {
  initialBalance.value = 1000
  initialPosition.value = 500
  reinvestPercent.value = 50
  minimumBalance.value = 100
  feePercent.value = 0.04
  slippagePercent.value = 0.1
}

const skipLabels = { overlap: '持仓中忽略', liquidated: '停止后忽略', open: '未平仓忽略' }
const columns: TableColumnsType<EquityReplayRow> = [
  { title: '#', key: 'sequence', dataIndex: 'sequence', width: 60 },
  { title: '交易对', key: 'symbol', dataIndex: 'symbol', width: 110, customRender: ({ text }) => h('strong', { class: 'symbol-name' }, String(text)) },
  { title: '入场时间', key: 'entry_time', dataIndex: 'entry_time', width: 170, customRender: ({ text }) => formatTime(text) },
  { title: '退出时间', key: 'exit_time', dataIndex: 'exit_time', width: 170, customRender: ({ text }) => formatTime(text) },
  { title: '状态', key: 'status', dataIndex: 'status', width: 110, customRender: ({ record }) => h(Tag, { color: record.status === 'executed' ? 'success' : 'default' }, () => record.status === 'executed' ? '已执行' : skipLabels[record.skipReason!] || '已忽略') },
  { title: '本笔仓位', key: 'positionAmount', dataIndex: 'positionAmount', width: 110, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '毛收益率', key: 'grossReturn', dataIndex: 'grossReturn', width: 100, customRender: ({ text }) => formatPercent(text) },
  { title: '手续费', key: 'feeAmount', dataIndex: 'feeAmount', width: 100, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '滑点影响', key: 'slippageAmount', dataIndex: 'slippageAmount', width: 100, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '回放盈亏', key: 'replayPnl', dataIndex: 'replayPnl', width: 110, customRender: ({ record }) => h('span', { class: pnlClass(record.replayPnl) }, `${formatNumber(record.replayPnl)} U`) },
  { title: '本笔复投', key: 'reinvestedProfit', dataIndex: 'reinvestedProfit', width: 110, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '交易资金池', key: 'tradingCapitalAfter', dataIndex: 'tradingCapitalAfter', width: 120, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '锁定储备', key: 'reserveCapitalAfter', dataIndex: 'reserveCapitalAfter', width: 110, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '结算后权益', key: 'balanceAfter', dataIndex: 'balanceAfter', width: 120, customRender: ({ text }) => `${formatNumber(text)} U` },
  { title: '回撤', key: 'drawdown', dataIndex: 'drawdown', width: 90, customRender: ({ text }) => formatPercent(text) }
]
</script>

<template>
  <BacktestPage title="账户收益曲线" :eyebrow="researchId" :back-to="{ path: '/backtests' }" :crumbs="[{ label: '回测复盘', to: '/backtests' }, { label: '收益曲线' }]">
    <QueryPanel :pending="parameterSetsQuery.isPending.value" :error="parameterSetsQuery.error.value" :empty="parameterSetsQuery.data.value?.items.length === 0" @retry="parameterSetsQuery.refetch()">
      <section class="equity-controls" aria-label="回放参数">
        <label class="parameter-choice"><span>策略参数</span><a-select v-model:value="selectedParameters" :options="parameterOptions" /></label>
        <label><span>初始资金</span><a-input-number v-model:value="initialBalance" :min="0.01" :precision="2" addon-after="U" /></label>
        <label><span>初始仓位</span><a-input-number v-model:value="initialPosition" :min="0" :max="initialBalance" :precision="2" addon-after="U" /></label>
        <label><span>盈利复投</span><a-input-number v-model:value="reinvestPercent" :min="0" :max="100" :precision="2" addon-after="%" /></label>
        <label><span>最低交易资金池</span><a-input-number v-model:value="minimumBalance" :min="0" :precision="2" addon-after="U" /></label>
        <label><span>单边手续费</span><a-input-number v-model:value="feePercent" :min="0" :precision="4" addon-after="%" /></label>
        <label><span>单边滑点</span><a-input-number v-model:value="slippagePercent" :min="0" :precision="4" addon-after="%" /></label>
        <a-tooltip title="恢复默认参数"><a-button type="text" shape="circle" aria-label="恢复默认回放参数" @click="resetSettings"><RotateCcw :size="16" /></a-button></a-tooltip>
      </section>

      <QueryPanel :pending="tradesQuery.isPending.value" :error="tradesQuery.error.value" :empty="tradesQuery.data.value?.items.length === 0" @retry="tradesQuery.refetch()">
        <div class="equity-summary-strip">
          <div><span>最终权益</span><strong>{{ formatNumber(result.finalBalance) }} U</strong></div>
          <div><span>净收益</span><strong :class="pnlClass(result.netProfit)">{{ formatNumber(result.netProfit) }} U</strong></div>
          <div><span>总收益率</span><strong :class="pnlClass(result.returnRate)">{{ formatPercent(result.returnRate) }}</strong></div>
          <div><span>最大回撤</span><strong class="value-negative">{{ formatPercent(result.maxDrawdown) }}</strong></div>
          <div><span>交易资金池</span><strong>{{ formatNumber(result.finalTradingCapital) }} U</strong></div>
          <div><span>锁定储备</span><strong>{{ formatNumber(result.finalReserveCapital) }} U</strong></div>
          <div><span>执行 / 忽略</span><strong>{{ result.executedCount }} / {{ result.skippedCount }}</strong></div>
          <div><span>账户状态</span><strong :class="result.liquidated ? 'value-negative' : 'value-positive'">{{ result.liquidated ? '已停止' : '运行完成' }}</strong></div>
        </div>

        <section class="equity-chart-section">
          <div class="equity-section-heading"><h3>权益走势</h3><span>平仓结算 · 成交点可悬停</span></div>
          <EquityCurveChart :points="result.points" />
        </section>

        <section class="equity-orders-section">
          <div class="equity-section-heading"><h3>订单时序</h3><span>严格按策略的单持仓准入顺序</span></div>
          <a-table :columns="columns" :data-source="result.rows" row-key="id" size="small" :scroll="{ x: 1710 }" :pagination="{ pageSize: 50, showSizeChanger: true, pageSizeOptions: ['25', '50', '100'] }" />
        </section>
      </QueryPanel>
    </QueryPanel>
  </BacktestPage>
</template>
