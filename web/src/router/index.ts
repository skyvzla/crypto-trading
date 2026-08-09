import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// 骨架阶段所有视图都指向同一个占位组件；接口定稿后逐个替换 component。
const Placeholder = () => import('@/views/PlaceholderView.vue')
const Universe = () => import('@/views/UniverseView.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/overview' },
  {
    path: '/overview',
    name: 'overview',
    component: Placeholder,
    meta: { title: '运行总览', ready: false }
  },
  {
    path: '/calendar',
    name: 'calendar',
    component: Placeholder,
    meta: { title: '收益日历', ready: false, needsApi: 'PnL 时序聚合端点' }
  },
  {
    path: '/positions',
    name: 'positions',
    component: Placeholder,
    meta: { title: '持仓', ready: false }
  },
  {
    path: '/trades',
    name: 'trades',
    component: Placeholder,
    meta: { title: '成交与买卖点', ready: false }
  },
  {
    path: '/stats',
    name: 'stats',
    component: Placeholder,
    meta: { title: '胜率与盈亏比', ready: false }
  },
  {
    path: '/symbols',
    name: 'symbols',
    component: Placeholder,
    meta: { title: '交易对统计', ready: false, needsApi: '按交易对聚合端点' }
  },
  {
    path: '/universe',
    name: 'universe',
    component: Universe,
    meta: { title: '交易对管理', ready: true }
  },
  {
    path: '/admissions',
    name: 'admissions',
    component: Placeholder,
    meta: { title: 'Subcategory 管理', ready: false }
  },
  { path: '/:pathMatch(.*)*', redirect: '/overview' }
]

export const router = createRouter({
  history: createWebHistory('/ui/'),
  routes
})
