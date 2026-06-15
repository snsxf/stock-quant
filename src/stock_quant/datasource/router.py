"""智能路由：根据标的代码自动选择数据源。

约定：
- 富途风格代码：`HK.00700` / `US.NVDA` / `SH.600519` / `SZ.000001`
- yfinance 风格：`NVDA` / `0700.HK` / `600519.SS` / `000001.SZ`
"""
from __future__ import annotations

from .base import DataSource
from .futu import FutuSource
from .yahoo import YahooSource


def detect_market(symbol: str) -> str:
    """返回 'HK' / 'US' / 'CN' / 'UNKNOWN'。"""
    s = symbol.upper()
    if s.startswith(("HK.", "US.", "SH.", "SZ.")):
        return {"HK": "HK", "US": "US", "SH": "CN", "SZ": "CN"}[s.split(".", 1)[0]]
    if s.endswith(".HK"):
        return "HK"
    if s.endswith((".SS", ".SZ")):
        return "CN"
    return "US"


def to_yahoo_symbol(symbol: str) -> str:
    """把富途风格代码转 yfinance 风格。"""
    if symbol.startswith("US."):
        return symbol[3:]
    if symbol.startswith("HK."):
        return symbol[3:].lstrip("0").zfill(4) + ".HK"
    if symbol.startswith("SH."):
        return symbol[3:] + ".SS"
    if symbol.startswith("SZ."):
        return symbol[3:] + ".SZ"
    return symbol


def to_futu_symbol(symbol: str) -> str:
    """把 yfinance 风格代码转富途风格。"""
    s = symbol.upper()
    if s.startswith(("HK.", "US.", "SH.", "SZ.")):
        return s
    if s.endswith(".HK"):
        return "HK." + s[:-3].zfill(5)
    if s.endswith(".SS"):
        return "SH." + s[:-3]
    if s.endswith(".SZ"):
        return "SZ." + s[:-3]
    return "US." + s


def get_source(
    symbol: str,
    *,
    prefer_futu: bool = True,
    futu_available: bool | None = None,
) -> tuple[DataSource, str]:
    """
    根据 symbol 自动返回 (数据源实例, 适配后的 symbol)。

    规则：
    - 港股 / A 股 → 必须富途
    - 美股 → 优先富途（含期权 Greeks），失败则 yfinance
    """
    market = detect_market(symbol)

    if market in ("HK", "CN"):
        return FutuSource(), to_futu_symbol(symbol)

    if prefer_futu and (futu_available is None or futu_available):
        try:
            src = FutuSource()
            return src, to_futu_symbol(symbol)
        except Exception:
            pass
    return YahooSource(), to_yahoo_symbol(symbol)
