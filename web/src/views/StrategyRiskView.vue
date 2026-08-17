<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import { Ban, CheckCircle2, GitBranch, Search, ShieldCheck } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import type { ExchangeCategory, StrategyCategoryAdmission, StrategyCategoryAdmissionAudit, UniversePreview, UniversePreviewItem } from '@/api/types'
import DataState from '@/features/operations/DataState.vue'
import MetricTile from '@/features/operations/MetricTile.vue'
import PageHeader from '@/features/operations/PageHeader.vue'
import { formatDateTime } from '@/features/operations/format'
import { collectPageItems } from '@/features/operations/pagination'

const route = useRoute()
const router = useRouter()
const strategies = ref<string[]>([])
const strategyId = ref(String(route.query.strategy ?? ''))
const categories = ref<ExchangeCategory[]>([])
const admissions = ref(new Map<string, StrategyCategoryAdmission>())
const preview = ref<UniversePreview | null>(null)
const audits = ref<StrategyCategoryAdmissionAudit[]>([])
const expanded = ref(new Set<string>())
const categorySearch = ref(String(route.query.category_q ?? ''))
const universeSearch = ref(String(route.query.universe_q ?? ''))
const initialMode = String(route.query.universe_mode ?? 'all')
const universeMode = ref<'all' | 'effective' | 'excluded'>(initialMode === 'effective' || initialMode === 'excluded' ? initialMode : 'all')
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)
const refreshedAt = ref<string | null>(null)
const confirmOpen = ref(false)
const pendingCategory = ref<ExchangeCategory | null>(null)
const pendingEnabled = ref(false)
const reason = ref('')

const categoryByKey = computed(() => new Map(categories.value.map((item) => [item.category_key, item])))
const trees = computed(() => categories.value.filter((item) => item.category_type === 'CATEGORY').map((parent) => ({ ...parent, children: categories.value.filter((child) => child.parent_key === parent.category_key) })))
const visibleTrees = computed(() => {
  const query = categorySearch.value.trim().toLowerCase()
  if (!query) return trees.value
  return trees.value.filter((item) => [item.name, item.code, ...item.children.flatMap((child) => [child.name, child.code])].some((value) => value.toLowerCase().includes(query)))
})
const visibleUniverse = computed(() => {
  const query = universeSearch.value.trim().toUpperCase()
  return (preview.value?.items ?? []).filter((item) => {
    if (query && !item.symbol.includes(query)) return false
    return true
  })
})
const strategyOptions = computed(() => strategies.value.map((value) => ({ label: value, value })))

function enabled(item: ExchangeCategory): boolean { return admissions.value.get(item.category_key)?.enabled ?? true }
function policyLabel(item: ExchangeCategory): string {
  const admission = admissions.value.get(item.category_key)
  if (!admission) return '默认允许'
  return admission.enabled ? '显式允许' : '显式关闭'
}

async function bootstrap() {
  loading.value = true
  error.value = null
  try {
    const [runtimePage, rows] = await Promise.all([
      collectPageItems((params) => operationsApi.runtimeStatus(params)),
      collectPageItems((params) => operationsApi.categoriesPage(true, params)).then((page) => page.items)
    ])
    strategies.value = [...new Set(runtimePage.items.map((item) => item.strategy_id))].sort()
    categories.value = rows
    expanded.value = new Set(rows.filter((item) => item.category_type === 'CATEGORY').map((item) => item.category_key))
    if (!strategies.value.includes(strategyId.value)) strategyId.value = strategies.value[0] ?? ''
    if (strategyId.value) await loadPolicy()
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '策略风控数据加载失败'
  } finally { loading.value = false }
}

async function loadPolicy() {
  if (!strategyId.value) return
  loading.value = true
  error.value = null
  try {
    await syncUrl()
    const [rules, universe, auditPage] = await Promise.all([
      collectPageItems((params) => operationsApi.strategyAdmissionsPage(strategyId.value, params)).then((page) => page.items),
      loadCompleteUniverse(),
      operationsApi.strategyAdmissionAudits({ strategy_id: strategyId.value, limit: 100 })
    ])
    admissions.value = new Map(rules.map((item) => [item.category_key, item]))
    preview.value = universe
    audits.value = auditPage.items
    refreshedAt.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '策略准入加载失败'
  } finally { loading.value = false }
}

