import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import type { LedgerFilters } from '@/api/types'

/** 账户/策略/交易对筛选条件，跨视图共享。 */
export const useFiltersStore = defineStore('filters', () => {
  const accountId = ref('')
  const strategyId = ref('')
  const symbol = ref('')

  const query = computed<LedgerFilters>(() => {
    const next: LedgerFilters = {}
    if (accountId.value.trim()) next.account_id = accountId.value.trim()
    if (strategyId.value.trim()) next.strategy_id = strategyId.value.trim()
    if (symbol.value.trim()) next.symbol = symbol.value.trim()
    return next
  })

  function reset() {
    accountId.value = ''
    strategyId.value = ''
    symbol.value = ''
  }

  return { accountId, strategyId, symbol, query, reset }
})
