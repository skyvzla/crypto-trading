<script setup lang="ts">
import { computed } from 'vue'
import { useQuery } from '@tanstack/vue-query'
import { RefreshCw, Search, X } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import { collectPageItems } from '@/shared/pagination'
import type { OperationFilters } from '@/features/operations/useOperationsView'

/**
 * 账户目录变动很慢，缓存 5 分钟。
 * FilterBar 出现在 5 个页面上，以前每次挂载都会把账户列表整个重拉一遍。
 */
const ACCOUNTS_STALE_TIME_MS = 5 * 60_000

const props = withDefaults(
  defineProps<{
    modelValue: OperationFilters
    accountRequired?: boolean
    showStatus?: boolean
    status?: string
    statusOptions?: Array<{ label: string; value: string }>
  }>(),
  {
    accountRequired: false,
    showStatus: false,
    status: '',
    statusOptions: () => [],
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: OperationFilters]
  'update:status': [value: string]
  reset: []
  apply: []
}>()

const filters = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value),
})

const accountsQuery = useQuery({
  queryKey: ['ledger-accounts'],
  queryFn: async () => {
    const page = await collectPageItems((params) => operationsApi.accounts(params))
    return page.items.map((item) => item.account_id)
  },
  staleTime: ACCOUNTS_STALE_TIME_MS,
})
const accountsLoading = computed(() => accountsQuery.isFetching.value)
const accountsError = computed(() => Boolean(accountsQuery.error.value))
const accountOptions = computed(() => [
  ...(props.accountRequired ? [] : [{ label: '全部账户', value: '' }]),
  ...(accountsQuery.data.value ?? []).map((accountId) => ({ label: accountId, value: accountId })),
])

function loadAccounts() {
  void accountsQuery.refetch()
}

function update(key: keyof OperationFilters, value: string) {
  filters.value = { ...filters.value, [key]: value }
}

function reset() {
  filters.value = { account_id: '', strategy_id: '', symbol: '' }
  emit('update:status', '')
  emit('reset')
  emit('apply')
}
</script>

<template>
  <div class="filter-ledger">
    <label>
      <span>账户{{ accountRequired ? ' *' : '' }}</span>
      <a-select
        :value="filters.account_id"
        allow-clear
        show-search
        :loading="accountsLoading"
        :options="accountOptions"
        :placeholder="accountRequired ? '选择账户 ID' : '全部账户'"
        @update:value="update('account_id', String($event ?? ''))"
      />
      <span v-if="accountsError" class="filter-account-error">
        账户列表读取失败
        <a-tooltip title="重新读取账户列表">
          <a-button type="text" size="small" aria-label="重新读取账户列表" @click.stop="loadAccounts">
            <template #icon><RefreshCw :size="13" /></template>
          </a-button>
        </a-tooltip>
      </span>
    </label>
    <label>
      <span>策略</span>
      <a-input
        :value="filters.strategy_id"
        allow-clear
        placeholder="全部策略"
        @update:value="update('strategy_id', String($event ?? ''))"
        @keyup.enter="$emit('apply')"
      />
    </label>
    <label>
      <span>交易对</span>
      <a-input
        :value="filters.symbol"
        allow-clear
        placeholder="如 BTCUSDT"
        @update:value="update('symbol', String($event ?? '').toUpperCase())"
        @keyup.enter="$emit('apply')"
      />
    </label>
    <label v-if="showStatus">
      <span>状态</span>
      <a-select
        :value="status"
        allow-clear
        placeholder="全部状态"
        :options="statusOptions"
        @update:value="$emit('update:status', String($event ?? ''))"
      />
    </label>
    <slot name="extra-fields" />
    <div class="filter-actions">
      <a-button type="primary" @click="$emit('apply')">
        <template #icon><Search :size="14" /></template>
        查询
      </a-button>
      <a-button aria-label="清空筛选" @click="reset">
        <template #icon><X :size="14" /></template>
      </a-button>
    </div>
  </div>
</template>
