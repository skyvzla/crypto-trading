import { computed, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'

export function useBacktestPagination(defaultPageSize: number) {
  const route = useRoute()
  const router = useRouter()
  const page = ref(Math.max(1, Number(route.query.page) || 1))
  const pageSize = ref(Math.max(1, Number(route.query.page_size) || defaultPageSize))

  watch([page, pageSize], ([nextPage, nextSize]) => {
    const query: LocationQuery = { ...route.query, page: String(nextPage), page_size: String(nextSize) }
    void router.replace({ query })
  })
  watch(() => [route.query.page, route.query.page_size], ([nextPage, nextSize]) => {
    page.value = Math.max(1, Number(nextPage) || 1)
    pageSize.value = Math.max(1, Number(nextSize) || defaultPageSize)
  })

  const preservedQuery = computed(() => ({ ...route.query }))
  return { page, pageSize, preservedQuery }
}
