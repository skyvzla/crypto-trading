"""训练 spike 做空"冲高幅度预测"线性模型，输出权重模块。

研究结论（docs/research/SPIKE_OPEN_SIGNAL_V3.md）：
- 触发点（1s 起涨）之后价格通常还要冲高（中位 +12%），冲高幅度可由
  触发时可观测指标线性预测（5 折 CV rho≈0.64，13 特征，不含 atr）。
- 入场挂单价 = 触发价 × (1 + clip(0.7 × 预测冲高, 3%, 35%) / 100)。

本脚本输出:
  src/trading_platform/research/up_premium_model.py
  （特征权重、均值、标准差、常数，供 short.py 运行时预测）
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path(__file__).resolve().parents[1]
METRICS_CSV = ROOT / "reports" / "spike_anomaly_metrics.csv"
CANDLES_ROOT = ROOT / "data" / "market" / "candles"
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

# 研究确定的应用参数
PREMIUM_MULTIPLIER = 0.7
PREMIUM_MIN_PCT = 3.0
PREMIUM_MAX_PCT = 35.0


def parse_ms(iso: str) -> int:
    return int(pd.Timestamp(iso).timestamp() * 1000)


def load_events() -> tuple[pd.DataFrame, dict[str, dict[int, tuple]]]:
    m = pd.read_csv(METRICS_CSV)
    up = m[m.direction == "upward"].copy()
    up = up[
        up.ret_after_4h.notna()
        & up.vwap_dev_5m.notna()
        & up.ema_ratio_5m.notna()
        & up.roc_5m.notna()
    ]
    p50_v, p50_e, p50_r = (
        up.vwap_dev_5m.median(),
        up.ema_ratio_5m.median(),
        up.roc_5m.median(),
    )
    hi = up[
        (up.vwap_dev_5m > p50_v)
        & (up.ema_ratio_5m > p50_e)
        & (up.roc_5m > p50_r)
    ].copy()

    idx = pd.read_parquet(CANDLES_ROOT / "archive_index.parquet")
    idx1m = idx[idx.timeframe == "1m"]
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")

    cache: dict[str, dict[int, tuple]] = {}
    for sym in hi["symbol"].unique():
        ev = hi[hi["symbol"] == sym]
        ints = []
        for r in ev.itertuples():
            osc_start = parse_ms(r.osc_start_utc)
            osc_end = parse_ms(r.osc_end_utc)
            ints.append((max(osc_start - 4 * 3600_000, 0), osc_end + 8 * 3600_000))
        merged = []
        for t0, t1 in sorted(ints):
            if merged and t0 <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], t1))
            else:
                merged.append((t0, t1))
        by: dict[int, tuple] = {}
        for t0, t1 in merged:
            files = [
                os.path.join(CANDLES_ROOT, v)
                for v in idx1m[
                    (idx1m["symbol"] == sym)
                    & (idx1m["first_open_ms"] < t1)
                    & (idx1m["last_close_ms"] >= t0)
                ]["relative_path"]
            ]
            if not files:
                continue
            rows = con.execute(
                """SELECT epoch_ms(open_time),open,high,low,close
                   FROM read_parquet(?,union_by_name=true)
                   WHERE symbol=? AND timeframe='1m'
                     AND epoch_ms(open_time)>=? AND epoch_ms(open_time)<?
                   ORDER BY open_time""",
                (files, sym, t0, t1),
            ).fetchall()
            for rr in rows:
                by[rr[0]] = tuple(rr[1:])
        cache[sym] = by
    con.close()
    return hi, cache


def find_trigger(
    by: dict[int, tuple], osc_start: int, osc_end: int
) -> tuple[int, float] | None:
    bars = sorted(t for t in by if osc_start <= t <= osc_end and len(by[t]) == 4)
    closes = [float(by[t][3]) for t in bars]
    trig = None
    for i in range(5, len(bars)):
        if closes[i] / closes[i - 5] - 1 >= 0.05:
            trig = (bars[i - 5], closes[i - 5])
    return trig


def build_dataset(hi: pd.DataFrame, cache: dict) -> pd.DataFrame:
    evts = []
    for r in hi.itertuples():
        by = cache[r.symbol]
        osc_start = parse_ms(r.osc_start_utc)
        osc_end = parse_ms(r.osc_end_utc)
        trig = find_trigger(by, osc_start, osc_end)
        if trig is None:
            continue
        allbars = sorted(t for t in by if len(by[t]) == 4)
        t_max = osc_end + 300_000
        inwin = [t for t in allbars if trig[0] <= t <= t_max]
        if len(inwin) < 3:
            continue
        peak = max(by[t][1] for t in inwin)
        feat = {
            c: getattr(r, c)
            for c in FEATURES
            if hasattr(r, c) and not pd.isna(getattr(r, c))
        }
        evts.append(
            {
                "symbol": r.symbol,
                "osc_end_utc": r.osc_end_utc,
                "total_up": (peak / trig[1] - 1) * 100,
                **feat,
            }
        )
    return pd.DataFrame(evts)


def main() -> None:
    print("加载事件与 1m 行情 ...")
    hi, cache = load_events()
    ev = build_dataset(hi, cache)
    print(f"事件数: {len(ev)}")
    print(
        f"总冲高(触发→最高): 中位 {ev.total_up.median():+.2f}%, "
        f"P75 {ev.total_up.quantile(.75):+.2f}%, P90 {ev.total_up.quantile(.9):+.2f}%"
    )

    X0 = ev[FEATURES].fillna(ev[FEATURES].median())
    y = ev.total_up.values
    n = len(ev)
    mean = X0.mean().values
    std = X0.std().values + 1e-9
    X = (X0.values - mean) / std

    # 5 折交叉验证 rho（训练时按折内均值/标准差标准化）
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

    # 全样本权重（落地用）
    w = np.linalg.lstsq(np.column_stack([np.ones(n), X]), y, rcond=None)[0]
    intercept, coefs = float(w[0]), [float(c) for c in w[1:]]

    lines = [
        '"""spike 做空冲高幅度预测模型（由 tools/train_up_premium_model.py 生成，勿手改）。',
        "",
        "输入 13 个触发时点可观测特征（标准化后线性加权），输出预测冲高幅度（%）。",
        "使用方式：pred = intercept + sum(coef * (x - mean) / std)",
        '"""',
        "",
        "# 特征顺序（与训练脚本 FEATURES 一致）",
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
    OUTPUT_MODULE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MODULE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"权重模块已写入: {OUTPUT_MODULE}")
    for name, c in sorted(zip(FEATURES, coefs), key=lambda kv: -abs(kv[1])):
        print(f"  {name:>20}: {c:+.4f}")


if __name__ == "__main__":
    main()