<script setup lang="ts">
import { computed, h, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Button, Switch, Tag, message, type TableColumnsType } from 'ant-design-vue'
import { DatabaseBackup, Search } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { ExchangeCategory, ExchangeSymbol, ExchangeSymbolSyncStatus, SymbolGlobalAdmissionAudit } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { collectPageItems } from '@/shared/pagination'
import { useLedgerLoader, useQuerySync } from '@/features/operations/useOperationsView'
import { formatDateTime } from '@/shared/format'

const route = useRoute()
const syncQuery = useQuerySync()
const symbols = ref<ExchangeSymbol[]>([])
const categories = ref<ExchangeCategory[]>([])
const categorySymbolSet = ref<Set<string> | null>(null)
const syncStatus = ref<ExchangeSymbolSyncStatus | null>(null)
const search = ref(String(route.query.q ?? ''))
const tradingStatus = ref(String(route.query.status ?? ''))
const admissionStatus = ref(String(route.query.admission ?? ''))
const categoryKey = ref(String(route.query.category ?? ''))
const updating = ref(new Set<string>())
const selected = ref<ExchangeSymbol | null>(null)
const selectedCategories = ref<ExchangeCategory[]>([])
const audits = ref<SymbolGlobalAdmissionAudit[]>([])
const detailOpen = ref(false)
const confirmOpen = ref(false)
const pendingEnabled = ref(false)
const pendingSymbol = ref<ExchangeSymbol | null>(null)
const reason = ref('')

const categoryOptions = computed(() => categories.value.map((item) => ({ label: `${item.category_type === 'SUBCATEGORY' ? '└ ' : ''}${item.name} (${item.symbol_count})`, value: item.category_key })))
const filtered = computed(() => {
  const query = search.value.trim().toUpperCase()
  return symbols.value.filter((item) => {
    if (query && ![item.symbol, item.base_asset, item.quote_asset, item.underlying_type].filter(Boolean).some((value) => String(value).toUpperCase().includes(query))) return false
    if (tradingStatus.value === 'TRADING' && item.status !== 'TRADING') return false
    if (tradingStatus.value === 'non-trading' && item.status === 'TRADING') return false
    if (admissionStatus.value === 'enabled' && !item.global_enabled) return false
    if (admissionStatus.value === 'disabled' && item.global_enabled) return false
    if (categorySymbolSet.value && !categorySymbolSet.value.has(item.symbol)) return false
    return true
  })
})

const columns: TableColumnsType<ExchangeSymbol> = [
  { title: '交易对', key: 'symbol', fixed: 'left', width: 145, customRender: ({ record }) => h(Button, { type: 'link', class: 'table-link', onClick: () => openDetail(record) }, () => record.symbol) },
  { title: '基础 / 计价', key: 'assets', width: 145, customRender: ({ record }) => `${record.base_asset || '—'} / ${record.quote_asset || '—'}` },
  { title: '合约', dataIndex: 'contract_type', key: 'contract', width: 130, customRender: ({ text }) => h(Tag, { color: 'blue' }, () => String(text)) },
  { title: '交易所状态', dataIndex: 'status', key: 'status', width: 130, customRender: ({ text }) => h(Tag, { color: text === 'TRADING' ? 'green' : 'gold' }, () => String(text)) },
  { title: 'Category', dataIndex: 'underlying_type', key: 'category', width: 135, customRender: ({ text }) => String(text || '—') },
  { title: '全局准入', dataIndex: 'global_enabled', key: 'admission', width: 115, customRender: ({ record }) => h(Switch, { checked: record.global_enabled, loading: updating.value.has(record.symbol), checkedChildren: '允许', unCheckedChildren: '禁止', onChange: (value) => requestAdmissionChange(record, Boolean(value)) }) },
  { title: '同步时间', dataIndex: 'synced_at', key: 'sync', width: 185, customRender: ({ text }) => formatDateTime(String(text)) }
]

const { loading, error, refreshedAt, reload } = useLedgerLoader(async ({ isStale }) => {
  const [symbolPage, categoryRows, status] = await Promise.all([
    collectPageItems((params) => operationsApi.exchangeSymbols(params)),
    collectPageItems((params) => operationsApi.categoriesPage(true, params)).then((page) => page.items),
    operationsApi.symbolSyncStatus()
  ])
  if (isStale()) return
  symbols.value = symbolPage.items
  categories.value = categoryRows
  syncStatus.value = status
  if (categoryKey.value) await filterCategory()
}, {
  fallbackMessage: '交易对数据加载失败',
  onActivate: () => {
    search.value = String(route.query.q ?? '')
    tradingStatus.value = String(route.query.status ?? '')
    admissionStatus.value = String(route.query.admission ?? '')
    categoryKey.value = String(route.query.category ?? '')
  }
})

