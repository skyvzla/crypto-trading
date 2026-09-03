import { computed, ref, watch } from 'vue'
import { useRoute, useRouter, type LocationQuery } from 'vue-router'

export function useBacktestPagination(defaultPageSize: number, namespace: string) {
  const route = useRoute()
  const router = useRouter()
  const pageKey = `${namespace}_page`
  const pageSizeKey = `${namespace}_page_size`
  const page = ref(Math.max(1, Number(route.query[pageKey]) || 1))
  const pageSize = ref(Math.max(1, Number(route.query[pageSizeKey]) || defaultPageSize))

  watch([page, pageSize], ([nextPage, nextSize]) => {
    const query: LocationQuery = { ...route.query, [pageKey]: String(nextPage), [pageSizeKey]: String(nextSize) }
    void router.replace({ query })
  })
  watch(
    () => [route.query[pageKey], route.query[pageSizeKey]],
    ([nextPage, nextSize]) => {
      page.value = Math.max(1, Number(nextPage) || 1)
      pageSize.value = Math.max(1, Number(nextSize) || defaultPageSize)
    },
  )

  const preservedQuery = computed(() => ({ ...route.query }))
  return { page, pageSize, preservedQuery }
}
