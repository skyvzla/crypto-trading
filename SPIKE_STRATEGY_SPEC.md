# 逼空插针策略技术规格

> 文档版本：v0.9（待定稿）
> 创建日期：2026-08-06
> 基于：已归档的历史策略架构初稿 + 平台架构讨论
> 状态：核心逻辑明确，部分参数待回测标定

---

## 一、策略定位

本策略专为 **1s 级别逼空加速、极端拉升和插针回落场景**设计，核心交易逻辑：

1. 持续监听 1s Bar 和短周期市场结构
2. 识别加速前起涨点、短时动能放大和成交量异常
3. 在满足起涨点约束后，预测极端价格区间
4. 通过多档限价单参与冲高回落做空
5. 管理部分成交、撤单、重挂、止损、止盈和超时退出

**策略分类**：1s 事件驱动型策略，运行在 `trading_platform/strategies/tick/` 进程内，使用账户 B。

---

## 二、数据输入

### 实时数据流

| 数据类型 | 来源 | 更新频率 | 用途 |
|---|---|---|---|
| 1s Bar | Redis Pub/Sub `bar1s:{symbol}` | 每秒 | 计算涨速、动能、成交量倍数 |
| 短周期 K 线（5m/15m） | Redis Hash | 每周期一次 | 计算 ATR、识别近期极值 |
| 标记价格（可选） | Redis Pub/Sub `mark:{symbol}` | 每秒 | 模拟交易所托管止损触发（V2） |

### 历史数据（启动预加载）

策略启动时需预加载：
- 最近 1 小时的 1s Bar（用于识别起涨点和基准波动率）
- 最近 24 小时的 15m K 线（用于计算 ATR 和近期极值）

---

## 三、核心状态机

```
Idle（空闲，等待信号）
  ↓ 检测到符合条件的起涨点
Monitoring（监控，持续计算特征）
  ↓ 触发条件全部满足
Triggered（触发，生成挂单计划）
  ↓ 风控通过
OrdersPlaced（订单已挂，等待成交）
  ↓ 部分或全部成交
PositionOpen（持仓中）
  ↓ 触发退出条件（止损/止盈/超时）
ExitPending（退出中，等待平仓完成）
  ↓ 仓位归零
Closed（已结束）
  ↓ 冷却期结束
Idle
```

**关键约束**：
- 同一币种同时只能有一个活跃事件（从 Triggered 到 Closed）
- 挂单未成交时触发失效条件 → 撤销所有未成交订单，状态跳转到 Closed
- 已成交仓位必须走完整退出流程，不能因新信号强制平仓

---

## 四、触发逻辑

### 4.1 起涨点识别

**定义**：本次加速开始前的价格低点，用作后续涨幅计算的基准。

**识别算法（待定，三选一）**：

| 方案 | 定义 | 优点 | 缺点 |
|---|---|---|---|
| A. 价格结构低点 | 滑动窗口内的局部最低点，要求之后持续上涨 | 直观，容易可视化验证 | 震荡行情误判 |
| B. 成交量突破点 | 成交量首次大幅放量的那一秒 | 捕捉资金流入起点 | 可能晚于实际起涨 |
| C. A + B 结合 | 价格低点和成交量放量在同一窗口内确认 | 准确性更高 | 计算复杂，回测慢 |

**回溯窗口**：30 秒 / 60 秒 / 120 秒（待回测标定）

**失效条件**：价格回落到起涨点 × 1.02 以内，当前起涨点作废，下次重新识别。

---

### 4.2 触发条件（全部满足才触发）

| 序号 | 条件 | 阈值（V1 待定） | 说明 |
|---|---|---|---|
| ① | 相对起涨点涨幅 | ≥ 10% | **硬性条件**，价格必须显著脱离起涨点 |
| ② | 1s 涨速 | ≥ 0.5% | 当前秒的加速度，必须有明显冲高 |
| ③ | 成交量倍数 | ≥ 3x | 最近 Xs 成交量 / 过去 Y 分钟均量 |
| ④ | ATR 范围 | 在合理区间 | 过低 = 假冲高，过高 = 已经崩盘 |
| ⑤ | 点差 | ≤ 0.05% | 流动性检查，点差过大不参与 |
| ⑥ | 卖盘深度 | ≥ 最小金额 | 挂单价位附近必须有足够挂单深度 |

