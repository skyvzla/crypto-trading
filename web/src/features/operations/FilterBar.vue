<script setup lang="ts">
import { computed } from 'vue'
import { Search, X } from 'lucide-vue-next'

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
  apply: []
}>()

const filters = computed({
  get: () => props.modelValue,
  set: (value) => emit('update:modelValue', value)
})

function update(key: keyof OperationFilters, value: string) {
  filters.value = { ...filters.value, [key]: value }
}

function reset() {
  filters.value = { account_id: '', strategy_id: '', symbol: '' }
  emit('update:status', '')
  emit('apply')
}
</script>

<template>
  <div class="filter-ledger">
    <label>
      <span>账户{{ accountRequired ? ' *' : '' }}</span>
      <a-input
        :value="filters.account_id"
        allow-clear
        placeholder="account_id"
        @update:value="update('account_id', String($event ?? ''))"
        @keyup.enter="$emit('apply')"
      />
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