async function loadCompleteUniverse(): Promise<UniversePreview> {
  const items: UniversePreviewItem[] = []
  let snapshot: UniversePreview | null = null
  const effective = universeMode.value === 'all' ? undefined : universeMode.value === 'effective'

  do {
    const page = await operationsApi.universePreview(strategyId.value, {
      freeze_days: 15,
      effective,
      limit: 1000,
      offset: items.length
    })
    snapshot = page
    const expected = page.total
    if (!page.items.length && items.length < expected) {
      throw new Error(`有效交易池分页读取在 ${items.length}/${expected} 条时中断`)
    }
    items.push(...page.items)
    if (items.length >= expected) break
  } while (snapshot)

  if (!snapshot) throw new Error('有效交易池接口未返回数据')
  return { ...snapshot, items, limit: items.length || snapshot.limit, offset: 0 }
}

async function syncUrl() {
  await router.replace({ query: {
    ...(strategyId.value ? { strategy: strategyId.value } : {}),
    ...(categorySearch.value.trim() ? { category_q: categorySearch.value.trim() } : {}),
    ...(universeSearch.value.trim() ? { universe_q: universeSearch.value.trim() } : {}),
    ...(universeMode.value !== 'all' ? { universe_mode: universeMode.value } : {})
  } })
}

async function changeStrategy() {
  await syncUrl()
  await loadPolicy()
}

async function changeUniverseMode() {
  universeSearch.value = ''
  await syncUrl()
  await loadPolicy()
}

function requestChange(item: ExchangeCategory, value: boolean) {
  pendingCategory.value = item
  pendingEnabled.value = value
  reason.value = ''
  confirmOpen.value = true
}

async function saveChange() {
  if (!pendingCategory.value || !strategyId.value || !reason.value.trim()) return
  saving.value = true
  const current = admissions.value.get(pendingCategory.value.category_key)
  try {
    await operationsApi.updateStrategyAdmission(strategyId.value, pendingCategory.value.category_key, { enabled: pendingEnabled.value, expected_version: current?.version ?? 0, updated_by: 'ledger-web', reason: reason.value.trim() })
    confirmOpen.value = false
    message.success(`${pendingCategory.value.name} 已${pendingEnabled.value ? '允许' : '关闭'}`)
    await loadPolicy()
  } catch (caught) {
    message.error(caught instanceof Error ? caught.message : '分类准入更新失败')
    await loadPolicy()
  } finally { saving.value = false }
}

function toggleTree(key: string) { const next = new Set(expanded.value); next.has(key) ? next.delete(key) : next.add(key); expanded.value = next }
function reasons(item: UniversePreviewItem): string { return item.exclusion_reasons.length ? item.exclusion_reasons.join('；') : '通过交易所、全局与策略分类门禁' }

onMounted(bootstrap)
</script>