**V1 参数冻结**：上述阈值在第一次回测后冻结为配置版本 `config/spike_v1.yaml`，运行中不可改动。

---

## 五、价格预测

**职责**：根据触发时的市场状态，估算本次插针的极端价格上沿，作为多档挂单的参考锚点。

### 预测来源优先级

| 优先级 | 来源 | 适用条件 | 数据要求 |
|---|---|---|---|
| 1 | 清算地图密集区 | 清算地图数据可用且延迟 ≤ 阈值 | V2 实现，V1 不依赖 |
| 2 | 动能外推 + ATR 加权 | 默认，始终可用 | 当前价、起涨点、ATR、近期涨速 |
| 3 | 近期极值 + 倍数外推 | 动能数据异常时备选 | 最近 N 小时最高价 |

每次预测输出必须带 `source` 标注（`liquidation_map` / `momentum` / `recent_extreme`），供日志和事后分析。

### 动能外推公式（V1 默认，待标定）

```python
# 候选公式
predicted_extreme = current_price * (1 + k * price_return_Ns)

# 其中：
# - current_price：触发时价格
# - price_return_Ns：最近 N 秒累计涨幅（如 30s 涨了 15%）
# - k：加速倍数系数，待回测标定（候选值 0.3 ~ 0.8）
# - ATR 作为上下限约束，防止极端输入导致预测值失控
```

**约束条件**：

```python
atr_15m = calculate_atr(recent_15m_bars, period=14)
predicted_extreme = clamp(
    predicted_extreme,
    min=current_price * 1.05,        # 最低也要高于当前价 5%
    max=current_price * (1 + 3 * atr_15m / current_price)  # 最高不超过 3 倍 ATR
)
```

---

## 六、多档挂单

### 档位配置（V1）

默认三档，按固定名义金额分配：

| 档位 | 占总名义金额比例 | 挂单价格 | TTL（秒） |
|---|---|---|---|
| 第一档 | 30% | predicted_extreme * 0.95 | 60 |
| 第二档 | 40% | predicted_extreme * 1.00 | 60 |
| 第三档 | 30% | predicted_extreme * 1.05 | 60 |

**总名义金额**：配置项，如 1000 USDT，杠杆只影响保证金占用，不能扩大名义额。

### 挂单规则

- 每档订单独立状态：未挂、已挂、部分成交、完全成交、撤销、失效
- 每档订单明确 `clientOrderId` = `{strategy}_{symbol}_{event_id}_{tier}`
- 超过 TTL 未成交 → 自动撤销
- 价格跌破失效价（如 `predicted_extreme * 0.90`）→ 撤销所有未成交订单

### 重挂逻辑（V1 不实现，V2 可选）

V1 挂单失败或撤销后不重挂，避免追高。V2 可考虑在动能继续扩张时重算价格并重挂，但必须限制重挂次数（最多 2 次）。

---

## 七、退出规则

### 核心设计理念

逼空插针的核心假设是**价格冲到极值后快速回落**。但清算狂热阶段，价格可能继续脉冲1-2次，固定价格止损容易被秒扫。因此采用**时间衰减止损 + 形态动态判断**：

- **短周期内不看固定价格，看形态**：动能是否衰减、是否高位横盘
- **止损距离随持仓时间缩紧**：初期极宽松（允许脉冲），后期收严（兜底保护）
- **形态判断阈值随时间收严**：持仓越久，对"不是插针"的判断越敏感

---

### 7.1 时间衰减止损（核心机制）

**止损距离计算公式**：

```python
def get_stop_loss_distance(hold_seconds):
    """
    返回止损距离（相对入场价的涨幅百分比）
    持仓时间越长，止损距离越小
    """
    if hold_seconds < 90:
        return None  # 禁止止损窗口
    elif hold_seconds < 300:
        # 90-300秒：从 +15% 线性衰减到 +5%
        return 0.15 - 0.10 / 210 * (hold_seconds - 90)
    elif hold_seconds < 900:
        # 300-900秒：从 +5% 线性衰减到 +3%
        return 0.05 - 0.02 / 600 * (hold_seconds - 300)
    else:
        # 900秒以上：固定 +3%
        return 0.03
```

