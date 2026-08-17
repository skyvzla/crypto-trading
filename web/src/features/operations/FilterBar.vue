<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Search, X } from 'lucide-vue-next'
import { operationsApi } from '@/api/operations'
import { collectPageItems } from '@/features/operations/pagination'

export interface OperationFilters {
  account_id: string
  strategy_id: string
  symbol: string
}

const props = withDefaults(defineProps<{
  modelValue: OperationFilters
  accountRequired?: boolean
  showStatus?: boolean
  status?: string
  statusOptions?: Array<{ label: string; value: string }>
}>(), {
  accountRequired: false,
  showStatus: false,
  status: '',
  statusOptions: () => []
})

const emit = defineEmits<{
  'update:modelValue': [value: OperationFilters]
  'update:status': [value: string]
  reset: []
  apply: []
}>()

const filters = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

const accountsLoading = ref(false)
const accountsError = ref(false)
const accounts = ref<string[]>([])
const accountOptions = computed(() => [
  ...(props.accountRequired ? [] : [{ label: '全部账户', value: '' }]),
  ...accounts.value.map((accountId) => ({ label: accountId, value: accountId }))
])

async function loadAccounts() {
  accountsLoading.value = true
  accountsError.value = false
  try {
    const page = await collectPageItems((params) => operationsApi.accounts(params))
    accounts.value = page.items.map((item) => item.account_id)
  } catch {
    accountsError.value = true
  } finally {
    accountsLoading.value = false
  }
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

onMounted(() => { void loadAccounts() })
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
