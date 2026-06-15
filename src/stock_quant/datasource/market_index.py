"""市场环境数据：VIX / VVIX / SPY / QQQ / SOXX / SMH / DIA / IWM 一键拉取。

用途：
  - VIX / VVIX → 大盘恐慌指数，决定整体仓位敞口
  - SPY / QQQ / DIA / IWM → 大盘 + 板块方向参考
  - SOXX / SMH → 半导体板块龙头
  - XLF / XLK / XLE / XLV → 行业 ETF
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from .futu import FutuSource


# 注意：富途 VIX 代码为 US.VIX（指数），SPY/QQQ 等是 ETF 直拉
DEFAULT_TICKERS: dict[str, str] = {
    "VIX":  "US.VIX",       # 恐慌指数
    "SPY":  "US.SPY",       # 标普 500
    "QQQ":  "US.QQQ",       # 纳指 100
    "DIA":  "US.DIA",       # 道琼斯
    "IWM":  "US.IWM",       # 罗素 2000
    "SMH":  "US.SMH",       # 半导体
    "SOXX": "US.SOXX",      # 半导体
    "XLK":  "US.XLK",       # 科技
    "XLF":  "US.XLF",       # 金融
}


# 富途指数代码 → yfinance 代码映射（兜底用）
_FUTU_TO_YF: dict[str, str] = {
    "US.VIX":  "^VIX",
    "US.VVIX": "^VVIX",
    "US.SPX":  "^GSPC",
    "US.IXIC": "^IXIC",
    "US.DJI":  "^DJI",
    "US.RUT":  "^RUT",
}


def _to_yf_symbol(futu_code: str) -> str:
    """把富途代码转成 yfinance 代码。ETF 类直接去前缀；指数走映射表。"""
    if futu_code in _FUTU_TO_YF:
        return _FUTU_TO_YF[futu_code]
    if futu_code.startswith("US."):
        return futu_code[3:]
    return futu_code


def _quote_from_futu(fs: FutuSource, futu_code: str) -> dict[str, Any] | None:
    try:
        q = fs.get_quote(futu_code)
        if not q or q.get("price") is None:
            return None
        return {
            "price": q.get("price"),
            "change_rate": q.get("change_rate"),
            "prev_close": q.get("prev_close"),
            "volume": q.get("volume"),
            "high": q.get("high"),
            "low": q.get("low"),
            "_source": "futu",
        }
    except Exception:
        return None


def _quote_from_yfinance(futu_code: str) -> dict[str, Any] | None:
    """yfinance 兜底：用最近 2 天日线计算 price/change_rate/prev_close 等。"""
    try:
        from .yahoo import YahooSource
    except Exception:
        return None
    yf_sym = _to_yf_symbol(futu_code)
    try:
        ys = YahooSource()
        hist = ys.get_history(yf_sym, period="5d", interval="1d")
        if hist is None or hist.empty:
            return None
        last = hist.iloc[-1]
        price = float(last["Close"])
        prev_close = float(hist.iloc[-2]["Close"]) if len(hist) >= 2 else None
        change_rate = (
            (price - prev_close) / prev_close * 100
            if prev_close not in (None, 0)
            else None
        )
        volume = float(last["Volume"]) if "Volume" in last else None
        high = float(last["High"]) if "High" in last else None
        low = float(last["Low"]) if "Low" in last else None
        return {
            "price": price,
            "change_rate": change_rate,
            "prev_close": prev_close,
            "volume": volume,
            "high": high,
            "low": low,
            "_source": "yfinance",
        }
    except Exception:
        return None


def _safe_quote(fs: FutuSource | None, futu_code: str) -> dict[str, Any] | None:
    """优先富途，失败/空数据时降级 yfinance。"""
    if fs is not None:
        q = _quote_from_futu(fs, futu_code)
        if q and q.get("price") is not None:
            return q
    return _quote_from_yfinance(futu_code)


def get_market_snapshot(tickers: dict[str, str] | None = None) -> dict[str, Any]:
    """返回 {ticker_name: {price, change_rate, ..., _source}}。

    实现："富途优先 + yfinance 兜底"：
      1. 优先调用 Futu OpenD 的 get_quote；
      2. 当 FutuOpenD 不可用 / 该 ticker 无权限 / 价格为空时，自动降级到 yfinance；
      3. 返回字段附带 `_source` 标记数据来源，便于上层 debug。
    """
    tickers = tickers or DEFAULT_TICKERS
    try:
        fs: FutuSource | None = FutuSource()
    except Exception:
        fs = None
    out: dict[str, Any] = {}
    for name, code in tickers.items():
        q = _safe_quote(fs, code)
        if q and q.get("price") is not None:
            out[name] = q
    return out


def interpret_vix(vix_price: float | None) -> str:
    if vix_price is None:
        return "unknown"
    if vix_price < 13:
        return "extreme_low (complacent)"
    if vix_price < 18:
        return "low (calm)"
    if vix_price < 25:
        return "medium (normal)"
    if vix_price < 35:
        return "high (fear)"
    return "extreme_high (panic)"
