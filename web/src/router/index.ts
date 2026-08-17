import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

const Overview = () => import('@/views/OverviewView.vue')
const Calendar = () => import('@/views/CalendarView.vue')
const PositionsOrders = () => import('@/views/PositionsOrdersView.vue')
const TradeReview = () => import('@/views/TradeReviewView.vue')
const CampaignTradeDetail = () => import('@/views/CampaignTradeDetailView.vue')
const Performance = () => import('@/views/PerformanceView.vue')
const Universe = () => import('@/views/UniverseView.vue')
const Categories = () => import('@/views/CategoryManagementView.vue')
const StrategyRisk = () => import('@/views/StrategyRiskView.vue')
const BacktestResearchList = () => import('@/views/backtests/BacktestResearchListView.vue')
const BacktestReportCatalog = () => import('@/views/backtests/BacktestReportCatalogView.vue')
const BacktestReportDetail = () => import('@/views/backtests/BacktestReportDetailView.vue')
const BacktestSymbolList = () => import('@/views/backtests/BacktestSymbolListView.vue')
const BacktestTradeList = () => import('@/views/backtests/BacktestTradeListView.vue')
const BacktestTradeReplay = () => import('@/views/backtests/BacktestTradeReplayView.vue')
const BacktestEquityReplay = () => import('@/views/backtests/BacktestEquityReplayView.vue')
const Notifications = () => import('@/views/NotificationsView.vue')

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/overview' },
  {
    path: '/overview',
    name: 'overview',
    component: Overview,
    meta: { title: '运行总览', ready: true }
  },
  {
    path: '/calendar',
    name: 'calendar',
    component: Calendar,
    meta: { title: '收益日历', ready: true }
  },
  {
    path: '/positions',
    name: 'positions',
    component: PositionsOrders,
    meta: { title: '持仓与订单', ready: true }
  },
  {
    path: '/trades',
    name: 'trades',
    component: TradeReview,
    meta: { title: '成交复盘', ready: true }
  },
  {
    path: '/trades/campaigns/:campaignId',
    name: 'campaign-trade-detail',
    component: CampaignTradeDetail,
    meta: { title: 'Campaign 成交', ready: true }
  },
  {
    path: '/performance',
    name: 'performance',
    component: Performance,
    meta: { title: '绩效分析', ready: true }
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
    path: '/backtests/:researchId/equity',
    name: 'backtest-equity-replay',
    component: BacktestEquityReplay,
    meta: { title: '账户收益曲线', ready: true }
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
    path: '/strategy-risk',
    name: 'strategy-risk',
    component: StrategyRisk,
    meta: { title: '策略风控', ready: true }
  },
  {
    path: '/categories',
    name: 'categories',
    component: Categories,
    meta: { title: '分类管理', ready: true }
  },
  { path: '/notifications', name: 'notifications', component: Notifications, meta: { title: '通知中心', ready: true } },
  { path: '/notifications/connectors', name: 'notifications-connectors', component: Notifications, meta: { title: '连接器与端点', ready: true } },
  { path: '/notifications/groups', name: 'notifications-groups', component: Notifications, meta: { title: '职责组', ready: true } },
  { path: '/notifications/policies', name: 'notifications-policies', component: Notifications, meta: { title: '路由策略', ready: true } },
  { path: '/notifications/activity', name: 'notifications-activity', component: Notifications, meta: { title: '事件与投递', ready: true } },
  { path: '/stats', name: 'stats', redirect: '/performance' },
  { path: '/symbols', name: 'symbols', redirect: '/performance' },
  { path: '/admissions', name: 'admissions', redirect: '/categories' },
  { path: '/:pathMatch(.*)*', redirect: '/overview' }
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes
})
