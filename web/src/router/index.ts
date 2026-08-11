import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

// 骨架阶段所有视图都指向同一个占位组件；接口定稿后逐个替换 component。
const Placeholder = () => import('@/views/PlaceholderView.vue')
const Universe = () => import('@/views/UniverseView.vue')
const BacktestResearchList = () => import('@/views/backtests/BacktestResearchListView.vue')
const BacktestReportCatalog = () => import('@/views/backtests/BacktestReportCatalogView.vue')
const BacktestReportDetail = () => import('@/views/backtests/BacktestReportDetailView.vue')
const BacktestSymbolList = () => import('@/views/backtests/BacktestSymbolListView.vue')
const BacktestTradeList = () => import('@/views/backtests/BacktestTradeListView.vue')
const BacktestTradeReplay = () => import('@/views/backtests/BacktestTradeReplayView.vue')

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
    path: '/backtests',
    name: 'backtests',
    component: BacktestResearchList,
    meta: { title: '回测复盘', ready: true }
  },
  {
    path: '/backtests/:researchId/reports',
    name: 'backtest-reports',
    component: BacktestReportCatalog,
    meta: { title: '回测分析报表', ready: true }
  },
  {
    path: '/backtests/:researchId/reports/:reportType',
    name: 'backtest-report-detail',
    component: BacktestReportDetail,
    meta: { title: '回测报表详情', ready: true }
  },
  {
    path: '/backtests/:researchId/symbols',
    name: 'backtest-symbols',
    component: BacktestSymbolList,
    meta: { title: '回测交易对', ready: true }
  },
  {
    path: '/backtests/:researchId/symbols/:symbol/trades',
    name: 'backtest-symbol-trades',
    component: BacktestTradeList,
    meta: { title: '回测交易记录', ready: true }
  },
  {
    path: '/backtests/:researchId/trades/:tradeId',
    name: 'backtest-trade-replay',
    component: BacktestTradeReplay,
    meta: { title: '单笔 K 线复盘', ready: true }
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
  history: createWebHashHistory(),
  routes
})
