import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useUrlPagination } from '@/shared/pagination'

export function useBacktestPagination(defaultPageSize: number, namespace: string, maxPageSize = 500) {
  const route = useRoute()
  const pageKey = `${namespace}_page`
  const pageSizeKey = `${namespace}_page_size`
  const pagination = useUrlPagination({
    defaultSize: defaultPageSize,
    maxSize: maxPageSize,
    pageKey,
    pageSizeKey,
  })

  const preservedQuery = computed(() => ({ ...route.query }))
  return { ...pagination, preservedQuery }
}