<template>
  <main class="operations-page strategy-risk-page">
    <PageHeader eyebrow="RISK / CATEGORY ADMISSION" title="策略风控" description="首期只管理选定策略的 Category/Subcategory 可选黑名单；不控制策略进程，也不配置仓位、杠杆或参数。" :loading="loading" :refreshed-at="refreshedAt" @refresh="strategyId ? loadPolicy() : bootstrap()" />
    <div class="strategy-selector"><label><span>策略 *</span><a-select v-model:value="strategyId" show-search placeholder="选择运行状态中已知策略" :options="strategyOptions" :filter-option="(input: string, option: { label?: string }) => String(option.label || '').toLowerCase().includes(input.toLowerCase())" @change="changeStrategy" /></label><div><ShieldCheck :size="15" /><span>策略来源：账本 runtime status</span></div></div>
    <a-alert v-if="!strategies.length && !loading" type="warning" show-icon message="没有可选择的策略" description="策略选择器只使用账本运行状态中的真实 strategy_id，不提供容易输错的自由文本输入。" />

    <DataState v-if="strategyId" :loading="loading" :error="error" @retry="loadPolicy">
      <section v-if="preview" class="metric-grid preview-metrics">
        <MetricTile label="候选交易对" :value="String(preview.total_symbols)" hint="交易所生命周期 + freeze_days=15" />
        <MetricTile label="最终有效交易池" :value="String(preview.effective_symbols)" tone="positive" hint="与执行 worker 同源判定" />
        <MetricTile label="已排除交易对" :value="String(preview.excluded_symbols)" :tone="preview.excluded_symbols ? 'warning' : 'neutral'" hint="含上游全局与策略门禁" />
        <MetricTile label="显式关闭分类" :value="String([...admissions.values()].filter((item) => !item.enabled).length)" hint="空配置保持默认允许" />
      </section>
      <div class="risk-rule"><GitBranch :size="15" /><span>判定顺序：交易所状态与生命周期 → 交易对全局准入 → 策略匹配分类的显式关闭 → 最终有效交易池</span></div>
      <div class="risk-layout">
        <section class="policy-panel data-card">
          <div class="data-card-heading"><div><h2>分类准入策略</h2><p>父 Category 关闭会覆盖子分类；任一匹配分类关闭即阻止新开仓</p></div></div>
          <div class="panel-search"><a-input v-model:value="categorySearch" allow-clear placeholder="搜索分类" @change="syncUrl" @press-enter="syncUrl"><template #prefix><Search :size="13" /></template></a-input></div>
          <div class="policy-tree">
            <article v-for="parent in visibleTrees" :key="parent.category_key">
              <div class="policy-row parent" :class="{ blocked: !enabled(parent) }"><button class="tree-toggle" @click="toggleTree(parent.category_key)">{{ expanded.has(parent.category_key) ? '−' : '+' }}</button><span class="policy-name"><strong>{{ parent.name }}</strong><small>{{ parent.symbol_count }} 交易对 · {{ policyLabel(parent) }}</small></span><a-tag :color="admissions.has(parent.category_key) ? (!enabled(parent) ? 'red' : 'blue') : 'default'">{{ policyLabel(parent) }}</a-tag><a-switch :checked="enabled(parent)" :checked-children="'允许'" :un-checked-children="'关闭'" @change="requestChange(parent, Boolean($event))" /></div>
              <div v-if="expanded.has(parent.category_key)">
                <div v-for="child in parent.children" :key="child.category_key" class="policy-row child" :class="{ blocked: !enabled(parent) || !enabled(child) }"><span class="tree-branch">└</span><span class="policy-name"><strong>{{ child.name }}</strong><small>{{ child.symbol_count }} 交易对 · {{ !enabled(parent) ? '父分类覆盖关闭' : policyLabel(child) }}</small></span><a-tag :color="!enabled(parent) || !enabled(child) ? 'red' : admissions.has(child.category_key) ? 'blue' : 'default'">{{ !enabled(parent) ? '父级关闭' : policyLabel(child) }}</a-tag><a-switch :checked="enabled(child)" :disabled="!enabled(parent)" @change="requestChange(child, Boolean($event))" /></div>
              </div>
            </article>
          </div>
        </section>

        <section class="universe-panel data-card">
          <div class="data-card-heading"><div><h2>生效预览与判定理由</h2><p>直接展示后端 effective universe，并按当前状态逐页完整载入</p></div><span class="heading-meta">{{ visibleUniverse.length }} / {{ preview?.items.length ?? 0 }}</span></div>
          <div class="universe-tools"><a-input v-model:value="universeSearch" allow-clear placeholder="查询已完整载入的交易对" @change="syncUrl" @press-enter="syncUrl"><template #prefix><Search :size="13" /></template></a-input><a-radio-group v-model:value="universeMode" button-style="solid" size="small" @change="changeUniverseMode"><a-radio-button value="all">全部</a-radio-button><a-radio-button value="effective">有效</a-radio-button><a-radio-button value="excluded">排除</a-radio-button></a-radio-group></div>
          <div class="universe-list">
            <div v-for="item in visibleUniverse" :key="item.symbol" class="universe-row"><span :class="item.effective ? 'effective' : 'excluded'"><CheckCircle2 v-if="item.effective" :size="15" /><Ban v-else :size="15" /><strong>{{ item.symbol }}</strong></span><div><a-tag :color="item.effective ? 'green' : 'red'">{{ item.effective ? '有效' : '排除' }}</a-tag><p>{{ reasons(item) }}</p><small v-if="item.blocked_category_keys.length">阻断分类：{{ item.blocked_category_keys.map((key) => categoryByKey.get(key)?.name || key).join('、') }}</small></div></div>
            <a-empty v-if="!visibleUniverse.length" description="没有匹配交易对" />
          </div>
        </section>
      </div>

      <section class="data-card audit-panel"><div class="data-card-heading"><div><h2>分类准入审计</h2><p>版本冲突时重新读取，不覆盖他人变更</p></div><span class="heading-meta">{{ audits.length }} RECORDS</span></div><div v-if="audits.length" class="audit-scroll"><table class="audit-table"><thead><tr><th>时间</th><th>分类</th><th>变更</th><th>版本 / 操作者</th><th>原因</th></tr></thead><tbody><tr v-for="audit in audits" :key="audit.id"><td>{{ formatDateTime(audit.changed_at) }}</td><td>{{ categoryByKey.get(audit.category_key)?.name || audit.category_key }}</td><td>{{ audit.previous_enabled == null ? '默认' : audit.previous_enabled ? '允许' : '关闭' }} → {{ audit.enabled ? '允许' : '关闭' }}</td><td>v{{ audit.version }} · {{ audit.changed_by }}</td><td>{{ audit.reason || '—' }}</td></tr></tbody></table></div><a-empty v-else description="当前策略没有显式分类准入变更" /></section>
    </DataState>

    <a-modal v-model:open="confirmOpen" :confirm-loading="saving" :title="`${pendingEnabled ? '允许' : '关闭'} ${pendingCategory?.name || ''}`" ok-text="确认变更" :ok-button-props="{ disabled: !reason.trim(), danger: !pendingEnabled }" @ok="saveChange">
      <a-alert v-if="!pendingEnabled" type="warning" show-icon message="该分类将阻止策略新开仓" description="相关未成交入场单应由执行链路按已确认规则撤销；已有仓位继续保护与退出。页面只提交准入事实，不直接操作订单。" />
      <a-alert v-else type="info" show-icon message="允许分类不会绕过交易所状态或交易对全局禁用。" />
      <label class="reason-field"><span>修改原因 *</span><a-textarea v-model:value="reason" maxlength="500" show-count :rows="3" placeholder="说明本次策略分类准入变更" /></label>
    </a-modal>
  </main>