**止损价随时间变化表**（入场价 50000 示例）：

| 持仓时间 | 止损距离 | 止损价 | 说明 |
|---|---|---|---|
| 0-90秒 | 无固定止损 | — | 禁止窗口，允许清算狂热期脉冲 |
| 90秒 | +15% | 57500 | 观察期起点，极宽松 |
| 120秒 | +13.6% | 56800 | |
| 180秒 | +10.0% | 55000 | |
| 240秒 | +7.1% | 53550 | |
| 300秒 | +5.0% | 52500 | 确认期起点 |
| 600秒 | +4.0% | 52000 | |
| 900秒 | +3.0% | 51500 | 兜底期，固定 |
| 900秒+ | +3.0% | 51500 | |

**触发条件**：
```python
if hold_seconds >= 90:
    stop_distance = get_stop_loss_distance(hold_seconds)
    stop_price = entry_price * (1 + stop_distance)
    if current_price >= stop_price:
        # 立即市价平仓
        return True, f"timed_stop_loss_{hold_seconds}s"
```

---

### 7.2 形态动态判断（全时段）

除固定价格止损外，还通过**动能和价格形态**提前识别"不是插针"的情况，立即止损。

#### 判断1：动能不衰减（提前止损信号）

**逻辑**：如果价格持续加速上涨，说明可能是真突破而不是插针，应提前止损。

**判断条件**：最近30秒的1s涨速均值超过阈值。

**阈值随时间收严**：

| 持仓时间 | 动能阈值 | 说明 |
|---|---|---|
| 0-90秒 | > 0.5% | 只要不是疯狂加速，不触发 |
| 90-300秒 | > 0.3% | 中等加速视为动能未衰减 |
| 300-900秒 | > 0.15% | 轻微持续上涨也触发 |
| 900秒+ | > 0.10% | 极轻微上涨都不允许 |

```python
recent_30s_bars = get_recent_bars(30)
avg_accel = mean([b.return_1s for b in recent_30s_bars])

if hold_seconds < 90:
    threshold = 0.005
elif hold_seconds < 300:
    threshold = 0.003
elif hold_seconds < 900:
    threshold = 0.0015
else:
    threshold = 0.001

if avg_accel > threshold:
    return True, "momentum_not_decay"
```

---

#### 判断2：高位横盘（提前止损信号）

**逻辑**：如果价格在高位窄幅震荡（横盘整理），说明可能蓄势二次拉升，应提前止损。

**判断条件**：最近60秒价格震荡幅度 < 阈值。

**阈值随时间收严**：

| 持仓时间 | 横盘判断 | 说明 |
|---|---|---|
| 0-90秒 | 不判断 | 初期波动正常 |
| 90-300秒 | 震荡幅度 < 0.5% | 宽松，只抓明显横盘 |
| 300-900秒 | 震荡幅度 < 0.3% | 收紧 |
| 900秒+ | 震荡幅度 < 0.2% | 严格 |

```python
recent_60s_bars = get_recent_bars(60)
highest = max([b.high for b in recent_60s_bars])
lowest = min([b.low for b in recent_60s_bars])
range_pct = (highest - lowest) / entry_price

if hold_seconds < 90:
    return False  # 不判断
elif hold_seconds < 300:
    threshold = 0.005
elif hold_seconds < 900:
    threshold = 0.003
else:
    threshold = 0.002

if range_pct < threshold:
    return True, "sideways_consolidation"
```

---

### 7.3 极端异常（全时段，立即止损）

无论持仓多久，以下情况**立即市价止损**：

| 异常类型 | 触发条件 | 说明 |
|---|---|---|
| 价格暴涨 | 当前价 > 预测极值 × 1.3 | 预测严重失败，判断错误 |
| 数据断线 | 超过10秒无新1s Bar | 进入保护模式，停止决策 |

---

### 7.4 止盈

**目标价**：入场均价 × (1 - take_profit_pct)，默认 -1.5%。

**触发条件**：当前价格 ≤ 入场均价 × (1 - 0.015)。

