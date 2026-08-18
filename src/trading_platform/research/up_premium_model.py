"""spike 做空冲高幅度预测模型 v2（真实 1s 触发口径，由 tools/train_trigger_premium_model.py 生成，勿手改）。

输入 13 个触发时点可观测特征（标准化后线性加权），输出预测冲高幅度（%）。
使用方式：pred = intercept + sum(coef * (x - mean) / std)
"""

FEATURES = ['vwap_dev_5m', 'ema_ratio_5m', 'roc_5m', 'amplitude_pct', 'accel_5m', 'pulse_1m', 'consecutive_green', 'vol_cv_1h', 'rsi_5m', 'sto_k_5m', 'green_share_1m', 'macd_hist_5m', 'obv_slope_5m']

MEAN = [3.51500214, 1.00435721, 7.90891423, 93.92547867, 0.09934753, 0.15347523, 2.34121622, 1.33505477, 57.97375102, 69.55773688, 0.48379004, 0.00335558, -23676052.32115138]
STD = [22.9285487, 0.24981633, 37.96599867, 99.11169674, 0.50367684, 0.15569442, 1.75315117, 0.87699925, 24.97130765, 36.08449333, 0.09019561, 0.29902031, 1093836315.5465772]
COEFS = [-11.91003873, -22.18882476, -5.06971983, 6.66151028, -0.77470744, 1.85298761, 1.88254152, 3.91728897, 9.47853496, -0.4583119, 4.45212118, 2.33985988, -0.82258804]
INTERCEPT = 33.15001753

PREMIUM_MULTIPLIER = 0.7
PREMIUM_MIN_PCT = 3.0
PREMIUM_MAX_PCT = 35.0


def predict_up_pct(features: dict[str, float]) -> float:
    """features: {特征名: 值}。返回预测冲高幅度（%）。缺省特征按 0（均值）处理。"""
    pred = INTERCEPT
    for name, mean, std, coef in zip(FEATURES, MEAN, STD, COEFS):
        x = features.get(name)
        if x is None:
            continue
        pred += coef * (x - mean) / std
    return float(pred)


def entry_premium_pct(pred_up_pct: float) -> float:
    """挂单价溢价百分比 = clip(multiplier * 预测冲高, min, max)。"""
    raw = PREMIUM_MULTIPLIER * pred_up_pct
    return min(max(raw, PREMIUM_MIN_PCT), PREMIUM_MAX_PCT)