</template>

<style scoped lang="scss">
.strategy-selector { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; margin-bottom:12px; padding:11px 12px; border:1px solid var(--line); background:var(--surface); }.strategy-selector label { display:grid; gap:5px; width:min(430px,100%); }.strategy-selector label>span { color:var(--muted); font-size:var(--font-size-xs); }.strategy-selector>div { display:flex; align-items:center; gap:6px; color:var(--color-gold); font:var(--font-size-xs) var(--font-family-mono); }.preview-metrics { margin-bottom:10px; }.risk-rule { display:flex; align-items:center; gap:8px; margin-bottom:10px; padding:9px 12px; border-left:3px solid var(--color-gold); background:color-mix(in srgb,var(--color-warning) 8%,transparent); color:var(--muted); font-size:var(--font-size-xs); }.risk-layout { display:grid; grid-template-columns:minmax(440px,1fr) minmax(390px,1fr); gap:12px; align-items:start; }.panel-search,.universe-tools { padding:9px 10px; border-bottom:1px solid var(--line); }.universe-tools { display:grid; grid-template-columns:1fr auto; gap:8px; }.policy-tree { max-height:720px; overflow:auto; }.policy-tree article { border-bottom:1px solid var(--line); }.policy-tree article:last-child { border-bottom:0; }.policy-row { display:grid; grid-template-columns:26px minmax(0,1fr) auto 54px; align-items:center; gap:7px; min-height:54px; padding:7px 10px; }.policy-row.child { min-height:47px; padding-left:34px; background:rgba(80,100,120,.025); }.policy-row.blocked { box-shadow:inset 3px 0 var(--color-danger); }.tree-toggle { display:grid; place-items:center; width:24px; height:24px; border:1px solid var(--line); border-radius:3px; background:transparent; color:var(--color-gold); cursor:pointer; }.tree-branch { color:var(--color-gold); }.policy-name { min-width:0; }.policy-name strong,.policy-name small { display:block; }.policy-name strong { font-size:var(--font-size-sm); }.policy-name small { margin-top:3px; color:var(--muted); font-size:var(--font-size-xs); }.universe-list { max-height:720px; overflow:auto; }.universe-row { display:grid; grid-template-columns:135px minmax(0,1fr); gap:9px; min-height:72px; padding:10px 12px; border-bottom:1px solid var(--line); }.universe-row:last-child { border-bottom:0; }.universe-row>span { display:flex; align-items:center; gap:6px; }.universe-row>span.effective { color:var(--color-success); }.universe-row>span.excluded { color:var(--color-danger); }.universe-row strong { color:var(--text); font:var(--font-size-sm) var(--font-family-mono); }.universe-row p,.universe-row small { display:block; margin:4px 0 0; color:var(--muted); font-size:var(--font-size-xs); line-height:1.5; }.audit-panel { margin-top:12px; }.audit-scroll { overflow:auto; }.audit-table { min-width:850px; }.reason-field { display:grid; gap:6px; margin-top:14px; }.reason-field span { color:var(--muted); font-size:var(--font-size-xs); }
@media(max-width:1020px){.risk-layout{grid-template-columns:1fr}}@media(max-width:600px){.strategy-selector{align-items:flex-start;flex-direction:column}.universe-tools{grid-template-columns:1fr}.policy-row{grid-template-columns:25px minmax(0,1fr) 54px}.policy-row .ant-tag{display:none}.universe-row{grid-template-columns:1fr}}
</style>
