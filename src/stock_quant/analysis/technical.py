"""技术指标：基于 ta 库（纯 Python，支持 3.11）。

包含：
- 日线级指标：MA / RSI / MACD / 布林带
- 分时级（intraday）盘中走势分析：VWAP / 开盘区间突破 / 量价节奏 / 盘中趋势 / 关键位
"""
from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Optional

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator, EMAIndicator
from ta.volatility import BollingerBands, AverageTrueRange


def add_moving_averages(df: pd.DataFrame, windows: tuple[int, ...] = (5, 10, 20, 50, 200)) -> pd.DataFrame:
    """为 OHLC DataFrame 添加 MA 列。"""
    df = df.copy()
    for w in windows:
        df[f"MA{w}"] = SMAIndicator(df["Close"], window=w).sma_indicator()
    return df


def add_rsi(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    df = df.copy()
    df[f"RSI{window}"] = RSIIndicator(df["Close"], window=window).rsi()
    return df


def add_macd(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    macd = MACD(df["Close"])
    df["MACD"] = macd.macd()
    df["MACD_SIGNAL"] = macd.macd_signal()
    df["MACD_HIST"] = macd.macd_diff()
    return df


def add_bbands(df: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    df = df.copy()
    bb = BollingerBands(df["Close"], window=window)
    df["BB_HIGH"] = bb.bollinger_hband()
    df["BB_LOW"] = bb.bollinger_lband()
    df["BB_MID"] = bb.bollinger_mavg()
    return df


def enrich_all(df: pd.DataFrame) -> pd.DataFrame:
    """一次性把 MA + RSI + MACD + 布林带全部加上。"""
    df = add_moving_averages(df)
    df = add_rsi(df)
    df = add_macd(df)
    df = add_bbands(df)
    return df


def latest_signals(df: pd.DataFrame) -> dict:
    """返回最新一根K线的关键信号摘要。"""
    df = enrich_all(df)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    def _cross(a_now, b_now, a_prev, b_prev):
        if a_prev <= b_prev and a_now > b_now:
            return "golden_cross"
        if a_prev >= b_prev and a_now < b_now:
            return "death_cross"
        return "none"

    rsi = last.get("RSI14")
    rsi_state = "neutral"
    if pd.notna(rsi):
        if rsi > 70:
            rsi_state = "overbought"
        elif rsi < 30:
            rsi_state = "oversold"

    return {
        "close": float(last["Close"]),
        "ma20": float(last["MA20"]) if pd.notna(last["MA20"]) else None,
        "ma50": float(last["MA50"]) if pd.notna(last["MA50"]) else None,
        "ma_cross_20_50": _cross(last["MA20"], last["MA50"], prev["MA20"], prev["MA50"]),
        "rsi14": float(rsi) if pd.notna(rsi) else None,
        "rsi_state": rsi_state,
        "macd_hist": float(last["MACD_HIST"]) if pd.notna(last["MACD_HIST"]) else None,
        "bb_position": (
            "upper" if last["Close"] >= last["BB_HIGH"]
            else "lower" if last["Close"] <= last["BB_LOW"]
            else "middle"
        ),
    }


def _normalize_intraday(df: pd.DataFrame) -> pd.DataFrame:
    """规范化分时数据字段：Futu 原生列名 time_key / open / close / ..."""
    df = df.copy()
    rename_map = {
        "time_key": "DateTime",
        "open": "Open", "close": "Close", "high": "High", "low": "Low",
        "volume": "Volume", "turnover": "Turnover",
    }
    for src, dst in rename_map.items():
        if src in df.columns and dst not in df.columns:
            df = df.rename(columns={src: dst})
    if "DateTime" in df.columns:
        df["DateTime"] = pd.to_datetime(df["DateTime"])
        df = df.sort_values("DateTime").reset_index(drop=True)
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """计算当日 VWAP（按自然日分组重置）。"""
    df = df.copy()
    if "DateTime" not in df.columns or "Volume" not in df.columns:
        return df
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["_pv"] = tp * df["Volume"]
    df["_date"] = df["DateTime"].dt.date
    df["VWAP"] = df.groupby("_date")["_pv"].cumsum() / df.groupby("_date")["Volume"].cumsum()
    return df.drop(columns=["_pv", "_date"])


def add_atr(df: pd.DataFrame, window: int = 14) -> pd.DataFrame:
    df = df.copy()
    atr = AverageTrueRange(df["High"], df["Low"], df["Close"], window=window)
    df[f"ATR{window}"] = atr.average_true_range()
    return df


def enrich_intraday(df: pd.DataFrame) -> pd.DataFrame:
    """一次性给 15min 分时数据加上 EMA9/EMA21/VWAP/RSI14/MACD/BBands/ATR14。"""
    df = _normalize_intraday(df)
    if df.empty:
        return df
    df["EMA9"] = EMAIndicator(df["Close"], window=9).ema_indicator()
    df["EMA21"] = EMAIndicator(df["Close"], window=21).ema_indicator()
    df["EMA55"] = EMAIndicator(df["Close"], window=55).ema_indicator()
    df = add_vwap(df)
    df = add_rsi(df, window=14)
    df = add_macd(df)
    df = add_bbands(df, window=20)
    df = add_atr(df, window=14)
    return df


def opening_range(df: pd.DataFrame, minutes: int = 30) -> Optional[dict]:
    """计算最近一个交易日的 开盘区间 (Opening Range)。
    
    返回：high / low / mid 及当前收盘价相对 ORB 的突破状态。
    适用于 5m / 15m / 30m K 线。
    """
    df = _normalize_intraday(df)
    if df.empty or "DateTime" not in df.columns:
        return None
    last_date = df["DateTime"].dt.date.max()
    day_df = df[df["DateTime"].dt.date == last_date].reset_index(drop=True)
    if day_df.empty:
        return None
    # 估算 K 线粒度
    if len(day_df) >= 2:
        bar_min = int((day_df["DateTime"].iloc[1] - day_df["DateTime"].iloc[0]).total_seconds() // 60) or 15
    else:
        bar_min = 15
    n_bars = max(1, minutes // bar_min)
    orb = day_df.iloc[:n_bars]
    high = float(orb["High"].max())
    low = float(orb["Low"].min())
    mid = (high + low) / 2.0
    close = float(day_df["Close"].iloc[-1])
    state = "inside"
    if close > high:
        state = "breakout_up"
    elif close < low:
        state = "breakout_down"
    return {
        "orb_minutes": minutes,
        "bars_used": int(n_bars),
        "high": high,
        "low": low,
        "mid": mid,
        "range_pct": round((high - low) / mid * 100, 2) if mid else None,
        "close": close,
        "state": state,
    }


def intraday_trend(df: pd.DataFrame) -> dict:
    """基于 EMA9 / EMA21 / VWAP 的盘中多空节奏判定。"""
    df = enrich_intraday(df)
    if df.empty:
        return {"trend": "unknown"}
    last = df.iloc[-1]
    close = float(last["Close"])
    ema9 = float(last["EMA9"]) if pd.notna(last["EMA9"]) else None
    ema21 = float(last["EMA21"]) if pd.notna(last["EMA21"]) else None
    vwap = float(last["VWAP"]) if "VWAP" in df.columns and pd.notna(last["VWAP"]) else None

    score = 0
    reasons: list[str] = []
    if ema9 and ema21:
        if ema9 > ema21 and close > ema9:
            score += 2
            reasons.append("EMA9>EMA21 且价格站上 EMA9（短线多头）")
        elif ema9 < ema21 and close < ema9:
            score -= 2
            reasons.append("EMA9<EMA21 且价格跌破 EMA9（短线空头）")
    if vwap:
        if close > vwap * 1.002:
            score += 1
            reasons.append(f"价格 {close:.2f} 站稳 VWAP {vwap:.2f} 上方（买方主导）")
        elif close < vwap * 0.998:
            score -= 1
            reasons.append(f"价格 {close:.2f} 位于 VWAP {vwap:.2f} 下方（卖方主导）")

    rsi = float(last["RSI14"]) if pd.notna(last["RSI14"]) else None
    macd_hist = float(last["MACD_HIST"]) if pd.notna(last["MACD_HIST"]) else None
    if rsi is not None:
        if rsi > 70:
            reasons.append(f"分时 RSI14={rsi:.1f} 超买")
        elif rsi < 30:
            reasons.append(f"分时 RSI14={rsi:.1f} 超卖")
    if macd_hist is not None:
        reasons.append(f"分时 MACD柱={macd_hist:+.3f}")
        score += 1 if macd_hist > 0 else -1

    trend = "neutral"
    if score >= 2:
        trend = "bullish"
    elif score <= -2:
        trend = "bearish"

    return {
        "trend": trend,
        "score": score,
        "close": close,
        "ema9": ema9,
        "ema21": ema21,
        "vwap": vwap,
        "rsi14": rsi,
        "macd_hist": macd_hist,
        "reasons": reasons,
    }


def intraday_volume_profile(df: pd.DataFrame, bins: int = 20) -> Optional[dict]:
    """最近一个交易日的 成交量分布 (简化 VPVR)。
    
    输出 POC（成交最密集价位）与 Value Area 70% 区间。
    """
    df = _normalize_intraday(df)
    if df.empty or "DateTime" not in df.columns:
        return None
    last_date = df["DateTime"].dt.date.max()
    day_df = df[df["DateTime"].dt.date == last_date]
    if day_df.empty:
        return None
    tp = (day_df["High"] + day_df["Low"] + day_df["Close"]) / 3.0
    vol = day_df["Volume"].astype(float)
    lo, hi = float(tp.min()), float(tp.max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    hist, _ = np.histogram(tp, bins=edges, weights=vol)
    poc_idx = int(hist.argmax())
    poc_price = (edges[poc_idx] + edges[poc_idx + 1]) / 2.0
    # Value Area 70%
    total = hist.sum()
    order = hist.argsort()[::-1]
    cum = 0.0
    selected = []
    for idx in order:
        cum += hist[idx]
        selected.append(idx)
        if cum >= total * 0.7:
            break
    va_lo = float(min(edges[i] for i in selected))
    va_hi = float(max(edges[i + 1] for i in selected))
    return {
        "poc": round(poc_price, 2),
        "value_area_low": round(va_lo, 2),
        "value_area_high": round(va_hi, 2),
        "day": str(last_date),
    }


def intraday_signals(df: pd.DataFrame) -> dict:
    """分时级一站式信号摘要（给 decide/brief 使用）。
    
    参数 df 可以是 15m / 5m / 30m K 线，需要包含 time_key/open/close/high/low/volume 字段。
    """
    df = _normalize_intraday(df)
    if df.empty:
        return {"available": False, "reason": "无分时数据"}
    trend = intraday_trend(df)
    orb = opening_range(df, minutes=30)
    vp = intraday_volume_profile(df)

    last_date = df["DateTime"].dt.date.max()
    day_df = df[df["DateTime"].dt.date == last_date]
    day_high = float(day_df["High"].max()) if not day_df.empty else None
    day_low = float(day_df["Low"].min()) if not day_df.empty else None
    day_open = float(day_df["Open"].iloc[0]) if not day_df.empty else None
    day_close = float(day_df["Close"].iloc[-1]) if not day_df.empty else None
    day_vol = float(day_df["Volume"].sum()) if not day_df.empty else None
    # 日内累计涨跌幅
    day_change_pct = None
    if day_open and day_close:
        day_change_pct = round((day_close - day_open) / day_open * 100, 2)

    # 最近 N 日同时段均量用来判定放量
    df["_date"] = df["DateTime"].dt.date
    prior_days = df[df["_date"] < last_date]
    avg_day_vol = None
    vol_ratio = None
    if not prior_days.empty:
        prior_day_vols = prior_days.groupby("_date")["Volume"].sum()
        if len(prior_day_vols) > 0:
            avg_day_vol = float(prior_day_vols.tail(5).mean())
            if avg_day_vol and day_vol:
                vol_ratio = round(day_vol / avg_day_vol, 2)

    return {
        "available": True,
        "day": str(last_date),
        "day_open": day_open,
        "day_high": day_high,
        "day_low": day_low,
        "day_close": day_close,
        "day_change_pct": day_change_pct,
        "day_volume": day_vol,
        "avg_5d_volume": avg_day_vol,
        "volume_ratio": vol_ratio,
        "trend": trend,
        "opening_range": orb,
        "volume_profile": vp,
    }


def format_intraday_signals(sig: dict) -> str:
    """把 intraday_signals 结果格式化为可读文本块。"""
    if not sig.get("available"):
        return f"⏱  分时分析不可用：{sig.get('reason', '无数据')}"
    lines = []
    lines.append(f"⏱  【分时级盘中走势 · {sig.get('day')}】")
    if sig.get("day_open") is not None:
        lines.append(
            f"  开={sig['day_open']:.2f}  高={sig['day_high']:.2f}  低={sig['day_low']:.2f}  "
            f"收={sig['day_close']:.2f}  日内涨跌={sig['day_change_pct']:+.2f}%"
        )
    if sig.get("volume_ratio"):
        v_state = "放量" if sig["volume_ratio"] > 1.2 else ("缩量" if sig["volume_ratio"] < 0.8 else "平量")
        lines.append(f"  量能: 今日 {sig['day_volume']:.0f} / 5日均量 {sig['avg_5d_volume']:.0f} = {sig['volume_ratio']}x ({v_state})")
    t = sig.get("trend", {})
    if t:
        if t.get("ema9") is not None:
            ema9_str = f"{t['ema9']:.2f}"
            vwap_str = f"{t['vwap']:.2f}" if t.get("vwap") is not None else "-"
            lines.append(
                f"  分时趋势: {t.get('trend')} (score={t.get('score')}) | "
                f"EMA9={ema9_str}  VWAP={vwap_str}"
            )
        else:
            lines.append(f"  分时趋势: {t.get('trend')}")
        for r in t.get("reasons", []):
            lines.append(f"    · {r}")
    orb = sig.get("opening_range")
    if orb:
        lines.append(
            f"  开盘区间(30min): {orb['low']:.2f} ~ {orb['high']:.2f} (幅{orb['range_pct']}%) | 当前: {orb['state']}"
        )
    vp = sig.get("volume_profile")
    if vp:
        lines.append(
            f"  成交密集 POC={vp['poc']}  Value Area {vp['value_area_low']}-{vp['value_area_high']}"
        )
    return "\n".join(lines)