async function filterCategory() {
  await syncUrl()
  if (!categoryKey.value) { categorySymbolSet.value = null; return }
  try {
    const page = await collectPageItems((params) => operationsApi.categorySymbols(categoryKey.value, params))
    categorySymbolSet.value = new Set(page.items.map((item) => item.symbol))
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : '分类交易对加载失败')
    categorySymbolSet.value = new Set()
  }
}

async function syncUrl() {
  await syncQuery({
    q: search.value.trim(),
    status: tradingStatus.value,
    admission: admissionStatus.value,
    category: categoryKey.value
  })
}

function requestAdmissionChange(item: ExchangeSymbol, enabled: boolean) {
  pendingSymbol.value = item
  pendingEnabled.value = enabled
  reason.value = ''
  confirmOpen.value = true
}

async function saveAdmission() {
  if (!pendingSymbol.value || !reason.value.trim()) return
  const item = pendingSymbol.value
  updating.value = new Set(updating.value).add(item.symbol)
  try {
    const updated = await operationsApi.updateSymbolAdmission(item.symbol, { enabled: pendingEnabled.value, expected_version: item.global_admission_version, updated_by: 'ledger-web', reason: reason.value.trim() })
    item.global_enabled = updated.enabled
    item.global_admission_version = updated.version
    confirmOpen.value = false
    message.success(`${item.symbol} 全局准入已${updated.enabled ? '允许' : '禁止'}`)
    if (selected.value?.symbol === item.symbol) await loadAudits(item.symbol)
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : '全局准入更新失败')
    await reload()
  } finally {
    const next = new Set(updating.value); next.delete(item.symbol); updating.value = next
  }
}

async function loadAudits(symbol: string) {
  const page = await operationsApi.symbolAdmissionAudits({ symbol, limit: 50 })
  audits.value = page.items
}

async function openDetail(item: ExchangeSymbol) {
  selected.value = item
  selectedCategories.value = []
  audits.value = []
  detailOpen.value = true
  const [categoryResult, auditResult] = await Promise.allSettled([operationsApi.symbolCategories(item.symbol), loadAudits(item.symbol)])
  selectedCategories.value = categoryResult.status === 'fulfilled' ? categoryResult.value : []
  if (auditResult.status === 'rejected') message.warning('准入审计记录加载失败')
}

</script>

