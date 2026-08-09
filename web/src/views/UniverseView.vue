<script setup lang="ts">
import { computed, h, onMounted, ref } from 'vue'
import {
  NButton,
  NDataTable,
  NDescriptions,
  NDescriptionsItem,
  NInput,
  NModal,
  NSpace,
  NSwitch,
  NTabPane,
  NTabs,
  NTag,
  useMessage,
  type DataTableColumns
} from 'naive-ui'
import { api } from '@/api/client'
import type {
  ExchangeCategory,
  ExchangeSymbol,
  Page,
  StrategyCategoryAdmission
} from '@/api/types'

const message = useMessage()
const symbols = ref<ExchangeSymbol[]>([])
const categories = ref<ExchangeCategory[]>([])
const categoryAdmissions = ref(new Map<string, StrategyCategoryAdmission>())
const search = ref('')
const strategyId = ref('spike-short')
const loading = ref(false)
const categoryLoading = ref(false)
const updatingSymbols = ref(new Set<string>())
const updatingCategories = ref(new Set<string>())
const selectedSymbol = ref<ExchangeSymbol | null>(null)
const selectedCategories = ref<ExchangeCategory[]>([])
const detailOpen = ref(false)

const filteredSymbols = computed(() => {
  const query = search.value.trim().toUpperCase()
  if (!query) return symbols.value
  return symbols.value.filter((item) =>
    [item.symbol, item.base_asset, item.quote_asset, item.underlying_type]
      .filter(Boolean)
      .some((value) => String(value).includes(query))
  )
})

const orderedCategories = computed(() => {
  const parents = categories.value.filter((item) => item.category_type === 'CATEGORY')
  const children = categories.value.filter((item) => item.category_type === 'SUBCATEGORY')
  return parents.flatMap((parent) => [
    parent,
    ...children.filter((child) => child.parent_key === parent.category_key)
  ])
})

async function loadSymbols() {
  loading.value = true
  try {
    const page = await api.get<Page<ExchangeSymbol>>('/exchange-symbols', {
      limit: 1000
    })
    symbols.value = page.items
  } catch (error) {
    message.error(error instanceof Error ? error.message : '交易对加载失败')
  } finally {
    loading.value = false
  }
}

async function loadCategoryPolicy() {
  const normalized = strategyId.value.trim()
  if (!normalized) return
  categoryLoading.value = true
  try {
    const [categoryRows, admissionRows] = await Promise.all([
      api.get<ExchangeCategory[]>('/exchange-categories'),
      api.get<StrategyCategoryAdmission[]>(
        `/strategy-category-admissions/${encodeURIComponent(normalized)}`
      )
    ])
    categories.value = categoryRows
    categoryAdmissions.value = new Map(
      admissionRows.map((item) => [item.category_key, item])
    )
  } catch (error) {
    message.error(error instanceof Error ? error.message : '分类策略加载失败')
  } finally {
    categoryLoading.value = false
  }
}

async function setSymbolEnabled(item: ExchangeSymbol, enabled: boolean) {
  updatingSymbols.value = new Set(updatingSymbols.value).add(item.symbol)
  try {
    const updated = await api.put<{
      enabled: boolean
      version: number
    }>(`/exchange-symbols/${encodeURIComponent(item.symbol)}/admission`, {
      enabled,
      expected_version: item.global_admission_version,
      updated_by: 'ledger-web',
      reason: enabled ? 'global symbol enabled' : 'global symbol disabled'
    })
    item.global_enabled = updated.enabled
    item.global_admission_version = updated.version
    message.success(`${item.symbol} 已${enabled ? '启用' : '停用'}`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '交易对开关更新失败')
    await loadSymbols()
  } finally {
    const next = new Set(updatingSymbols.value)
    next.delete(item.symbol)
    updatingSymbols.value = next
  }
}

function categoryEnabled(item: ExchangeCategory): boolean {
  return categoryAdmissions.value.get(item.category_key)?.enabled ?? true
}

async function setCategoryEnabled(item: ExchangeCategory, enabled: boolean) {
  const normalizedStrategy = strategyId.value.trim()
  if (!normalizedStrategy) return
  updatingCategories.value = new Set(updatingCategories.value).add(item.category_key)
  const current = categoryAdmissions.value.get(item.category_key)
  try {
    const updated = await api.put<StrategyCategoryAdmission>(
      `/strategy-category-admissions/${encodeURIComponent(normalizedStrategy)}/${encodeURIComponent(item.category_key)}`,
      {
        enabled,
        expected_version: current?.version ?? 0,
        updated_by: 'ledger-web',
        reason: enabled ? 'category enabled' : 'category disabled'
      }
    )
    const next = new Map(categoryAdmissions.value)
    next.set(item.category_key, updated)
    categoryAdmissions.value = next
  } catch (error) {
    message.error(error instanceof Error ? error.message : '分类开关更新失败')
    await loadCategoryPolicy()
  } finally {
    const next = new Set(updatingCategories.value)
    next.delete(item.category_key)
    updatingCategories.value = next
  }
}

async function openSymbol(item: ExchangeSymbol) {
  selectedSymbol.value = item
  selectedCategories.value = []
  detailOpen.value = true
  try {
    selectedCategories.value = await api.get<ExchangeCategory[]>(
      `/exchange-symbols/${encodeURIComponent(item.symbol)}/categories`
    )
  } catch (error) {
    message.error(error instanceof Error ? error.message : '分类加载失败')
  }
}