**执行方式**：
- V1：全部市价平仓
- V2可选：分批平仓（如先平50%，剩余继续持有观察动能）

---

### 7.5 超时退出

**最大持仓时间**：从第一笔成交开始计时，默认 900 秒（15 分钟）。

**超时后处理**：
- 如果浮盈 > 0：不强制平仓，继续持有等待止盈或动能衰减
- 如果浮盈 ≤ 0：立即市价平仓，避免长时间占用资金

---

### 7.6 完整决策流程

```python
def should_exit(position, current_bar, recent_bars):
    hold_seconds = current_bar.timestamp - position.entry_time
    current_price = current_bar.close
    
    # 1. 极端异常（全时段）
    if current_price > position.predicted_extreme * 1.3:
        return "stop_loss", "extreme_breakout"
    
    # 2. 止盈
    if current_price <= position.entry_price * (1 - 0.015):
        return "take_profit", "target_reached"
    
    # 3. 形态判断（全时段，阈值动态）
    if check_momentum_not_decay(recent_bars, hold_seconds):
        return "stop_loss", "momentum_not_decay"
    
    if check_sideways(recent_bars, position.entry_price, hold_seconds):
        return "stop_loss", "sideways_consolidation"
    
    # 4. 时间衰减止损（90秒后生效）
    if hold_seconds >= 90:
        stop_distance = get_stop_loss_distance(hold_seconds)
        stop_price = position.entry_price * (1 + stop_distance)
        if current_price >= stop_price:
            return "stop_loss", f"timed_decay_{hold_seconds}s"
    
    # 5. 超时退出（900秒后）
    if hold_seconds >= 900 and position.unrealized_pnl <= 0:
        return "stop_loss", "timeout_exit"
    
    return None, None
```

---

### 7.7 参数标定说明（重要）

**当前所有阈值均为候选值，需通过回测标定后冻结**。

标定流程：
1. 从历史回测中抽取触发事件样本（如100个插针事件）
2. 分析每个样本的价格走势：
   - 开仓后90秒内最高价相对入场价的涨幅分布 → 确定90秒止损距离
   - 开仓后300秒内的动能衰减速度 → 确定动能阈值
   - 开仓后是否出现高位横盘 → 确定横盘判断阈值
3. 调整参数后重新回测，计算胜率、Profit Factor、被扫止损率
4. 冻结为V1配置，后续不改动

**关键指标**：
- **被扫止损率**：应 < 30%（说明止损距离合理）
- **形态误判率**：动能/横盘判断触发的止损中，后续确实不回落的比例应 > 70%
- **止盈覆盖率**：触发事件中最终达到止盈的比例应 > 50%

如果某个阈值导致被扫止损率过高，说明太严格，需要放宽；如果形态误判率高，说明太宽松，需要收紧。

---

## 八、风控约束

策略执行前必须通过进程内风控层（`trading_platform/strategies/tick/risk.py`）校验：

| 风控项 | 限制 | 说明 |
|---|---|---|
| 单币最大名义仓位 | 如 1000 USDT | 单个币种同时只能有一个活跃事件 |
| 账户总仓位 | 如 5000 USDT | 1s 策略群所有币种仓位之和 |
| 单日最大亏损 | 如 -500 USDT | 触发后当日禁止新开仓 |
| 连续止损冷却 | 连续 3 次止损 → 冷却 1 小时 | 避免连续亏损 |
| 数据延迟检查 | 1s Bar 延迟 > 5 秒 → 禁止开仓 | 保护模式 |
| 盘口深度不足 | 挂单价位缺少足够挂单 → 拒绝 | 流动性保护 |

---

## 九、数据质量与异常处理

### 行情异常

| 异常类型 | 处理方式 |
|---|---|
| Redis 超过 5 秒无新 1s Bar | 进入保护模式，禁止新开仓，已有仓位继续退出 |
| 1s Bar 时间戳乱序 | 记录告警，排序后继续处理 |
| 1s Bar 数据异常（价格为 0、成交量负数） | 丢弃该条，记录告警 |

### 订单异常

