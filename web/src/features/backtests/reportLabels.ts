const labels: Record<string, string> = {
  bucket: '分组', trade_count: '交易数', win_count: '盈利数', loss_count: '亏损数', win_rate: '胜率',
  net_pnl: '净盈亏', net_return: '净收益率', average_pnl: '平均盈亏', average_holding_seconds: '平均持仓时间',
  holding_bucket: '持仓时间区间', pnl_bucket: '盈亏金额区间', fill_bucket: '成交档位', tier: '档位',
  symbol: '交易对', parameters: '参数', run_id: '运行 ID', breakout_window_hours: '上涨窗口',
  rise_trade_count: '上涨后交易数', rise_win_rate: '上涨后胜率', rise_net_pnl: '上涨后净盈亏',
  collision_group_id: '竞争组 ID', collision_size: '竞争单数', independent_pnl: '独立净盈亏', conservative_pnl: '保守净盈亏'
  ,signal_time: '信号时间', signal_time_iso: '信号时间', entry_time: '开仓时间', entry_time_iso: '开仓时间',
  exit_time: '退出时间', exit_time_iso: '退出时间', entry_price: '开仓价格', exit_price: '退出价格',
  average_entry_price: '开仓均价', entry_quantity: '开仓数量', exit_quantity: '退出数量',
  entry_fill_count: '开仓成交笔数', exit_fill_count: '退出成交笔数', exit_reason: '退出原因', status: '状态',
  winner: '是否盈利', side: '方向', campaign_id: '交易批次', trade_id: '交易 ID',
  tier1_price: '第一档价格', tier2_price: '第二档价格', tier3_price: '第三档价格',
  tier1_weight: '第一档权重', tier2_weight: '第二档权重', tier3_weight: '第三档权重',
  tier1_fill_count: '第一档成交数', tier2_fill_count: '第二档成交数', tier3_fill_count: '第三档成交数',
  tier1_avg_fill_price: '第一档成交均价', tier2_avg_fill_price: '第二档成交均价', tier3_avg_fill_price: '第三档成交均价',
  invalid_price: '失效价格', trigger_price: '触发价格', prior_high: '前期高点', prior_high_4h: '4 小时前高',
  low_4h: '4 小时低点', low_6h: '6 小时低点', low_8h: '8 小时低点', low_12h: '12 小时低点', low_24h: '24 小时低点',
  rise_from_4h_low: '相对 4 小时低点涨幅', rise_from_6h_low: '相对 6 小时低点涨幅',
  rise_from_8h_low: '相对 8 小时低点涨幅', rise_from_12h_low: '相对 12 小时低点涨幅',
  rise_from_24h_low: '相对 24 小时低点涨幅', box_3d_position: '3 天箱体位置', box_7d_position: '7 天箱体位置',
  holding_seconds: '持仓秒数', collision_status: '交易竞争状态'
}

export function reportLabel(key: string): string {
  return labels[key] || key.replaceAll('_', ' ')
}

export function hasReportLabel(key: string): boolean {
  return key in labels
}