const symbolColumns: DataTableColumns<ExchangeSymbol> = [
  {
    title: '交易对',
    key: 'symbol',
    width: 150,
    render: (row) =>
      h(
        NButton,
        { text: true, type: 'primary', onClick: () => openSymbol(row) },
        { default: () => row.symbol }
      )
  },
  { title: '基础资产', key: 'base_asset', width: 110 },
  { title: '计价资产', key: 'quote_asset', width: 110 },
  {
    title: '合约',
    key: 'contract_type',
    width: 130,
    render: (row) => h(NTag, { size: 'small', bordered: false }, { default: () => row.contract_type })
  },
  {
    title: '状态',
    key: 'status',
    width: 110,
    render: (row) =>
      h(
        NTag,
        { size: 'small', bordered: false, type: row.status === 'TRADING' ? 'success' : 'warning' },
        { default: () => row.status }
      )
  },
  { title: 'Category', key: 'underlying_type', width: 130 },
  {
    title: '全局开关',
    key: 'global_enabled',
    width: 110,
    render: (row) =>
      h(NSwitch, {
        value: row.global_enabled,
        loading: updatingSymbols.value.has(row.symbol),
        onUpdateValue: (value) => setSymbolEnabled(row, value)
      })
  }
]

onMounted(() => Promise.all([loadSymbols(), loadCategoryPolicy()]))
</script>

<template>
  <div class="universe-page">
    <NTabs type="line" animated>
      <NTabPane name="symbols" tab="交易对">
        <div class="toolbar">
          <NInput v-model:value="search" clearable placeholder="搜索交易对或资产" />
          <NTag :bordered="false" type="info">{{ filteredSymbols.length }} / {{ symbols.length }}</NTag>
        </div>
        <NDataTable
          :columns="symbolColumns"
          :data="filteredSymbols"
          :loading="loading"
          :row-key="(row: ExchangeSymbol) => row.symbol"
          :pagination="{ pageSize: 25, showSizePicker: true, pageSizes: [25, 50, 100] }"
          :scroll-x="850"
          striped
        />
      </NTabPane>

      <NTabPane name="categories" tab="策略分类">
        <div class="toolbar strategy-toolbar">
          <NInput v-model:value="strategyId" placeholder="策略 ID" @keyup.enter="loadCategoryPolicy" />
          <NButton type="primary" :loading="categoryLoading" @click="loadCategoryPolicy">加载</NButton>
        </div>
        <div class="category-list">
          <div
            v-for="item in orderedCategories"
            :key="item.category_key"
            class="category-row"
            :class="{ child: item.category_type === 'SUBCATEGORY' }"
          >
            <div class="category-name">
              <span>{{ item.name }}</span>
              <NTag size="small" :bordered="false" type="default">
                {{ item.category_type === 'CATEGORY' ? 'Category' : 'Subcategory' }}
              </NTag>
              <NTag
                v-if="!categoryAdmissions.has(item.category_key)"
                size="small"
                :bordered="false"
                type="info"
              >默认</NTag>
            </div>
            <NSwitch
              :value="categoryEnabled(item)"
              :loading="updatingCategories.has(item.category_key)"
              @update:value="(value: boolean) => setCategoryEnabled(item, value)"
            />
          </div>
        </div>
      </NTabPane>
    </NTabs>

    <NModal v-model:show="detailOpen" preset="card" :title="selectedSymbol?.symbol" class="detail-modal">
      <NDescriptions v-if="selectedSymbol" :column="2" label-placement="top" bordered>
        <NDescriptionsItem label="基础资产">{{ selectedSymbol.base_asset || '-' }}</NDescriptionsItem>
        <NDescriptionsItem label="计价资产">{{ selectedSymbol.quote_asset || '-' }}</NDescriptionsItem>
        <NDescriptionsItem label="合约类型">{{ selectedSymbol.contract_type }}</NDescriptionsItem>
        <NDescriptionsItem label="交易状态">{{ selectedSymbol.status }}</NDescriptionsItem>
        <NDescriptionsItem label="上架时间">{{ selectedSymbol.onboard_date || '-' }}</NDescriptionsItem>
        <NDescriptionsItem label="下架时间">{{ selectedSymbol.delivery_date || '-' }}</NDescriptionsItem>
      </NDescriptions>
      <NSpace class="detail-categories" :size="8" wrap>
        <NTag v-for="item in selectedCategories" :key="item.category_key" :type="item.category_type === 'CATEGORY' ? 'info' : 'default'">
          {{ item.name }}
        </NTag>
      </NSpace>
    </NModal>
  </div>
</template>

<style scoped>
.universe-page {
  max-width: 1400px;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 52px;
  max-width: 520px;
}
.strategy-toolbar {
  max-width: 420px;
}
.category-list {
  border: 1px solid var(--line);
  border-radius: 6px;
  overflow: hidden;
}
.category-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 56px;
  align-items: center;
  min-height: 52px;
  padding: 0 16px;
  border-bottom: 1px solid var(--line);
}
.category-row:last-child {
  border-bottom: 0;
}
.category-row.child {
  padding-left: 42px;
  background: rgba(255, 255, 255, 0.018);
}
.category-name {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.detail-modal {
  width: min(720px, calc(100vw - 32px));
}
.detail-categories {
  margin-top: 18px;
}
@media (max-width: 640px) {
  .toolbar {
    align-items: stretch;
    flex-direction: column;
    padding: 8px 0 12px;
  }
  .strategy-toolbar {
    flex-direction: row;
  }
  .category-row {
    padding: 0 12px;
  }
  .category-row.child {
    padding-left: 24px;
  }
}
</style>
