"""智能路由：根据标的代码自动选择数据源。

约定：
- 富途风格代码：`HK.00700` / `US.NVDA` / `SH.600519` / `SZ.000001`
- yfinance 风格：`NVDA` / `0700.HK` / `600519.SS` / `000001.SZ`
- yfinance 原生宏观/期货/加密符号（富途不识别，必须直接走 yfinance）：
    * 指数  `^VIX` / `^VVIX` / `^VIX3M` / `^SKEW` / `^TNX` / `^IRX` / `^TYX`
    * 美元  `DX-Y.NYB` / `DXY`（DXY 自动映射为 `DX-Y.NYB`）
    * 期货  `GC=F` / `CL=F` / `SI=F` / `HG=F` / `NG=F` / `DX=F`
    * 加密  `BTC-USD` / `ETH-USD` / `SOL-USD` 等
"""
from __future__ import annotations

from .base import DataSource
from .futu import FutuSource
from .yahoo import YahooSource


# DXY 没有标准 yfinance 代码，常用 `DX-Y.NYB`（ICE 美元指数）
_DXY_ALIAS = {"DXY": "DX-Y.NYB"}


def _is_yf_native(symbol: str) -> bool:
    """识别 yfinance 原生宏观/期货/加密符号。富途完全不识别这些，必须直接走 yfinance。"""
    s = symbol.upper()
    if s.startswith("^"):                 # ^VIX / ^TNX 等
        return True
    if s.endswith("=F"):                  # GC=F / CL=F 等期货
        return True
    if "-" in s and s.split("-")[-1] in {"USD", "USDT", "EUR", "GBP", "JPY", "CNY"}:
        return True                       # BTC-USD / ETH-USD 等
    if s in _DXY_ALIAS or s == "DX-Y.NYB":
        return True
    return False


def detect_market(symbol: str) -> str:
    """返回 'HK' / 'US' / 'CN' / 'YF_NATIVE'。"""
    s = symbol.upper()
    if _is_yf_native(s):
        return "YF_NATIVE"
    if s.startswith(("HK.", "US.", "SH.", "SZ.")):
        return {"HK": "HK", "US": "US", "SH": "CN", "SZ": "CN"}[s.split(".", 1)[0]]
    if s.endswith(".HK"):
        return "HK"
    if s.endswith((".SS", ".SZ")):
        return "CN"
    return "US"


def to_yahoo_symbol(symbol: str) -> str:
    """把富途风格代码转 yfinance 风格。yfinance 原生符号原样返回（DXY → DX-Y.NYB）。"""
    s = symbol.upper()
    if _is_yf_native(s):
        return _DXY_ALIAS.get(s, s)
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
    - yfinance 原生符号（^VIX / GC=F / BTC-USD / DXY 等）→ 直接 yfinance
    - 港股 / A 股 → 必须富途
    - 美股 → 优先富途（含期权 Greeks），失败则 yfinance
    """
    market = detect_market(symbol)

    if market == "YF_NATIVE":
        return YahooSource(), to_yahoo_symbol(symbol)

    if market in ("HK", "CN"):
        return FutuSource(), to_futu_symbol(symbol)

    if prefer_futu and (futu_available is None or futu_available):
        try:
            src = FutuSource()
            return src, to_futu_symbol(symbol)
        except Exception:
            pass
    return YahooSource(), to_yahoo_symbol(symbol)