| 异常类型 | 处理方式 |
|---|---|
| 下单成功但本地未收到确认 | 30 秒后主动查单，对账状态 |
| 撤单成功但成交回报延迟到达 | 以交易所 User Data Stream 回报为准 |
| 部分成交后网络断线 | 重连后从交易所查询当前仓位，恢复状态机 |

### 进程重启恢复

策略进程重启后：
1. 从 PostgreSQL 读取所有 `status != closed` 的事件
2. 从交易所查询当前订单和仓位
3. 对账差异（有仓位但本地无记录）→ 记录告警，按交易所状态恢复
4. 进入数据预热期（等待 1s Bar 窗口填满）后恢复正常

---

## 十、配置版本管理

**配置文件**：`config/spike_v1.yaml`

```yaml
version: "v1.0"
frozen_at: "2026-08-15"  # 参数冻结日期（回测标定后填写）

trigger:
  start_point_lookback_seconds: 60     # 起涨点回溯窗口（待标定）
  min_rise_from_start_pct: 0.10        # 10%
  min_accel_1s_pct: 0.005              # 0.5%
  min_volume_ratio: 3.0
  max_spread_pct: 0.0005               # 0.05%

price_prediction:
  method: "momentum"
  accel_multiplier: 0.5                # k 系数（待标定）
  atr_period: 14
  atr_max_multiplier: 3.0

orders:
  tiers: 3
  tier_ratios: [0.3, 0.4, 0.3]
  tier_price_multipliers: [0.95, 1.00, 1.05]  # 相对 predicted_extreme 的倍数
  ttl_seconds: 60
  max_notional_per_symbol: 1000        # USDT

exit:
  # 时间衰减止损
  time_decay_stop_loss:
    freeze_seconds: 90                 # 禁止止损窗口（待标定）
    phase1:                            # 观察期
      start_seconds: 90
      end_seconds: 300
      start_distance_pct: 0.15         # +15%（待标定）
      end_distance_pct: 0.05           # +5%（待标定）
    phase2:                            # 确认期
      start_seconds: 300
      end_seconds: 900
      start_distance_pct: 0.05
      end_distance_pct: 0.03
    final_distance_pct: 0.03           # 900秒后固定
  
  # 形态动态判断
  pattern:
    momentum_not_decay:                # 动能不衰减判断（待标定）
      phase1_threshold: 0.005          # 0-90秒：0.5%
      phase2_threshold: 0.003          # 90-300秒：0.3%
      phase3_threshold: 0.0015         # 300-900秒：0.15%
      phase4_threshold: 0.001          # 900秒+：0.1%
    
    sideways:                          # 高位横盘判断（待标定）
      phase1_enabled: false            # 0-90秒：不判断
      phase2_threshold: 0.005          # 90-300秒：0.5%
      phase3_threshold: 0.003          # 300-900秒：0.3%
      phase4_threshold: 0.002          # 900秒+：0.2%
  
  # 极端异常
  extreme_breakout_multiplier: 1.3     # 预测极值 × 1.3
  
  # 止盈与超时
  take_profit_pct: 0.015               # 入场价 -1.5%
  max_hold_seconds: 900

risk:
  max_account_notional: 5000           # USDT
  max_daily_loss: -500
  consecutive_stop_loss_cooldown: 3
  cooldown_seconds: 3600
```

**版本锁定**：每次回测和实盘运行记录配置文件的 SHA256 哈希，结果文件中必须包含该哈希，确保可追溯。

**参数标定流程**（V1 冻结前必须完成）：

1. **采样历史插针事件**：从历史数据中提取至少 100 个符合触发条件的样本
2. **分析价格走势特征**：
   - 开仓后 90/180/300/900 秒的最高价涨幅分布 → 确定止损距离
   - 开仓后动能衰减速度和横盘出现频率 → 确定形态判断阈值
3. **参数扫描回测**：多组参数组合回测，计算关键指标
4. **选择最优参数组**：平衡胜率、Profit Factor、被扫止损率
5. **冻结配置并计算哈希**：写入 `frozen_at` 日期，后续不改动

**关键验证指标**：

| 指标 | 目标值 | 说明 |
|---|---|---|
| 被扫止损率 | < 30% | 止损距离合理性验证 |
| 形态误判率（止损后继续上涨） | < 30% | 形态判断准确性验证 |
| 止盈覆盖率 | > 50% | 策略有效性验证 |
| Profit Factor | > 1.5 | 整体盈利能力验证 |