<template>
  <main class="operations-page universe-page">
    <PageHeader eyebrow="REFERENCE DATA / GLOBAL GATE" title="交易对管理" description="只维护交易所标的事实与平台全局准入；策略分类开关已迁移到策略风控。" :loading="loading" :refreshed-at="refreshedAt" @refresh="reload" />
    <div v-if="syncStatus" :class="['status-strip', { stale: syncStatus.stale, error: syncStatus.status !== 'SUCCESS' }]">
      <DatabaseBackup :size="14" /><span>同步状态 <strong>{{ syncStatus.status }}</strong> · 最近成功 {{ formatDateTime(syncStatus.last_success_at) }} · {{ syncStatus.synced_symbols }} 个交易对 · effective universe {{ syncStatus.effective_universe_ready ? 'ready' : 'not ready' }}</span><a-tag v-if="syncStatus.stale" color="gold">STALE</a-tag><span v-if="syncStatus.last_error">{{ syncStatus.last_error }}</span>
    </div>
    <div class="universe-filters">
      <a-input v-model:value="search" allow-clear placeholder="搜索交易对或资产" @change="syncUrl" @press-enter="syncUrl"><template #prefix><Search :size="14" /></template></a-input>
      <a-select v-model:value="tradingStatus" allow-clear placeholder="交易所状态" :options="[{label:'TRADING',value:'TRADING'},{label:'非 TRADING',value:'non-trading'}]" @change="syncUrl" />
      <a-select v-model:value="admissionStatus" allow-clear placeholder="全局准入" :options="[{label:'允许',value:'enabled'},{label:'禁止',value:'disabled'}]" @change="syncUrl" />
      <a-select v-model:value="categoryKey" show-search allow-clear placeholder="Category / Subcategory" :options="categoryOptions" :filter-option="(input: string, option: { label?: string }) => String(option.label || '').toLowerCase().includes(input.toLowerCase())" @change="filterCategory" />
      <span>{{ filtered.length }} / {{ symbols.length }} · 已逐页完整载入</span>
    </div>
    <DataState :loading="loading" :error="error" :empty="!filtered.length" @retry="reload">
      <div class="table-frame"><a-table :columns="columns" :data-source="filtered" row-key="symbol" :pagination="{ pageSize: 25, showSizeChanger: true, pageSizeOptions: ['25','50','100'] }" :scroll="{ x: 1050 }" /></div>
    </DataState>

    <a-modal v-model:open="confirmOpen" :title="`${pendingEnabled ? '允许' : '禁止'} ${pendingSymbol?.symbol} 全局准入`" ok-text="确认修改" :ok-button-props="{ disabled: !reason.trim(), danger: !pendingEnabled }" @ok="saveAdmission">
      <a-alert v-if="!pendingEnabled" type="warning" show-icon message="禁止后，任何策略都不能绕过该全局门禁新开仓。" />
      <label class="reason-field"><span>修改原因 *</span><a-textarea v-model:value="reason" :rows="3" maxlength="500" show-count placeholder="记录本次全局准入变更原因" /></label>
    </a-modal>

    <a-drawer v-model:open="detailOpen" width="min(760px, 96vw)" :title="`${selected?.symbol || ''} · 交易对详情`">
      <template v-if="selected">
        <a-descriptions :column="2" bordered size="small">
          <a-descriptions-item label="基础 / 计价">{{ selected.base_asset || '—' }} / {{ selected.quote_asset || '—' }}</a-descriptions-item>
          <a-descriptions-item label="保证金资产">{{ selected.margin_asset || '—' }}</a-descriptions-item>
          <a-descriptions-item label="合约类型">{{ selected.contract_type }}</a-descriptions-item>
          <a-descriptions-item label="交易所状态">{{ selected.status }}</a-descriptions-item>
          <a-descriptions-item label="上架时间">{{ formatDateTime(selected.onboard_date) }}</a-descriptions-item>
          <a-descriptions-item label="退市时间">{{ formatDateTime(selected.delivery_date) }}</a-descriptions-item>
          <a-descriptions-item label="最后同步">{{ formatDateTime(selected.synced_at) }}</a-descriptions-item>
          <a-descriptions-item label="全局准入"><a-tag :color="selected.global_enabled ? 'green' : 'red'">{{ selected.global_enabled ? '允许' : '禁止' }}</a-tag> v{{ selected.global_admission_version }}</a-descriptions-item>
        </a-descriptions>
        <section class="drawer-section"><h3>Category / Subcategory</h3><a-space wrap><a-tag v-for="category in selectedCategories" :key="category.category_key" :color="category.category_type === 'CATEGORY' ? 'blue' : undefined">{{ category.name }} · {{ category.category_type }}</a-tag><span v-if="!selectedCategories.length" class="muted">没有同步分类关联</span></a-space></section>
        <a-alert class="metadata-note" type="info" show-icon message="当前交易对接口未返回精度、最小下单量等 metadata，因此详情不使用占位值冒充事实。" />
        <section class="drawer-section"><h3>全局准入审计</h3><div v-if="audits.length" class="audit-scroll"><table class="audit-table"><thead><tr><th>时间</th><th>变更</th><th>操作者</th><th>原因</th></tr></thead><tbody><tr v-for="audit in audits" :key="audit.id"><td>{{ formatDateTime(audit.changed_at) }}</td><td>{{ audit.previous_enabled == null ? '默认' : audit.previous_enabled ? '允许' : '禁止' }} → {{ audit.enabled ? '允许' : '禁止' }}</td><td>{{ audit.changed_by }}</td><td>{{ audit.reason || '—' }}</td></tr></tbody></table></div><div v-else class="muted">没有显式变更记录，当前为默认允许。</div></section>
      </template>
    </a-drawer>
  </main>
</template>

<style scoped lang="scss">
.status-strip { margin-bottom:10px; }.universe-filters { display:grid; grid-template-columns:minmax(180px,1.3fr) minmax(130px,.6fr) minmax(120px,.6fr) minmax(220px,1fr) auto; align-items:center; gap:8px; margin-bottom:12px; }.universe-filters .ant-select { width:100%; }.universe-filters > span { color:var(--muted); font:var(--font-size-xs) var(--font-family-mono); }.reason-field { display:grid; gap:6px; margin-top:14px; }.reason-field span,.muted { color:var(--muted); font-size:var(--font-size-xs); }.drawer-section { margin-top:18px; }.drawer-section h3 { margin:0 0 8px; font-size:var(--font-size-md); }.metadata-note { margin-top:16px; }.audit-scroll { overflow-x:auto; }.audit-table { min-width:620px; }
@media(max-width:900px){.universe-filters{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:520px){.universe-filters{grid-template-columns:1fr}}
</style>
