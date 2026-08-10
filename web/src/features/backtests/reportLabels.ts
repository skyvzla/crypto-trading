const labels: Record<string, string> = {
  bucket: '分组', trade_count: '交易数', win_count: '盈利数', loss_count: '亏损数', win_rate: '胜率',
  net_pnl: '净盈亏', net_return: '净收益率', average_pnl: '平均盈亏', average_holding_seconds: '平均持仓时间',
  holding_bucket: '持仓时间区间', pnl_bucket: '盈亏金额区间', fill_bucket: '成交档位', tier: '档位',
  symbol: '交易对', parameters: '参数', run_id: '运行 ID', breakout_window_hours: '上涨窗口',
  rise_trade_count: '上涨后交易数', rise_win_rate: '上涨后胜率', rise_net_pnl: '上涨后净盈亏',
  collision_group_id: '竞争组 ID', collision_size: '竞争单数', independent_pnl: '独立净盈亏', conservative_pnl: '保守净盈亏'
}

export function reportLabel(key: string): string {
  return labels[key] || key.replaceAll('_', ' ')
}