---

## 十一、回测要求

### 数据要求

- 使用原始 aggTrade 事件，在回测中实时聚合为 1s Bar（与实盘路径一致）
- 不能使用触发事件之后才完成的 K 线高点、成交量或其他未来数据
- 严格按事件时间顺序执行，禁止批量处理

### 成交模型

**保守限价成交规则**：

```python
# 做空限价单成交判断
if bar_high >= order_price:
    # 该秒最高价触及挂单价，视为成交
    fill_price = order_price
    fill_qty = order_qty
else:
    # 未触及，不成交
    pass
```

**不采用乐观假设**：即使盘口有足够深度，也不假设一定全部成交。V1 简化为触价即全部成交，V2 可引入部分成交模型。

### 费用模拟

- 手续费：Taker 0.05%，Maker 0.02%（逼空挂单是 Maker）
- 滑点：V1 不模拟，V2 可加入（止损市价单滑点 0.1%）

### 结果保存

每次回测生成：
- `run_meta.json`：配置哈希、策略版本（git commit）、数据范围、运行时间
- `triggers.parquet`：所有触发事件（包括未成交的）
- `orders.parquet`：所有挂单和成交记录
- `positions.parquet`：每个事件的持仓生命周期
- `summary.json`：胜率、Profit Factor、最大回撤、总触发次数、成交率

---

## 十二、监控指标

### 实时指标（Prometheus）

| 指标 | 类型 | 说明 |
|---|---|---|
| `spike_bar1s_delay_seconds` | Gauge | 1s Bar 接收延迟 |
| `spike_triggers_total` | Counter | 触发次数 |
| `spike_orders_placed_total` | Counter | 挂单次数 |
| `spike_orders_filled_total` | Counter | 成交次数 |
| `spike_positions_open` | Gauge | 当前持仓数 |
| `spike_pnl_realized` | Counter | 已实现盈亏（累计） |
| `spike_pnl_unrealized` | Gauge | 未实现盈亏（当前） |
| `spike_risk_blocked_total` | Counter | 风控拒绝次数 |

### 告警规则

| 告警 | 条件 | 级别 |
|---|---|---|
| 1s Bar 断流 | 超过 10 秒无数据 | Critical |
| 订单状态对账差异 | 本地与交易所不一致 | High |
| 连续止损 | 3 次连续止损 | Medium |
| 单日亏损接近上限 | 已亏 80% 的限额 | Medium |

---

## 十三、上线阶段

| 阶段 | 目标 | 验收标准 | 预计时长 |
|---|---|---|---|
| 1. 回测验证 | 覆盖历史插针事件，冻结参数 | 结果可确定性复现，Profit Factor > 1.5 | 1 周 |
| 2. 纸盘测试 | 实时触发与历史回放结果对比 | 触发时间差 < 2 秒，价格预测偏差 < 5% | 3 天 |
| 3. 测试网小额 | 使用测试网 API，真实挂单 | 订单全生命周期无异常 | 3 天 |
| 4. 实盘灰度 | 1-2 个币种，极低仓位（50 USDT） | 对账零差异，风控未穿透 | 1 周 |
| 5. 逐步放量 | 增加币种和仓位 | 每周评估，无严重亏损 | 持续 |

---

## 十四、待定事项（需回测后确认）

| 编号 | 问题 | 候选答案 | 决策依据 |
|---|---|---|---|
| P1 | 起涨点识别算法 | 方案 A / B / C | 历史数据回测准确率 |
| P2 | 起涨点回溯窗口 | 30s / 60s / 120s | 误判率 vs 覆盖率权衡 |
| P3 | 动能外推系数 k | 0.3 / 0.5 / 0.8 | 预测误差和成交率 |
| P4 | 触发条件各阈值 | 表 4.2 中的数值 | 触发频率和胜率平衡 |
| P5 | 挂单档位价格倍数 | 0.95/1.00/1.05 vs 0.92/1.00/1.08 | 成交率和盈亏比 |
| P6 | 禁止止损窗口 | 60s / 90s / 120s | 被扫止损率分析 |
| P7 | 止损距离衰减曲线 | 表 7.1 中的时间-距离映射 | 被扫止损率 vs 形态误判率平衡 |
| P8 | 动能不衰减阈值 | 表 7.2 中的四阶段阈值 | 形态判断准确率 |
| P9 | 高位横盘阈值 | 表 7.2 中的四阶段阈值 | 形态判断准确率 |
| P10 | 止盈目标距离 | -1.0% / -1.5% / -2.0% | 止盈触达率和整体盈利 |

