<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ChevronDown, ChevronRight, DatabaseBackup, FolderTree, ListFilter, Search } from 'lucide-vue-next'
import { message } from 'ant-design-vue'
import { operationsApi } from '@/api/operations'
import type { ExchangeCategory, ExchangeSymbol, ExchangeSymbolSyncStatus } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { collectPageItems } from '@/shared/pagination'
import { isQuerySynced, useLedgerLoader, useQuerySync } from '@/features/operations/useOperationsView'
import { formatDateTime } from '@/shared/format'

/** 详情区每页交易对数量。 */
const DETAIL_PAGE_SIZE = 50

const route = useRoute()
const syncQuery = useQuerySync()
const categories = ref<ExchangeCategory[]>([])
const syncStatus = ref<ExchangeSymbolSyncStatus | null>(null)
const search = ref(String(route.query.q ?? ''))
const expanded = ref(new Set<string>())
const selected = ref<ExchangeCategory | null>(null)
const viewingUnclassified = ref(false)
const selectedSymbols = ref<ExchangeSymbol[]>([])
const selectedTotal = ref(0)
const detailPage = ref(Math.max(1, Number(route.query.detail_page) || 1))
const detailLoading = ref(false)
let detailRequest = 0

const parents = computed(() => categories.value.filter((item) => item.category_type === 'CATEGORY').map((parent) => ({ ...parent, children: categories.value.filter((child) => child.parent_key === parent.category_key) })))
const orphans = computed(() => categories.value.filter((item) => item.category_type === 'SUBCATEGORY' && !categories.value.some((parent) => parent.category_key === item.parent_key)))
const visibleParents = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return parents.value
  return parents.value.filter((parent) => [parent.name, parent.code, ...parent.children.flatMap((child) => [child.name, child.code])].some((value) => value.toLowerCase().includes(query)))
})

/** 本页放进地址栏的内容。写回与「URL 是否被外部改动」共用这一处声明。 */
function routeQuery() {
  const hasDetail = Boolean(selected.value || viewingUnclassified.value)
  return {
    q: search.value.trim(),
    ...(viewingUnclassified.value ? { unclassified: 'true' } : selected.value ? { category: selected.value.category_key } : {}),
    ...(hasDetail && detailPage.value > 1 ? { detail_page: detailPage.value } : {})
  }
}

/**
 * 把地址栏状态同步回本地 ref。
 *
 * 只恢复搜索框与详情页码；选中的分类由加载函数按 URL 重新推导，
 * 因为它需要先拿到分类列表才能定位。
 */
function restoreFromRoute() {
  search.value = String(route.query.q ?? '')
  detailPage.value = Math.max(1, Number(route.query.detail_page) || 1)
}

const { loading, error, refreshedAt, reload } = useLedgerLoader(async ({ isStale }) => {
  const [categoryPage, status] = await Promise.all([
    collectPageItems((params) => operationsApi.categoriesPage(false, params)),
    operationsApi.symbolSyncStatus()
  ])
  if (isStale()) return
  const rows = categoryPage.items
  categories.value = rows
  syncStatus.value = status
  // 默认只展开前三个 Category，避免首屏铺开上百个子分类。
  expanded.value = new Set(rows.filter((item) => item.category_type === 'CATEGORY').slice(0, 3).map((item) => item.category_key))
  if (route.query.unclassified === 'true') {
    await selectUnclassified(false)
  } else if (route.query.category) {
    const initial = rows.find((item) => item.category_key === String(route.query.category))
    if (initial) await selectCategory(initial, false)
  }
}, {
  fallbackMessage: '分类目录加载失败',
  onActivate: restoreFromRoute
})

