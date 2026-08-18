"""训练"真实 1s 触发口径"冲高幅度预测模型（v2）。

数据来源: run_research_premium --mode record 输出的触发记录 CSV
（触发时点特征 + total_up_pct = 触发后 4h 内最高价相对触发价的涨幅）。

与 v1（osc_end 事后特征口径）不同，v2 特征为触发时刻实时可观测值，
输出:
  reports/up_premium_model_trigger.json   (trade 模式 --model 加载)
  src/trading_platform/research/up_premium_model.py (默认模型，覆盖 v1)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
RECORD_DIR = ROOT / "reports" / "research_record"
OUTPUT_JSON = ROOT / "reports" / "up_premium_model_trigger.json"
OUTPUT_MODULE = ROOT / "src" / "trading_platform" / "research" / "up_premium_model.py"

FEATURES = [
    "vwap_dev_5m",
    "ema_ratio_5m",
    "roc_5m",
    "amplitude_pct",
    "accel_5m",
    "pulse_1m",
    "consecutive_green",
    "vol_cv_1h",
    "rsi_5m",
    "sto_k_5m",
    "green_share_1m",
    "macd_hist_5m",
    "obv_slope_5m",
]

PREMIUM_MULTIPLIER = 0.7
PREMIUM_MIN_PCT = 3.0
PREMIUM_MAX_PCT = 35.0


def load_records() -> pd.DataFrame:
    csvs = sorted(RECORD_DIR.glob("group_*.csv"))
    if not csvs:
        raise SystemExit(f"未找到记录 CSV: {RECORD_DIR}/group_*.csv")
    df = pd.concat((pd.read_csv(c) for c in csvs), ignore_index=True)
    df = df[df.total_up_pct.notna() & df.symbol.notna()].reset_index(drop=True)
    return df


def main() -> None:
    ev = load_records()
    print(f"触发记录: {len(ev)}（{ev.symbol.nunique()} 币）")
    print(
        f"总冲高(触发→4h内最高): 中位 {ev.total_up_pct.median():+.2f}%, "
        f"P75 {ev.total_up_pct.quantile(.75):+.2f}%, P90 {ev.total_up_pct.quantile(.9):+.2f}%"
    )

    X0 = ev[FEATURES].fillna(ev[FEATURES].median())
    y = ev.total_up_pct.values
    n = len(ev)
    mean = X0.mean().values
    std = X0.std().values + 1e-9
    X = (X0.values - mean) / std

    rng = np.random.RandomState(42)
    perm = rng.permutation(n)
    folds = np.array_split(perm, 5)
    cv_pred = np.zeros(n)
    for fi in range(5):
        test = folds[fi]
        train = np.concatenate([f for j, f in enumerate(folds) if j != fi])
        mu = X0.iloc[train].mean().values
        sd = X0.iloc[train].std().values + 1e-9
        Xt = (X0.iloc[train].values - mu) / sd
        Xv = (X0.iloc[test].values - mu) / sd
        w = np.linalg.lstsq(
            np.column_stack([np.ones(len(train)), Xt]), y[train], rcond=None
        )[0]
        cv_pred[test] = np.column_stack([np.ones(len(test)), Xv]) @ w
    rho = spearmanr(cv_pred, y).statistic
    print(f"5 折 CV Spearman rho: {rho:.3f}")

    w = np.linalg.lstsq(np.column_stack([np.ones(n), X]), y, rcond=None)[0]
    intercept, coefs = float(w[0]), [float(c) for c in w[1:]]

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(
        json.dumps(
            {
                "features": FEATURES,
                "mean": {f: float(v) for f, v in zip(FEATURES, mean)},
                "std": {f: float(v) for f, v in zip(FEATURES, std)},
                "coefs": {f: float(c) for f, c in zip(FEATURES, coefs)},
                "intercept": intercept,
                "cv_rho": float(rho),
                "n": int(n),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    lines = [
        '"""spike 做空冲高幅度预测模型 v2（真实 1s 触发口径，由 tools/train_trigger_premium_model.py 生成，勿手改）。',
        "",
        "输入 13 个触发时点可观测特征（标准化后线性加权），输出预测冲高幅度（%）。",
        "使用方式：pred = intercept + sum(coef * (x - mean) / std)",
        '"""',
        "",
        f"FEATURES = {FEATURES!r}",
        "",
        f"MEAN = {[round(v, 8) for v in mean.tolist()]!r}",
        f"STD = {[round(v, 8) for v in std.tolist()]!r}",
        f"COEFS = {[round(c, 8) for c in coefs]!r}",
        f"INTERCEPT = {round(intercept, 8)}",
        "",
        "PREMIUM_MULTIPLIER = 0.7",
        "PREMIUM_MIN_PCT = 3.0",
        "PREMIUM_MAX_PCT = 35.0",
        "",
        "",
        "def predict_up_pct(features: dict[str, float]) -> float:",
        '    """features: {特征名: 值}。返回预测冲高幅度（%）。缺省特征按 0（均值）处理。"""',
        "    pred = INTERCEPT",
        "    for name, mean, std, coef in zip(FEATURES, MEAN, STD, COEFS):",
        "        x = features.get(name)",
        "        if x is None:",
        "            continue",
        "        pred += coef * (x - mean) / std",
        "    return float(pred)",
        "",
        "",
        "def entry_premium_pct(pred_up_pct: float) -> float:",
        '    """挂单价溢价百分比 = clip(multiplier * 预测冲高, min, max)。"""',
        "    raw = PREMIUM_MULTIPLIER * pred_up_pct",
        "    return min(max(raw, PREMIUM_MIN_PCT), PREMIUM_MAX_PCT)",
        "",
    ]
    OUTPUT_MODULE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"模型写入: {OUTPUT_JSON}")
    print(f"默认模块覆盖: {OUTPUT_MODULE}")
    for name, c in sorted(zip(FEATURES, coefs), key=lambda kv: -abs(kv[1])):
        print(f"  {name:>20}: {c:+.4f}")


if __name__ == "__main__":
    main()