**标定优先级**：

1. **P6, P7**（止损相关）：优先级最高，直接影响风险控制
2. **P1, P2**（起涨点识别）：决定触发准确性
3. **P3, P5**（价格预测和挂单）：影响成交率
4. **P8, P9**（形态判断）：优化止损效率
5. **P4, P10**（其他触发和退出）：微调

**标定方法示例（P6, P7）**：

```python
# 从历史回测中抽取100个触发事件
samples = extract_trigger_events(backtest_results, count=100)

# 分析每个事件开仓后的价格走势
for sample in samples:
    # 记录开仓后各时间点的最高价涨幅
    peak_90s = sample.get_peak_price(90) / sample.entry_price - 1
    peak_180s = sample.get_peak_price(180) / sample.entry_price - 1
    peak_300s = sample.get_peak_price(300) / sample.entry_price - 1
    
    # 记录最终是否盈利
    final_pnl = sample.final_pnl

# 统计分位数
p90_peak_90s = np.percentile([s.peak_90s for s in samples], 90)  # 如 +12%
p90_peak_180s = np.percentile([s.peak_180s for s in samples], 90)  # 如 +8%
p90_peak_300s = np.percentile([s.peak_300s for s in samples], 90)  # 如 +6%

# 根据分位数设置止损距离
# P90 表示允许 10% 的样本被止损扫掉，保护 90% 的样本
stop_distance_90s = p90_peak_90s * 1.2   # 留 20% 余量
stop_distance_180s = p90_peak_180s * 1.2
stop_distance_300s = p90_peak_300s * 1.2
```

---

## 十五、回测验证清单

标定完成后，必须验证以下场景：

### 场景 1：标准插针（应盈利）

- 价格快速冲高 10-20%，持续 30-90 秒
- 动能快速衰减，成交量萎缩
- 价格回落到入场价以下 1.5% 以上

**预期**：触发 → 挂单成交 → 持有 → 止盈退出

---

### 场景 2：脉冲后继续上涨（应止损）

- 价格冲高 10%，短暂回调 2-3%
- 再次加速上涨，突破前高
- 动能不衰减，持续放量

**预期**：触发 → 挂单成交 → 动能不衰减判断 → 提前止损（在 90-180 秒内）

---

### 场景 3：高位横盘整理（应止损）

- 价格冲高 12%，进入横盘
- 在 ±0.5% 区间震荡超过 60 秒
- 未明显回落也未继续上涨

**预期**：触发 → 挂单成交 → 横盘判断 → 提前止损（在 90-300 秒内）

---

### 场景 4：极端插针（应盈利，但可能被扫）

- 价格暴涨 25%，持续 10 秒
- 立即快速回落
- 回落速度极快，可能触及时间衰减止损

**预期**：如果 90 秒内回落到止盈价 → 盈利；如果 90 秒后才回落 → 可能被扫止损（可接受，极端场景）

---

### 场景 5：假突破（不应触发）

- 价格从起涨点上涨 8%（未达 10% 阈值）
- 成交量未明显放大
- 快速回落

**预期**：不触发（未满足触发条件 ①）

---

### 场景 6：震荡行情（不应触发）

- 价格在 5% 区间内反复震荡
- 无明确起涨点
- 成交量分散

**预期**：不触发（起涨点识别失败或触发条件不满足）

---

每个场景至少验证 10 个历史样本，统计准确率。如果某场景准确率 < 70%，重新调整相关参数。

---

*文档状态：v1.0 定稿，核心逻辑明确。时间衰减止损机制及所有阈值参数待回测标定后冻结，届时升级为 v1.0 正式版。*