function toggle(key: string) {
  const next = new Set(expanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expanded.value = next
}

function toggleAll() {
  expanded.value = expanded.value.size ? new Set() : new Set(parents.value.map((item) => item.category_key))
}

async function selectCategory(item: ExchangeCategory, resetPage = true) {
  viewingUnclassified.value = false
  selected.value = item
  if (resetPage) {
    detailPage.value = 1
    selectedTotal.value = 0
  }
  await syncUrl()
  await loadDetailSymbols()
}

async function selectUnclassified(resetPage = true) {
  viewingUnclassified.value = true
  selected.value = null
  if (resetPage) {
    detailPage.value = 1
    selectedTotal.value = 0
  }
  await syncUrl()
  await loadDetailSymbols()
}

async function loadDetailSymbols() {
  const request = ++detailRequest
  selectedSymbols.value = []
  detailLoading.value = true
  try {
    const params = { limit: DETAIL_PAGE_SIZE, offset: (detailPage.value - 1) * DETAIL_PAGE_SIZE }
    const page = viewingUnclassified.value
      ? await operationsApi.exchangeSymbols({ ...params, unclassified: true })
      : selected.value
        ? await operationsApi.categorySymbols(selected.value.category_key, params)
        : null
    if (request === detailRequest && page) {
      const lastPage = Math.max(1, Math.ceil(page.total / DETAIL_PAGE_SIZE))
      if (detailPage.value > lastPage) {
        detailPage.value = lastPage
        await syncUrl()
        await loadDetailSymbols()
        return
      }
      selectedSymbols.value = page.items
      selectedTotal.value = page.total
    }
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : '关联交易对加载失败')
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

async function changeDetailPage(page: number) {
  detailPage.value = page
  await syncUrl()
  await loadDetailSymbols()
}


// 已经在本页时直接改地址栏——手改 URL、打开一条带不同筛选的分享链接——组件
// 既不会重新挂载也不会重新 activate，只靠 onActivated 跟不上。
//
// 自己写回的 query 与 routeQuery() 一致，所以这里不会把应用筛选变成两次请求；
// 路由名变了说明已经切走，被缓存的实例不该再管地址栏。
const ownRoute = route.name
watch(() => route.query, () => {
  if (route.name !== ownRoute || isQuerySynced(route.query, routeQuery())) return
  restoreFromRoute()
  void reload()
})

async function syncUrl() {
  await syncQuery(routeQuery())
}

</script>

<template>
  <main class="operations-page categories-page">
    <PageHeader eyebrow="REFERENCE DATA / BINANCE TAXONOMY" title="分类管理" description="只读展示程序同步的 Category → Subcategory → 交易对关系；Web 不创建、重命名或编辑同步事实。" :loading="loading" :refreshed-at="refreshedAt" @refresh="reload" />
    <div v-if="syncStatus" :class="['status-strip', { stale: syncStatus.stale, error: syncStatus.status !== 'SUCCESS' }]">
      <DatabaseBackup :size="14" /><span>来源 <strong>Binance 同步事实</strong> · {{ syncStatus.status }} · 最近成功 {{ formatDateTime(syncStatus.last_success_at) }} · 允许最大年龄 {{ syncStatus.max_age_hours }}h</span><a-tag v-if="syncStatus.stale" color="gold">STALE · 历史数据仍展示</a-tag>
    </div>
    <div class="category-tools"><a-input v-model:value="search" allow-clear placeholder="搜索 Category / Subcategory" @change="syncUrl" @press-enter="syncUrl"><template #prefix><Search :size="14" /></template></a-input><a-button @click="toggleAll">{{ expanded.size ? '全部收起' : '全部展开' }}</a-button><a-button :type="viewingUnclassified ? 'primary' : 'default'" @click="selectUnclassified()"><template #icon><ListFilter :size="14" /></template>未分类交易对</a-button><span>{{ parents.length }} Category · {{ categories.length - parents.length }} Subcategory</span></div>
    <a-alert v-if="syncStatus?.last_error" type="warning" show-icon :message="syncStatus.last_error" description="保留并展示最近一次成功同步的数据。" class="sync-error" />
    <DataState :loading="loading" :error="error" :empty="!categories.length && !viewingUnclassified" @retry="reload">
      <div class="category-layout">
        <section class="taxonomy-tree">
          <article v-for="parent in visibleParents" :key="parent.category_key" class="taxonomy-parent">
            <div class="taxonomy-node parent" :class="{ selected: selected?.category_key === parent.category_key, inactive: !parent.active }">
              <button type="button" class="expand-control" :aria-label="expanded.has(parent.category_key) ? '收起子分类' : '展开子分类'" :aria-expanded="expanded.has(parent.category_key)" @click="toggle(parent.category_key)"><ChevronDown v-if="expanded.has(parent.category_key)" :size="14" /><ChevronRight v-else :size="14" /></button>
              <button type="button" class="parent-select" @click="selectCategory(parent)"><FolderTree :size="15" /><span class="node-copy"><strong>{{ parent.name }}</strong><small>{{ parent.code }} · {{ parent.source }}</small></span><a-tag>{{ parent.symbol_count }} 交易对</a-tag><a-tag v-if="!parent.active" color="default">INACTIVE</a-tag></button>
            </div>
            <div v-if="expanded.has(parent.category_key) || search.trim()" class="taxonomy-children">
              <button v-for="child in parent.children" :key="child.category_key" type="button" class="taxonomy-node child" :class="{ selected: selected?.category_key === child.category_key, inactive: !child.active }" @click="selectCategory(child)"><span class="tree-line">└</span><span class="node-copy"><strong>{{ child.name }}</strong><small>{{ child.code }}</small></span><a-tag>{{ child.symbol_count }}</a-tag><a-tag v-if="!child.active">INACTIVE</a-tag></button>
              <div v-if="!parent.children.length" class="no-children">没有 Subcategory</div>
            </div>
          </article>
          <article v-if="orphans.length" class="orphan-block"><h3>缺少父节点的 Subcategory</h3><button v-for="item in orphans" :key="item.category_key" type="button" class="taxonomy-node child" @click="selectCategory(item)"><span class="tree-line">!</span><span class="node-copy"><strong>{{ item.name }}</strong><small>{{ item.parent_key || 'NO PARENT KEY' }}</small></span><a-tag color="gold">{{ item.symbol_count }}</a-tag></button></article>
          <a-empty v-if="!visibleParents.length" description="没有匹配的分类" />
        </section>

        <aside class="category-detail data-card">
          <template v-if="selected || viewingUnclassified">
            <div class="data-card-heading"><div><h2>{{ viewingUnclassified ? '未分类交易对' : selected?.name }}</h2><p>{{ viewingUnclassified ? 'NO ACTIVE CATEGORY ASSOCIATION' : `${selected?.category_type} · ${selected?.category_key}` }}</p></div><span class="heading-meta">{{ selectedTotal }} SYMBOLS</span></div>
            <a-spin :spinning="detailLoading">
              <div v-if="selectedSymbols.length" class="category-symbol-list"><div v-for="symbol in selectedSymbols" :key="symbol.symbol" class="category-symbol-row"><span><strong>{{ symbol.symbol }}</strong><small>{{ symbol.base_asset }} / {{ symbol.quote_asset }}</small></span><span><a-tag :color="symbol.status === 'TRADING' ? 'green' : 'gold'">{{ symbol.status }}</a-tag><a-tag :color="symbol.global_enabled ? 'blue' : 'red'">{{ symbol.global_enabled ? '全局允许' : '全局禁止' }}</a-tag></span></div></div>
              <a-empty v-else-if="!detailLoading" :description="viewingUnclassified ? '当前没有未分类交易对' : '该分类没有关联交易对'" />
              <div v-if="selectedTotal > DETAIL_PAGE_SIZE" class="detail-pagination"><a-pagination :current="detailPage" :page-size="DETAIL_PAGE_SIZE" :total="selectedTotal" size="small" show-less-items @change="changeDetailPage" /></div>
            </a-spin>
          </template>
          <div v-else class="select-hint"><FolderTree :size="28" /><p>选择左侧 Category 或 Subcategory 查看关联交易对和当前有效状态。</p></div>
        </aside>
      </div>
    </DataState>
  </main>
</template>

<style scoped lang="scss">
.status-strip { margin-bottom:10px; }.category-tools { display:grid; grid-template-columns:minmax(230px,1fr) auto auto auto; align-items:center; gap:8px; margin-bottom:12px; }.category-tools > span { color:var(--muted); font:var(--type-meta) var(--font-family-mono); }.sync-error { margin-bottom:10px; }.category-layout { display:grid; grid-template-columns:minmax(360px,.85fr) minmax(390px,1.15fr); gap:12px; align-items:start; }.taxonomy-tree { display:grid; gap:7px; }.taxonomy-parent { border:1px solid var(--line); border-radius:5px; overflow:hidden; background:var(--surface); }.taxonomy-node { display:flex; align-items:center; gap:8px; width:100%; min-height:52px; padding:8px 10px; border:0; background:transparent; color:var(--text); text-align:left; cursor:pointer; }.taxonomy-node:hover { background:var(--surface-hover); }.taxonomy-node.selected { box-shadow:inset 3px 0 var(--color-gold); background:color-mix(in srgb,var(--color-warning) 8%,transparent); }.taxonomy-node.inactive { opacity:.58; }.taxonomy-node.parent { padding:0 10px; cursor:default; }.parent-select { display:flex; align-items:center; gap:8px; flex:1; min-width:0; min-height:50px; padding:8px 0; border:0; background:transparent; color:var(--text); text-align:left; cursor:pointer; }.expand-control { display:grid; place-items:center; flex:0 0 24px; width:24px; height:24px; padding:0; border:1px solid var(--line); border-radius:3px; background:transparent; color:var(--color-gold); cursor:pointer; }.expand-control:hover,.expand-control:focus-visible { border-color:var(--color-gold); outline:none; }.node-copy { min-width:0; margin-right:auto; }.node-copy strong,.node-copy small { display:block; }.node-copy strong { font-size:var(--type-secondary); }.node-copy small { margin-top:3px; color:var(--muted); font:var(--type-meta) var(--font-family-mono); overflow-wrap:anywhere; }.taxonomy-children { border-top:1px solid var(--line); background:rgba(80,100,120,.025); }.taxonomy-node.child { min-height:43px; padding-left:42px; border-bottom:1px solid var(--line); }.taxonomy-node.child:last-child { border-bottom:0; }.tree-line { color:var(--color-gold); font-family:var(--font-family-mono); }.no-children { padding:12px 42px; color:var(--muted); font-size:var(--type-meta); }.orphan-block { border:1px solid var(--color-warning); }.orphan-block h3 { margin:0; padding:9px 12px; color:var(--color-warning); font-size:var(--type-meta); }.category-detail { position:sticky; top:0; min-height:350px; }.category-symbol-list { max-height:70vh; overflow:auto; }.category-symbol-list button { display:flex; align-items:center; justify-content:space-between; gap:12px; width:100%; min-height:54px; padding:8px 12px; border:0; border-bottom:1px solid var(--line); background:transparent; color:var(--text); text-align:left; }.category-symbol-list button:last-child { border-bottom:0; }.category-symbol-list strong,.category-symbol-list small { display:block; }.category-symbol-list strong { font:var(--type-secondary) var(--font-family-mono); }.category-symbol-list small { color:var(--muted); font-size:var(--type-meta); }.select-hint { display:grid; place-items:center; gap:8px; min-height:330px; padding:30px; color:var(--muted); text-align:center; }.select-hint p { max-width:320px; line-height:1.7; }
.category-symbol-row { display:flex; align-items:center; justify-content:space-between; gap:12px; min-height:54px; padding:8px 12px; border-bottom:1px solid var(--line); color:var(--text); }.category-symbol-row:last-child { border-bottom:0; }.detail-pagination { display:flex; justify-content:flex-end; padding:10px 12px; border-top:1px solid var(--line); }
@media(max-width:900px){.category-layout{grid-template-columns:1fr}.category-detail{position:static}.category-tools{grid-template-columns:1fr auto auto}.category-tools>span{grid-column:1/-1}}@media(max-width:560px){.category-tools{grid-template-columns:1fr}.taxonomy-node.child{padding-left:22px}}
</style>
